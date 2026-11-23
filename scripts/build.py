#!/usr/bin/env python3
"""Generate the profile README cards and README.md from live GitHub data.

Outputs:
  assets/card.svg            dossier header (profile photo, stats, activity)
  assets/stack.svg           languages and tools detected across public repos
  assets/notes.svg           current focus and offline notes
  assets/projects/<repo>.svg one tile per public repository
  README.md                  assembled profile layout

Needs GITHUB_TOKEN (or GH_TOKEN) in the environment. Pillow + fonttools[woff].
"""
from __future__ import annotations

import base64
import functools
import html
import io
import json
import os
import random
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fontTools import subset
from fontTools.ttLib import TTFont
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONTS = ASSETS / "fonts"
USER = "ZnOw01"

# ── palette ──────────────────────────────────────────────────────────────
PAPER = "#B7D8CD"    # mint paper
INK = "#121A18"      # print ink
INK_SOFT = "#3E5852"
PUMPKIN = "#E4572E"  # the one accent

# Everything below is the owner's own words. Repos, languages and tools are
# NOT listed here: they are discovered from the GitHub API on every run.
PROFILE: dict[str, str] = {
    "role": "CS STUDENT",
    "mode": "BUILD · BREAK · LEARN",
    "reading": "CODE · LANGUAGES · TOOLS · LINUX",
    "tagline": "Always building things, breaking things and learning a bit more every day.",
}
NOW: list[tuple[str, list[str]]] = [
    ("BUILDING", ["New projects"]),
    ("READING ABOUT", ["Programming", "Programming languages", "Tools", "Linux"]),
]
OFFLINE: list[tuple[str, list[str]]] = [
    ("VIDEO GAMES", []),
    ("BOOKS", ["Programming", "Classic literature"]),
    ("LEARNING", ["English"]),
]

# file or directory name (at depth <= 2 of a repo) -> tool it reveals
TOOL_MARKERS: dict[str, str] = {
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker",
    "compose.yaml": "Docker",
    "uv.lock": "uv",
    "requirements.txt": "pip",
    "bun.lock": "Bun",
    "bun.lockb": "Bun",
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "CMakeLists.txt": "CMake",
    "meson.build": "Meson",
    "Makefile": "Make",
    "Cargo.toml": "Cargo",
    "go.mod": "Go modules",
    "flake.nix": "Nix",
    "vcpkg.json": "vcpkg",
    "astro.config.mjs": "Astro",
    "astro.config.ts": "Astro",
    "vite.config.ts": "Vite",
    "vitest.config.ts": "Vitest",
    "vitest.config.mts": "Vitest",
    "tailwind.config.js": "Tailwind",
    "components.json": "shadcn/ui",
    "conftest.py": "pytest",
    "pytest.ini": "pytest",
    ".github/workflows": "GitHub Actions",
    ".pre-commit-config.yaml": "pre-commit",
    "ruff.toml": "Ruff",
    ".prettierrc": "Prettier",
    "eslint.config.js": "ESLint",
    "Doxyfile": "Doxygen",
    ".clang-format": "clang-format",
}

BAYER8: tuple[tuple[int, ...], ...] = (
    (0, 48, 12, 60, 3, 51, 15, 63),
    (32, 16, 44, 28, 35, 19, 47, 31),
    (8, 56, 4, 52, 11, 59, 7, 55),
    (40, 24, 36, 20, 43, 27, 39, 23),
    (2, 50, 14, 62, 1, 49, 13, 61),
    (34, 18, 46, 30, 33, 17, 45, 29),
    (10, 58, 6, 54, 9, 57, 5, 53),
    (42, 26, 38, 22, 41, 25, 37, 21),
)

FONT_SPECS: dict[str, tuple[str, str]] = {
    "t": ("JBM", "JetBrainsMono-Regular.ttf"),
    "b": ("JBMB", "JetBrainsMono-Bold.ttf"),
    "x": ("JBMX", "JetBrainsMono-ExtraBold.ttf"),
}

_SAFE_REPO_RE = re.compile(r"[A-Za-z0-9._-]+")

_GRAPHQL_QUERY = """query($login: String!, $cursor: String) {
  user(login: $login) {
    name
    bio
    avatarUrl(size: 760)
    followers { totalCount }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
          }
        }
      }
    }
    repositories(
      first: 100
      after: $cursor
      privacy: PUBLIC
      ownerAffiliations: OWNER
      orderBy: {field: PUSHED_AT, direction: DESC}
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        name
        description
        isFork
        stargazerCount
        pushedAt
        parent { nameWithOwner }
        languages(first: 50, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node { name }
          }
        }
        rootTree: object(expression: "HEAD:") {
          ... on Tree {
            entries {
              name
              object {
                ... on Tree {
                  entries {
                    name
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}"""


@dataclass(frozen=True)
class Repo:
    name: str
    description: str
    fork: bool
    parent: str | None
    stars: int
    pushed_at: str
    langs: tuple[str, ...]
    tools: tuple[str, ...]


@dataclass(frozen=True)
class ProfileData:
    name: str
    bio: str
    avatar_url: str
    followers: int
    stars: int
    contrib: int
    weeks: tuple[int, ...]
    langs: tuple[tuple[str, int], ...]
    tools: tuple[tuple[str, int], ...]
    repos: tuple[Repo, ...]


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, message: str, url: str) -> None:
        self.status = status
        self.url = url
        super().__init__(f"GitHub API {status} for {url}: {message}")


# ── data ─────────────────────────────────────────────────────────────────
def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        raise SystemExit("set GITHUB_TOKEN or GH_TOKEN")
    return tok


def _validate_https(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"refusing non-HTTPS URL: {url}")


def _req(url: str, body: dict[str, Any] | None = None) -> Any:
    _validate_https(url)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-readme",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    for attempt in range(3):
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
                msg = str(parsed.get("message") or raw)
            except ValueError:
                msg = raw or err.reason
            if err.code >= 500 and attempt < 2:
                time.sleep(1 << attempt)
                continue
            raise GitHubAPIError(err.code, msg, url) from err
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt < 2:
                time.sleep(1 << attempt)
                continue
            raise RuntimeError(f"GitHub request failed for {url}: {err}") from err
    raise AssertionError("unreachable")


def extract_tools(root_tree: dict[str, Any] | None) -> set[str]:
    """Identify tooling markers within depth <= 2 of a repository's HEAD tree."""
    if not root_tree:
        return set()
    found: set[str] = set()
    for entry in root_tree.get("entries") or ():
        name = entry.get("name") or ""
        if tool := TOOL_MARKERS.get(name):
            found.add(tool)
        sub_tree = entry.get("object")
        if not isinstance(sub_tree, dict):
            continue
        for child in sub_tree.get("entries") or ():
            cname = child.get("name") or ""
            for key in (f"{name}/{cname}", cname):
                if tool := TOOL_MARKERS.get(key):
                    found.add(tool)
    return found


def _extract_readme_tagline(markdown: str) -> str:
    """Return the first plain prose line (>12 chars) from a README markdown string."""
    in_fence = False
    in_comment = False
    for line in markdown.splitlines():
        s = line.strip()
        if in_comment:
            if "-->" in s:
                in_comment = False
            continue
        if s.startswith("<!--"):
            if "-->" not in s:
                in_comment = True
            continue
        if s.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not s or s.startswith(("#", "<", "[", "!", "|", ">", "-", "* ")):
            continue
        s = re.sub(r"[*_`]|<br\s*/?>", "", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s).strip()
        if len(s) > 12:
            return s
    return ""


def readme_tagline(name: str) -> str:
    """First plain sentence of a repo README, used only when it has no description."""
    quoted = urllib.parse.quote(name, safe="")
    try:
        raw = _req(f"https://api.github.com/repos/{USER}/{quoted}/readme")
        text = base64.b64decode(raw["content"]).decode("utf-8", "replace")
    except GitHubAPIError as err:
        if err.status != 404:
            print(f"warning: readme fetch skipped for {name}: {err}", file=sys.stderr)
        return ""
    except (RuntimeError, KeyError, ValueError) as err:
        print(f"warning: readme parse skipped for {name}: {err}", file=sys.stderr)
        return ""
    return _extract_readme_tagline(text)


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def fetch() -> ProfileData:
    cursor: str | None = None
    user_meta: dict[str, Any] | None = None
    raw_nodes: list[dict[str, Any]] = []

    while True:
        gql = _req(
            "https://api.github.com/graphql",
            {"query": _GRAPHQL_QUERY, "variables": {"login": USER, "cursor": cursor}},
        )
        if errors := gql.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {errors}")
        user = (gql.get("data") or {}).get("user")
        if user is None:
            raise RuntimeError(f"GitHub user {USER!r} not found")
        if user_meta is None:
            user_meta = user
        repo_conn = user["repositories"]
        raw_nodes.extend(n for n in repo_conn["nodes"] if n and n["name"] != USER)
        page_info = repo_conn["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    assert user_meta is not None
    cal = user_meta["contributionsCollection"]["contributionCalendar"]
    weeks = tuple(
        sum(day["contributionCount"] for day in week["contributionDays"])
        for week in cal["weeks"]
    )

    lang_repos: dict[str, int] = {}
    lang_bytes: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    repos: list[Repo] = []

    for node in raw_nodes:
        edges = (node.get("languages") or {}).get("edges") or []
        total_bytes = sum(edge["size"] for edge in edges) or 1
        sorted_edges = sorted(
            edges, key=lambda edge: (-edge["size"], edge["node"]["name"].lower())
        )
        # a language counts for a repo when it is at least 2% of its code
        repo_langs = tuple(
            edge["node"]["name"] for edge in sorted_edges if edge["size"] / total_bytes >= 0.02
        )
        for edge in sorted_edges:
            lname = edge["node"]["name"]
            if edge["size"] / total_bytes >= 0.02:
                lang_repos[lname] = lang_repos.get(lname, 0) + 1
                lang_bytes[lname] = lang_bytes.get(lname, 0) + edge["size"]

        repo_tools = tuple(sorted(extract_tools(node.get("rootTree")), key=str.lower))
        for tool in repo_tools:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1

        desc = (node.get("description") or "").strip()
        if not desc:
            desc = readme_tagline(node["name"])

        repos.append(
            Repo(
                name=node["name"],
                description=desc,
                fork=bool(node.get("isFork")),
                parent=(node.get("parent") or {}).get("nameWithOwner"),
                stars=int(node.get("stargazerCount") or 0),
                pushed_at=node["pushedAt"],
                langs=repo_langs,
                tools=repo_tools,
            )
        )

    langs_sorted = tuple(
        (k, lang_repos[k])
        for k in sorted(lang_repos, key=lambda k: (-lang_repos[k], -lang_bytes[k], k.lower()))
    )
    tools_sorted = tuple(sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0].lower())))
    repos_sorted = tuple(sorted(repos, key=lambda r: (r.fork, -_ts(r.pushed_at), r.name.lower())))

    return ProfileData(
        name=user_meta.get("name") or USER,
        bio=(user_meta.get("bio") or "").strip(),
        avatar_url=user_meta["avatarUrl"],
        followers=int(user_meta["followers"]["totalCount"]),
        stars=sum(r.stars for r in repos_sorted),
        contrib=int(cal["totalContributions"]),
        weeks=weeks[-52:],
        langs=langs_sorted,
        tools=tools_sorted,
        repos=repos_sorted,
    )


# ── fonts: subset + embed so the SVG renders the same everywhere ─────────
@functools.cache
def _font_bytes(filename: str) -> bytes:
    return (FONTS / filename).read_bytes()


def font_face(family: str, filename: str, text: str) -> str:
    opts = subset.Options(flavor="woff2", layout_features=["kern"])
    # recalcTimestamp=False: identical text gives byte-identical output, so the
    # hourly workflow only commits when something real changed
    font = TTFont(io.BytesIO(_font_bytes(filename)), recalcTimestamp=False)
    subsetter = subset.Subsetter(opts)
    subsetter.populate(text="".join(sorted(set(text + " "))))
    subsetter.subset(font)
    buf = io.BytesIO()
    font.flavor = "woff2"
    font.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return (
        f"@font-face{{font-family:'{family}';"
        f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}"
    )


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def _collect_font_chars(body: str) -> dict[str, set[str]]:
    root = ET.fromstring(f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>')
    used: dict[str, set[str]] = {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in {"text", "tspan"}:
            continue
        classes = (el.attrib.get("class") or "").split()
        font_cls = next((c for c in classes if c in FONT_SPECS), None)
        if font_cls is None:
            continue
        text_content = "".join(el.itertext()) if tag == "text" else (el.text or "")
        if text_content:
            used.setdefault(font_cls, set()).update(text_content)
    return used


def _render_svg(
    width: int,
    height: int,
    aria_label: str,
    title: str,
    body: str,
    *,
    extra_css: str = "",
) -> str:
    used_chars = _collect_font_chars(body)
    css_parts: list[str] = []
    for cls, (family, filename) in FONT_SPECS.items():
        chars = used_chars.get(cls)
        if not chars:
            continue
        css_parts.append(font_face(family, filename, "".join(sorted(chars))))
    for cls, (family, _) in FONT_SPECS.items():
        if cls in used_chars:
            css_parts.append(f".{cls}{{font-family:{family},monospace;fill:{INK}}}")
    if extra_css:
        css_parts.append(extra_css)
    style = "".join(css_parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(aria_label)}">\n'
        f"<title>{esc(title)}</title>\n"
        f"<style>{style}</style>\n"
        f"{body}\n"
        "</svg>\n"
    )


# ── portrait ─────────────────────────────────────────────────────────────
# The photo is shown as-is: the live GitHub avatar, or assets/portrait.* if
# present. Only a centre square crop and a resize to fit the frame are applied.
def _avatar_request_url(avatar_url: str) -> str:
    _validate_https(avatar_url)
    parts = urllib.parse.urlsplit(avatar_url)
    params = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if "s" not in params and "size" not in params:
        query = f"{parts.query}&s=760" if parts.query else "s=760"
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    return avatar_url


def _portrait_source(avatar_url: str) -> Image.Image:
    for ext in ("png", "jpg", "jpeg", "webp"):
        override = ASSETS / f"portrait.{ext}"
        if override.exists():
            with Image.open(override) as im:
                return im.convert("RGB")
    cache = ASSETS / "avatar.jpg"
    try:
        req = urllib.request.Request(
            _avatar_request_url(avatar_url),
            headers={"User-Agent": f"{USER}-readme"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        if not cache.exists() or cache.read_bytes() != raw:
            cache.write_bytes(raw)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        print(f"warning: avatar fetch failed ({err}); keeping cached copy", file=sys.stderr)
    if not cache.exists():
        raise RuntimeError(f"avatar unavailable and no cached copy at {cache}")
    with Image.open(cache) as im:
        return im.convert("RGB")


def portrait(avatar_url: str, size: int = 380, scale: int = 2) -> str:
    im = _portrait_source(avatar_url)
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    side = min(s, size * scale)
    if side != s:
        im = im.resize((side, side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── header card ──────────────────────────────────────────────────────────
def activity_rows(weeks: Sequence[int], cols: int, rows: int) -> list[str]:
    recent = list(weeks[-cols:])
    if len(recent) < cols:
        recent = [0] * (cols - len(recent)) + recent
    top = max(recent, default=0) or 1
    grid = [[" "] * cols for _ in range(rows)]
    for i, w in enumerate(recent):
        h = w / top * rows
        for r in range(rows):
            fill = h - (rows - 1 - r)
            if fill >= 1:
                grid[r][i] = "#"
            elif fill > 0.55:
                grid[r][i] = "+"
            elif fill > 0.2:
                grid[r][i] = "."
        if w and grid[rows - 1][i] == " ":
            grid[rows - 1][i] = "."
    return ["".join(g) for g in grid]


def card(data: ProfileData, *, now: datetime | None = None) -> str:
    W, H = 960, 640
    P = 24
    TOP = 72
    PH = 380                      # portrait side
    PX0, PX1 = P, P + PH
    RX0, RX1 = PX1, W - P
    BOT = TOP + PH                # 452
    FOOT = H - P - 34

    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    bio = data.bio or "MMMMMmmmmm..."
    if len(bio) > 26:
        bio = bio[:25] + "…"

    rows = [
        ("HANDLE", USER),
        ("ALIAS", data.name),
        ("ROLE", PROFILE["role"]),
        ("MODE", PROFILE["mode"]),
        ("STACK", " · ".join(k.upper() for k, _ in data.langs[:3])),
        ("READING", PROFILE["reading"]),
        ("SIGNAL", bio),
    ]
    stats = [
        ("REPOS", len(data.repos)),
        ("STARS", data.stars),
        ("CONTRIB/YR", data.contrib),
        ("FOLLOWERS", data.followers),
    ]

    act_cols, act_rows = 52, 5
    recent_weeks = data.weeks[-act_cols:]
    act = activity_rows(recent_weeks, act_cols, act_rows)
    peak_val = max(recent_weeks, default=0)
    pad = max(0, act_cols - len(recent_weeks))
    peak = (
        pad + max(range(len(recent_weeks)), key=recent_weeks.__getitem__)
        if peak_val > 0
        else -1
    )

    img = portrait(data.avatar_url)

    s: list[str] = []
    a = s.append
    a(f'<rect width="{W}" height="{H}" rx="14" fill="{PAPER}"/>')

    # print grain: sparse ink specks combined into a single path
    rng = random.Random(7)
    speck_d = "".join(
        f"M{rng.randint(4, W - 6)},{rng.randint(4, H - 6)}h1.6v1.6h-1.6z"
        for _ in range(90)
    )
    a(f'<path fill="{INK}" opacity=".18" d="{speck_d}"/>')

    # frame and corner registration squares
    a(f'<rect x="{P}" y="{P}" width="{W - 2 * P}" height="{H - 2 * P}" class="ln"/>')
    a(f'<line x1="{P}" y1="{TOP}" x2="{W - P}" y2="{TOP}" class="ln"/>')
    a(f'<line x1="{P}" y1="{BOT}" x2="{W - P}" y2="{BOT}" class="ln"/>')
    a(f'<line x1="{PX1}" y1="{TOP}" x2="{PX1}" y2="{BOT}" class="ln"/>')
    a(f'<line x1="{P}" y1="{FOOT}" x2="{W - P}" y2="{FOOT}" class="ln"/>')
    corners = [
        (P, P), (W - P, P), (P, TOP), (W - P, TOP), (PX1, TOP), (P, BOT),
        (PX1, BOT), (W - P, BOT), (P, FOOT), (W - P, FOOT), (P, H - P), (W - P, H - P),
    ]
    corner_d = "".join(f"M{x - 3.5},{y - 3.5}h7v7h-7z" for x, y in corners)
    a(f'<path fill="{INK}" d="{corner_d}"/>')

    # title bar
    a(
        f'<text x="{P + 20}" y="{P + 32}" class="x" font-size="22" '
        f'letter-spacing="1">SUBJECT // {esc(USER.upper())}</text>'
    )
    a(f'<rect x="{P + 286}" y="{P + 15}" width="12" height="12" fill="{PUMPKIN}"/>')
    a(
        f'<text x="{W - P - 92}" y="{P + 31}" class="b" font-size="13" '
        'text-anchor="end" letter-spacing="1.5">PUMPKIN.SYS</text>'
    )
    for i in range(3):
        fill = INK if i < 2 else "none"
        a(
            f'<rect x="{W - P - 76 + i * 20}" y="{P + 17}" width="12" height="12" '
            f'fill="{fill}" stroke="{INK}" stroke-width="1.5"/>'
        )

    # portrait
    a(
        f'<image x="{PX0}" y="{TOP}" width="{PH}" height="{PH}" '
        f'preserveAspectRatio="xMidYMid slice" href="data:image/jpeg;base64,{img}"/>'
    )
    a(f'<rect x="{PX0}" y="{TOP}" width="{PH}" height="{PH}" class="ln"/>')

    # data rows
    row_h = 38
    y0 = TOP
    for i, (k, v) in enumerate(rows):
        yb = y0 + (i + 1) * row_h
        yt = yb - 13
        a(
            f'<text x="{RX0 + 20}" y="{yt}" class="b soft" font-size="13" '
            f'letter-spacing="1.5">{esc(k)}</text>'
        )
        if k == "SIGNAL":
            a(
                f'<text x="{RX1 - 34}" y="{yt}" class="b" font-size="15" '
                f'text-anchor="end">{esc(v)}</text>'
            )
            a(
                f'<rect x="{RX1 - 28}" y="{yt - 13}" width="9" height="16" '
                f'fill="{PUMPKIN}" class="cur"/>'
            )
        else:
            a(
                f'<text x="{RX1 - 20}" y="{yt}" class="b" font-size="15" '
                f'text-anchor="end">{esc(v)}</text>'
            )
        if i < len(rows) - 1:
            a(f'<line x1="{RX0}" y1="{yb}" x2="{RX1}" y2="{yb}" class="hair"/>')

    # stats strip (bottom of right column)
    sy0 = y0 + len(rows) * row_h
    sh = BOT - sy0
    a(f'<rect x="{RX0}" y="{sy0}" width="{RX1 - RX0}" height="{sh}" fill="{INK}"/>')
    cw = (RX1 - RX0) / len(stats)
    for i, (k, v) in enumerate(stats):
        cx = RX0 + cw * i + 20
        val_str = str(v)
        val_size = 38 if len(val_str) < 5 else 32
        a(f'<text x="{cx}" y="{sy0 + 50}" class="x inv" font-size="{val_size}">{val_str}</text>')
        a(
            f'<text x="{cx}" y="{sy0 + 76}" class="b muted" font-size="11" '
            f'letter-spacing="1.5">{esc(k)}</text>'
        )
        if i:
            a(
                f'<line x1="{RX0 + cw * i}" y1="{sy0 + 18}" x2="{RX0 + cw * i}" '
                f'y2="{sy0 + sh - 18}" stroke="{PAPER}" stroke-width="1" '
                'stroke-dasharray="2 3" opacity=".5"/>'
            )

    # activity: ascii histogram, one column per week
    ax0, ax1 = P + 20, W - P - 20
    a(
        f'<text x="{ax0}" y="{BOT + 26}" class="b soft" font-size="12" '
        'letter-spacing="1.5">ACTIVITY // 52 WEEKS</text>'
    )
    a(
        f'<text x="{ax1}" y="{BOT + 26}" class="b soft" font-size="12" '
        f'letter-spacing="1.5" text-anchor="end">PEAK {peak_val} / WK</text>'
    )
    colw = (ax1 - ax0) / act_cols
    for r, line in enumerate(act):
        y = BOT + 50 + r * 14
        for c, ch in enumerate(line):
            if ch == " ":
                continue
            cls = "x acc" if c == peak else "x"
            a(
                f'<text x="{ax0 + c * colw + colw / 2:.1f}" y="{y}" '
                f'class="{cls}" font-size="15" text-anchor="middle">{ch}</text>'
            )
    a(
        f'<line x1="{ax0}" y1="{BOT + 50 + act_rows * 14 - 6}" '
        f'x2="{ax1}" y2="{BOT + 50 + act_rows * 14 - 6}" class="hair"/>'
    )

    # footer
    a(
        f'<text x="{P + 20}" y="{H - P - 12}" class="t" font-size="11.5" '
        f'letter-spacing="1.2">&gt; LAST SYNC {stamp} · AUTO-SYNCED HOURLY</text>'
    )
    a(
        f'<text x="{W - P - 20}" y="{H - P - 12}" class="t" font-size="11.5" '
        f'letter-spacing="1.2" text-anchor="end">github.com/{esc(USER)}</text>'
    )

    extra_css = (
        f".soft{{fill:{INK_SOFT}}}.inv{{fill:{PAPER}}}.muted{{fill:{PAPER};opacity:.75}}"
        f".acc{{fill:{PUMPKIN}}}.ln{{stroke:{INK};stroke-width:1.5;fill:none}}"
        f".hair{{stroke:{INK};stroke-width:1;stroke-dasharray:2 3;opacity:.55}}"
        "@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}"
        ".cur{animation:blink 1.1s steps(1) infinite}"
        "@media (prefers-reduced-motion:reduce){.cur{animation:none}}"
    )
    return _render_svg(
        W, H, f"{USER} profile card", f"{USER} · Pumpkin", "\n".join(s), extra_css=extra_css
    )


# ── project tiles ────────────────────────────────────────────────────────
def ago(iso: str, *, now: datetime | None = None) -> str:
    dt = datetime.fromisoformat(iso)
    days = ((now or datetime.now(UTC)) - dt).days
    if days <= 0:
        return "TODAY"
    if days < 30:
        return f"{days}D AGO"
    if days < 365:
        return f"{days // 30}MO AGO"
    return f"{days // 365}Y AGO"


def tile(repo: Repo, idx: int, *, now: datetime | None = None) -> str:
    W, H = 460, 168
    name = repo.name
    display_name = name if len(name) <= 34 else name[:33] + "…"
    name_size = 24 if len(display_name) <= 16 else (20 if len(display_name) <= 24 else 16)

    lines = textwrap.wrap(
        repo.description, width=46, break_long_words=True, break_on_hyphens=False
    )
    if len(lines) > 2:
        lines = lines[:2]
        lines[1] = lines[1][:43].rstrip(" .,;:") + "…"

    lang_parts = [lang.upper() for lang in repo.langs[:3]]
    while len(lang_parts) > 1 and len(" · ".join(lang_parts)) > 28:
        lang_parts.pop()
    lang = " · ".join(lang_parts) or "—"
    if len(lang) > 28:
        lang = lang[:27] + "…"

    upd = ago(repo.pushed_at, now=now)
    tag = (
        f"FORK OF {repo.parent}"
        if repo.fork and repo.parent
        else ("FORK" if repo.fork else "ORIGINAL")
    )
    if len(tag) > 50:
        tag = tag[:49] + "…"

    # dithered gradient stripe down the left edge, unique per tile, collapsed into one path
    stripe_d = "".join(
        f"M{10 + x},{10 + y}h4v4h-4z"
        for y in range(0, H - 20, 4)
        for x in range(0, 16, 4)
        if BAYER8[(y // 4 + idx) % 8][(x // 4) % 8] < (y / (H - 20)) * 64
    )

    tag_cls = "b acc" if tag == "ORIGINAL" else "b soft"
    s = [
        f'<rect width="{W}" height="{H}" rx="12" fill="{PAPER}"/>',
        (
            f'<rect x="10" y="10" width="{W - 20}" height="{H - 20}" '
            f'fill="none" stroke="{INK}" stroke-width="1.5"/>'
        ),
        f'<path fill="{INK}" d="{stripe_d}"/>',
        f'<line x1="26" y1="10" x2="26" y2="{H - 10}" stroke="{INK}" stroke-width="1.5"/>',
        (
            f'<text x="42" y="34" class="{tag_cls}" font-size="10.5" '
            f'letter-spacing="1.3">{esc(tag)}</text>'
        ),
        (
            f'<text x="42" y="60" class="x" font-size="{name_size}" '
            f'letter-spacing=".5">{esc(display_name)}</text>'
        ),
    ]
    for i, ln in enumerate(lines):
        s.append(f'<text x="42" y="{86 + i * 18}" class="t" font-size="13">{esc(ln)}</text>')
    s.append(
        f'<line x1="26" y1="{H - 44}" x2="{W - 10}" y2="{H - 44}" '
        f'stroke="{INK}" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/>'
    )
    s.append(
        f'<text x="42" y="{H - 21}" class="b" font-size="12" '
        f'letter-spacing="1.2">{esc(lang)}</text>'
    )
    s.append(
        f'<text x="{W - 26}" y="{H - 21}" class="b" font-size="12" '
        f'letter-spacing="1.2" text-anchor="end">★ {repo.stars}  ·  {upd}</text>'
    )
    extra_css = f".acc{{fill:{PUMPKIN}}}.soft{{fill:{INK_SOFT}}}"
    return _render_svg(W, H, name, name, "\n".join(s), extra_css=extra_css)


# ── panels: stack (discovered) and notes (owner's words) ────────────────
def _chips(
    items: Sequence[str],
    x0: float,
    y: float,
    x1: float,
    size: float = 12.5,
    gap: float = 8,
) -> tuple[list[str], float]:
    """Lay out inked chips left to right, wrapping. Returns (svg parts, bottom y)."""
    out: list[str] = []
    x = x0
    cw = size * 0.6
    h = size + 12
    for it in items:
        w = len(it) * cw + 18
        if x + w > x1 and x > x0:
            x = x0
            y += h + gap
        out.append(
            f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" '
            f'fill="none" stroke="{INK}" stroke-width="1.3"/>'
        )
        out.append(
            f'<text x="{x + 9:.1f}" y="{y + h - 8}" class="b" '
            f'font-size="{size}">{esc(it)}</text>'
        )
        x += w + gap
    return out, y + h


def stack_card(data: ProfileData) -> str:
    W = 960
    langs = data.langs
    tools = [t for t, _ in data.tools]
    top = max((n for _, n in langs), default=1)
    rows = len(langs)

    ty = 116 + rows * 26 + 8
    chips, chips_bot = _chips(tools, 34, ty + 24, W - 34) if tools else ([], 0.0)
    H = (round(chips_bot) + 29) if tools else (116 + rows * 26 + 12)

    s: list[str] = [
        f'<rect width="{W}" height="{H}" rx="12" fill="{PAPER}"/>',
        (
            f'<rect x="10" y="10" width="{W - 20}" height="{H - 20}" '
            f'fill="none" stroke="{INK}" stroke-width="1.5"/>'
        ),
        '<text x="34" y="46" class="x" font-size="20" letter-spacing="1">STACK //</text>',
        f'<rect x="146" y="32" width="11" height="11" fill="{PUMPKIN}"/>',
        (
            f'<text x="{W - 34}" y="45" class="b soft" font-size="11.5" '
            f'letter-spacing="1.3" text-anchor="end">'
            f'DETECTED FROM {len(data.repos)} PUBLIC REPOS</text>'
        ),
        f'<line x1="10" y1="64" x2="{W - 10}" y2="64" stroke="{INK}" stroke-width="1.5"/>',
        '<text x="34" y="90" class="b soft" font-size="11.5" letter-spacing="1.3">LANGUAGES</text>',
        (
            f'<text x="{W - 34}" y="90" class="b soft" font-size="11.5" '
            'letter-spacing="1.3" text-anchor="end">REPOS</text>'
        ),
    ]

    bx0, bx1 = 250, W - 90
    cells = 56
    cw = (bx1 - bx0) / cells
    bar_w = f"{cw - 2.5:.1f}"
    top_bar_d: list[str] = []
    ink_bar_d: list[str] = []
    dot_d: list[str] = []

    for i, (k, n) in enumerate(langs):
        y = 116 + i * 26
        s.append(f'<text x="34" y="{y}" class="b" font-size="13.5">{esc(k)}</text>')
        filled = max(1, round(n / top * cells))
        target_bar = top_bar_d if i == 0 else ink_bar_d
        for c in range(cells):
            if c < filled:
                target_bar.append(f"M{bx0 + c * cw:.1f},{y - 11}h{bar_w}v12h-{bar_w}z")
            elif c % 2 == 0:
                dot_d.append(f"M{bx0 + c * cw + cw / 2 - 1.5:.1f},{y - 6.5}h2.5v2.5h-2.5z")
        s.append(
            f'<text x="{W - 34}" y="{y}" class="x" font-size="13.5" '
            f'text-anchor="end">{n}</text>'
        )

    if top_bar_d:
        s.append(f'<path fill="{PUMPKIN}" d="{"".join(top_bar_d)}"/>')
    if ink_bar_d:
        s.append(f'<path fill="{INK}" d="{"".join(ink_bar_d)}"/>')
    if dot_d:
        s.append(f'<path fill="{INK}" opacity=".35" d="{"".join(dot_d)}"/>')

    if tools:
        s.append(
            f'<line x1="34" y1="{ty - 14}" x2="{W - 34}" y2="{ty - 14}" '
            f'stroke="{INK}" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/>'
        )
        s.append(
            f'<text x="34" y="{ty + 12}" class="b soft" font-size="11.5" '
            'letter-spacing="1.3">TOOLS</text>'
        )
        s.extend(chips)

    return _render_svg(
        W, H, "Stack", "Stack", "\n".join(s), extra_css=f".soft{{fill:{INK_SOFT}}}"
    )


def notes_card() -> str:
    W = 960
    mid = W // 2
    cols: list[tuple[str, list[tuple[str, list[str]]]]] = [("NOW", NOW), ("OFFLINE", OFFLINE)]

    col_svg: list[str] = []
    max_y = 86
    for ci, (title, group) in enumerate(cols):
        x0 = 34 + ci * mid
        col_svg.append(
            f'<text x="{x0}" y="46" class="x" font-size="20" '
            f'letter-spacing="1">{title} //</text>'
        )
        y = 86
        bullet_fill = PUMPKIN if ci == 0 else INK
        for k, vals in group:
            col_svg.append(
                f'<rect x="{x0}" y="{y - 10}" width="8" height="8" fill="{bullet_fill}"/>'
            )
            col_svg.append(
                f'<text x="{x0 + 18}" y="{y}" class="b" font-size="13" '
                f'letter-spacing="1.3">{esc(k)}</text>'
            )
            y += 30
            if vals:
                wrapped = textwrap.wrap(
                    " · ".join(vals), width=52, break_long_words=True, break_on_hyphens=False
                )
                for li, ln in enumerate(wrapped):
                    col_svg.append(
                        f'<text x="{x0 + 18}" y="{y - 6}" class="t" '
                        f'font-size="13.5">{esc(ln)}</text>'
                    )
                    y += 30 if li == len(wrapped) - 1 else 22
        max_y = max(max_y, y)

    H = max_y + 58
    s = [
        f'<rect width="{W}" height="{H}" rx="12" fill="{PAPER}"/>',
        (
            f'<rect x="10" y="10" width="{W - 20}" height="{H - 20}" '
            f'fill="none" stroke="{INK}" stroke-width="1.5"/>'
        ),
        f'<line x1="{mid}" y1="10" x2="{mid}" y2="{H - 56}" stroke="{INK}" stroke-width="1.5"/>',
        (
            f'<line x1="10" y1="{H - 56}" x2="{W - 10}" y2="{H - 56}" '
            f'stroke="{INK}" stroke-width="1.5"/>'
        ),
        *col_svg,
        (
            f'<text x="34" y="{H - 26}" class="t" font-size="13.5">'
            f'&gt; {esc(PROFILE["tagline"])}</text>'
        ),
    ]
    return _render_svg(W, H, "Now and offline", "Now and offline", "\n".join(s))


def _notes_summary(group: Sequence[tuple[str, Sequence[str]]]) -> str:
    parts: list[str] = []
    for k, vals in group:
        label = k.lower()
        if vals:
            joined = ", ".join(v.lower() for v in vals)
            parts.append(f"{label} {joined}" if label.endswith("about") else f"{label} ({joined})")
        else:
            parts.append(label)
    return "; ".join(parts)


def readme(data: ProfileData) -> str:
    cells: list[str] = []
    for repo in data.repos:
        n = repo.name
        q = urllib.parse.quote(n, safe="")
        cells.append(
            f'<td width="50%"><a href="https://github.com/{USER}/{q}">'
            f'<img src="assets/projects/{q}.svg" width="100%" alt="{esc(n)}"></a></td>'
        )
    rows: list[str] = []
    for i in range(0, len(cells), 2):
        pair = cells[i:i + 2]
        if len(pair) == 1:
            pair.append('<td width="50%"></td>')
        rows.append("  <tr>\n    " + "\n    ".join(pair) + "\n  </tr>")

    own = sum(1 for r in data.repos if not r.fork)
    forks = len(data.repos) - own
    fork_word = "fork" if forks == 1 else "forks"
    langs_summary = ", ".join(k for k, _ in data.langs)
    card_alt = (
        f"{USER}: {PROFILE['role'].lower()}. {PROFILE['tagline']} "
        "Repos, stars, yearly contributions and a 52-week activity chart."
    )
    stack_alt = f"Languages and tools detected from public repositories: {langs_summary}."
    notes_alt = f"Now: {_notes_summary(NOW)}. Offline: {_notes_summary(OFFLINE)}."
    footer_note = (
        'Everything above is generated by <a href="scripts/build.py">'
        "<code>scripts/build.py</code></a> from live GitHub data and refreshed every hour, "
        "so new repositories and forks show up on their own. Font: JetBrains Mono (OFL)."
    )

    return f"""<!-- generated by scripts/build.py; edit PROFILE / NOW / OFFLINE there, not here -->
<a href="https://github.com/{USER}">
  <img src="assets/card.svg" width="100%" alt="{esc(card_alt)}">
</a>

<img src="assets/stack.svg" width="100%" alt="{esc(stack_alt)}">

<img src="assets/notes.svg" width="100%" alt="{esc(notes_alt)}">

### Repositories

<sub>{own} original · {forks} {fork_word} · originals first, newest push first</sub>

<table>
{chr(10).join(rows)}
</table>

<sub>{footer_note}</sub>
"""


def _safe_repo_name(name: str) -> str:
    if not name or name in {".", ".."} or not _SAFE_REPO_RE.fullmatch(name):
        raise ValueError(f"unsafe repository name for tile path: {name!r}")
    return name


def main() -> None:
    now = datetime.now(UTC)
    data = fetch()
    (ASSETS / "card.svg").write_text(card(data, now=now), encoding="utf-8")
    (ASSETS / "stack.svg").write_text(stack_card(data), encoding="utf-8")
    (ASSETS / "notes.svg").write_text(notes_card(), encoding="utf-8")

    pdir = ASSETS / "projects"
    pdir.mkdir(exist_ok=True)
    keep: set[str] = set()
    for i, repo in enumerate(data.repos):
        safe_name = _safe_repo_name(repo.name)
        f = pdir / f"{safe_name}.svg"
        f.write_text(tile(repo, i, now=now), encoding="utf-8")
        keep.add(f.name)
    for f in pdir.glob("*.svg"):
        if f.name not in keep:
            f.unlink()  # repo deleted or made private

    (ROOT / "README.md").write_text(readme(data), encoding="utf-8")
    print("ok", len(data.repos), "repos", [k for k, _ in data.langs], [t for t, _ in data.tools])


if __name__ == "__main__":
    main()
