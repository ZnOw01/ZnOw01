#!/usr/bin/env python3
"""Generate the profile README cards from live GitHub data.

Outputs:
  assets/card.svg            dossier header (dithered + ASCII portrait, stats, activity)
  assets/projects/<repo>.svg one tile per featured project

Needs GITHUB_TOKEN (or GH_TOKEN) in the environment. Pillow + fonttools[woff].
"""
from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONTS = ASSETS / "fonts"
USER = "ZnOw01"

# ── palette ──────────────────────────────────────────────────────────────
PAPER = "#B7D8CD"   # mint paper
PANEL = "#A6CCBF"   # recessed panel
INK = "#121A18"     # print ink
INK_SOFT = "#3E5852"
PUMPKIN = "#E4572E"  # the one accent

# Everything below is the owner's own words. Repos, languages and tools are
# NOT listed here: they are discovered from the GitHub API on every run.
PROFILE = {
    "role": "CS STUDENT",
    "mode": "BUILD · BREAK · LEARN",
    "reading": "CODE · LANGUAGES · TOOLS · LINUX",
    "tagline": "Always building things, breaking things and learning a bit more every day.",
}
NOW = [
    ("BUILDING", ["New projects"]),
    ("READING ABOUT", ["Programming", "Programming languages", "Tools", "Linux"]),
]
OFFLINE = [
    ("VIDEO GAMES", []),
    ("BOOKS", ["Programming", "Classic literature"]),
    ("LEARNING", ["English"]),
]

# file or directory name (at depth <= 2 of a repo) -> tool it reveals
TOOL_MARKERS = {
    "Dockerfile": "Docker", "docker-compose.yml": "Docker", "compose.yaml": "Docker",
    "uv.lock": "uv", "requirements.txt": "pip",
    "bun.lock": "Bun", "bun.lockb": "Bun", "package-lock.json": "npm", "pnpm-lock.yaml": "pnpm",
    "CMakeLists.txt": "CMake", "meson.build": "Meson", "Makefile": "Make", "Cargo.toml": "Cargo",
    "go.mod": "Go modules", "flake.nix": "Nix", "vcpkg.json": "vcpkg",
    "astro.config.mjs": "Astro", "astro.config.ts": "Astro", "vite.config.ts": "Vite",
    "vitest.config.ts": "Vitest", "vitest.config.mts": "Vitest", "tailwind.config.js": "Tailwind",
    "components.json": "shadcn/ui", "conftest.py": "pytest", "pytest.ini": "pytest",
    ".github/workflows": "GitHub Actions", ".pre-commit-config.yaml": "pre-commit",
    "ruff.toml": "Ruff", ".prettierrc": "Prettier", "eslint.config.js": "ESLint",
    "Doxyfile": "Doxygen", ".clang-format": "clang-format", "manifest.json": None,
}

BAYER8 = [
    [0, 48, 12, 60, 3, 51, 15, 63],
    [32, 16, 44, 28, 35, 19, 47, 31],
    [8, 56, 4, 52, 11, 59, 7, 55],
    [40, 24, 36, 20, 43, 27, 39, 23],
    [2, 50, 14, 62, 1, 49, 13, 61],
    [34, 18, 46, 30, 33, 17, 45, 29],
    [10, 58, 6, 54, 9, 57, 5, 53],
    [42, 26, 38, 22, 41, 25, 37, 21],
]
RAMP = " .:-=+*#%@"


# ── data ─────────────────────────────────────────────────────────────────
def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        raise SystemExit("set GITHUB_TOKEN or GH_TOKEN")
    return tok


def _req(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-readme",
    })
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.load(resp)


def fetch() -> dict:
    user = _req(f"https://api.github.com/users/{USER}")
    repos = _req(f"https://api.github.com/users/{USER}/repos?per_page=100")
    q = """query($login:String!){user(login:$login){contributionsCollection{
      contributionCalendar{totalContributions weeks{contributionDays{contributionCount}}}}}}"""
    gql = _req("https://api.github.com/graphql", {"query": q, "variables": {"login": USER}})
    cal = gql["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    weeks = [sum(d["contributionCount"] for d in w["contributionDays"]) for w in cal["weeks"]]
    repos = [r for r in repos if r["name"] != USER and not r.get("private")]
    lang_repos: dict[str, int] = {}
    lang_bytes: dict[str, int] = {}
    tools: dict[str, int] = {}
    for r in repos:
        full = _req(f"https://api.github.com/repos/{USER}/{r['name']}")
        r["parent"] = (full.get("parent") or {}).get("full_name")
        lb = _req(f"https://api.github.com/repos/{USER}/{r['name']}/languages")
        total = sum(lb.values()) or 1
        # a language counts for a repo when it is at least 2% of its code
        r["langs"] = [k for k, v in sorted(lb.items(), key=lambda kv: -kv[1]) if v / total >= 0.02]
        for k in r["langs"]:
            lang_repos[k] = lang_repos.get(k, 0) + 1
            lang_bytes[k] = lang_bytes.get(k, 0) + lb[k]
        r["tools"] = sorted(repo_tools(r["name"], r["default_branch"]))
        for t in r["tools"]:
            tools[t] = tools.get(t, 0) + 1
        if not r.get("description"):
            r["description"] = readme_tagline(r["name"])
    langs = sorted(lang_repos, key=lambda k: (-lang_repos[k], -lang_bytes[k]))
    return {
        "name": user.get("name") or USER,
        "bio": (user.get("bio") or "").strip(),
        "avatar": user["avatar_url"],
        "repos": user["public_repos"],
        "followers": user["followers"],
        "stars": sum(r["stargazers_count"] for r in repos),
        "contrib": cal["totalContributions"],
        "weeks": weeks[-52:],
        "langs": [(k, lang_repos[k]) for k in langs],
        "tools": sorted(tools.items(), key=lambda kv: (-kv[1], kv[0].lower())),
        "repos_list": sorted(repos, key=lambda r: (bool(r.get("fork")), -_ts(r["pushed_at"]))),
    }


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def repo_tools(name: str, branch: str) -> set[str]:
    try:
        tree = _req(f"https://api.github.com/repos/{USER}/{name}/git/trees/{branch}?recursive=1")
    except Exception:
        return set()
    found = set()
    for e in tree.get("tree", []):
        path = e["path"]
        parts = path.split("/")
        if len(parts) > 2:
            continue
        for key in (path, parts[-1]):
            tool = TOOL_MARKERS.get(key)
            if tool:
                found.add(tool)
    return found


def readme_tagline(name: str) -> str:
    """First plain sentence of a repo README, used only when it has no description."""
    try:
        raw = _req(f"https://api.github.com/repos/{USER}/{name}/readme")
        text = base64.b64decode(raw["content"]).decode("utf-8", "replace")
    except Exception:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "<", "[", "!", "|", "```", ">", "-", "*  ")):
            continue
        s = re.sub(r"[*_`]|<br\s*/?>", "", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s).strip()
        if len(s) > 12:
            return s
    return ""


# ── fonts: subset + embed so the SVG renders the same everywhere ─────────
def font_face(family: str, file: str, text: str) -> str:
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["kern"]
    f = TTFont(FONTS / file)
    s = subset.Subsetter(opts)
    s.populate(text=text + " ")
    s.subset(f)
    buf = io.BytesIO()
    f.flavor = "woff2"
    f.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"@font-face{{font-family:'{family}';src:url(data:font/woff2;base64,{b64}) format('woff2');}}"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&#39;")


# ── portrait: Bayer dither that dissolves into ASCII ─────────────────────
def portrait(avatar_url: str, size: int = 380, scale: int = 2) -> str:
    cache = ASSETS / "avatar.jpg"
    try:
        with urllib.request.urlopen(avatar_url + "&s=460", timeout=30) as r:
            cache.write_bytes(r.read())
    except Exception:
        pass  # keep the cached copy
    src = Image.open(cache).convert("L")
    w, h = src.size
    # tighter crop on the face (avatar subject sits centre-right)
    src = src.crop((int(w * 0.08), int(h * 0.04), int(w * 0.96), int(h * 0.92)))
    src = ImageOps.autocontrast(src, cutoff=1)
    # lift midtones so skin reads as paper and only hair/lines carry ink
    lut = [int(255 * ((i / 255) ** 0.5)) for i in range(256)]
    src = src.point(lut)
    src = src.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))

    W = size * scale
    ink = (0x12, 0x1A, 0x18)
    paper = (0xB7, 0xD8, 0xCD)
    out = Image.new("RGB", (W, W), paper)
    d = ImageDraw.Draw(out)

    px_cell = 3 * scale            # dither pixel
    n = W // px_cell
    dith = src.resize((n, n), Image.LANCZOS).load()

    ch_cell = 10 * scale           # ascii cell
    m = W // ch_cell
    asc = src.resize((m, m), Image.LANCZOS).load()
    font = ImageFont.truetype(str(FONTS / "JetBrainsMono-Bold.ttf"), int(ch_cell * 1.12))

    # ragged boundary: each ascii row starts at a slightly different column
    rng = random.Random(USER)
    split = []
    # ascii eats the hair on the left; the face stays in dither
    base = int(m * 0.26)
    for r in range(m):
        base += rng.choice((-1, 0, 0, 1))
        base = max(int(m * 0.18), min(int(m * 0.32), base))
        split.append(base)

    for y in range(n):
        for x in range(n):
            ar = min(m - 1, (y * px_cell) // ch_cell)
            if (x * px_cell) // ch_cell < split[ar]:
                continue
            t = (BAYER8[y % 8][x % 8] + 0.5) / 64 * 255
            if dith[x, y] < t:
                d.rectangle([x * px_cell, y * px_cell, (x + 1) * px_cell - 1, (y + 1) * px_cell - 1], fill=ink)

    for r in range(m):
        for c in range(0, split[r]):
            v = 1 - asc[c, r] / 255          # darkness 0..1
            ch = RAMP[min(len(RAMP) - 1, int(v * len(RAMP)))]
            if ch == " ":
                continue
            cx, cy = c * ch_cell + ch_cell / 2, r * ch_cell + ch_cell / 2
            d.text((cx, cy), ch, font=font, fill=ink, anchor="mm")
        # a pixel or two of debris across the seam, like a half-finished scan
        for _ in range(rng.randint(0, 2)):
            c = split[r] + rng.randint(0, 3)
            if c < m:
                d.rectangle([c * ch_cell + scale, r * ch_cell + scale, c * ch_cell + 3 * scale, r * ch_cell + 3 * scale], fill=ink)

    out = out.quantize(colors=4)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


# ── header card ──────────────────────────────────────────────────────────
def activity_rows(weeks: list[int], cols: int, rows: int) -> list[str]:
    top = max(weeks) or 1
    grid = [[" "] * cols for _ in range(rows)]
    for i, w in enumerate(weeks[-cols:]):
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


def card(data: dict) -> str:
    W, H = 960, 640
    P = 24
    TOP = 72
    PH = 380                      # portrait side
    PX0, PX1 = P, P + PH
    RX0, RX1 = PX1, W - P
    BOT = TOP + PH                # 452

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bio = data["bio"] or "MMMMMmmmmm..."
    if len(bio) > 26:
        bio = bio[:25] + "…"

    rows = [
        ("HANDLE", USER),
        ("ALIAS", data["name"]),
        ("ROLE", PROFILE["role"]),
        ("MODE", PROFILE["mode"]),
        ("STACK", " · ".join(k.upper() for k, _ in data["langs"][:3])),
        ("READING", PROFILE["reading"]),
        ("SIGNAL", bio),
    ]
    stats = [
        ("REPOS", data["repos"]),
        ("STARS", data["stars"]),
        ("CONTRIB/YR", data["contrib"]),
        ("FOLLOWERS", data["followers"]),
    ]

    act_cols, act_rows = 52, 5
    act = activity_rows(data["weeks"], act_cols, act_rows)
    peak = max(range(len(data["weeks"][-act_cols:])), key=lambda i: data["weeks"][-act_cols:][i])

    text_all = "".join(a + b for a, b in rows) + "".join(k + str(v) for k, v in stats)
    text_all += "".join(act) + now + bio + "SUBJECT//ZNOW01PUMPKIN.SYSACTIVITY52WEEKSPEAKLASTSYNCREGENERATEDDAILYAUTOSYNCEDHOURLY·[]_>0123456789/:-"
    style = (
        font_face("JBM", "JetBrainsMono-Regular.ttf", text_all)
        + font_face("JBMB", "JetBrainsMono-Bold.ttf", text_all)
        + font_face("JBMX", "JetBrainsMono-ExtraBold.ttf", text_all)
    )

    img = portrait(data["avatar"])

    s = []
    a = s.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{USER} profile card">')
    a(f"<title>{USER} · Pumpkin</title>")
    a("<style>" + style + f"""
      .t{{font-family:JBM,monospace;fill:{INK}}}
      .b{{font-family:JBMB,monospace;fill:{INK}}}
      .x{{font-family:JBMX,monospace;fill:{INK}}}
      .soft{{fill:{INK_SOFT}}}
      .ln{{stroke:{INK};stroke-width:1.5;fill:none}}
      .hair{{stroke:{INK};stroke-width:1;stroke-dasharray:2 3;opacity:.55}}
      @keyframes scan{{0%{{transform:translateY(0)}}100%{{transform:translateY({PH - 6}px)}}}}
      .scan{{animation:scan 5.5s cubic-bezier(.6,0,.4,1) infinite alternate}}
      @keyframes blink{{0%,49%{{opacity:1}}50%,100%{{opacity:0}}}}
      .cur{{animation:blink 1.1s steps(1) infinite}}
      @media (prefers-reduced-motion:reduce){{.scan,.cur{{animation:none}}}}
    </style>""")

    a(f'<rect width="{W}" height="{H}" rx="14" fill="{PAPER}"/>')
    # print grain: sparse ink specks
    rng = random.Random(7)
    specks = "".join(f'<rect x="{rng.randint(4, W - 6)}" y="{rng.randint(4, H - 6)}" width="1.6" height="1.6"/>' for _ in range(90))
    a(f'<g fill="{INK}" opacity=".18">{specks}</g>')

    # frame
    a(f'<rect x="{P}" y="{P}" width="{W - 2 * P}" height="{H - 2 * P}" class="ln"/>')
    a(f'<line x1="{P}" y1="{TOP}" x2="{W - P}" y2="{TOP}" class="ln"/>')
    a(f'<line x1="{P}" y1="{BOT}" x2="{W - P}" y2="{BOT}" class="ln"/>')
    a(f'<line x1="{PX1}" y1="{TOP}" x2="{PX1}" y2="{BOT}" class="ln"/>')
    FOOT = H - P - 34
    a(f'<line x1="{P}" y1="{FOOT}" x2="{W - P}" y2="{FOOT}" class="ln"/>')
    for x, y in [(P, P), (W - P, P), (P, TOP), (W - P, TOP), (PX1, TOP), (P, BOT), (PX1, BOT),
                 (W - P, BOT), (P, FOOT), (W - P, FOOT), (P, H - P), (W - P, H - P)]:
        a(f'<rect x="{x - 3.5}" y="{y - 3.5}" width="7" height="7" fill="{INK}"/>')

    # title bar
    a(f'<text x="{P + 20}" y="{P + 32}" class="x" font-size="22" letter-spacing="1">SUBJECT // {USER.upper()}</text>')
    a(f'<rect x="{P + 286}" y="{P + 15}" width="12" height="12" fill="{PUMPKIN}"/>')
    a(f'<text x="{W - P - 92}" y="{P + 31}" class="b" font-size="13" text-anchor="end" letter-spacing="1.5">PUMPKIN.SYS</text>')
    for i in range(3):
        fill = INK if i < 2 else "none"
        a(f'<rect x="{W - P - 76 + i * 20}" y="{P + 17}" width="12" height="12" fill="{fill}" stroke="{INK}" stroke-width="1.5"/>')

    # portrait
    a(f'<image x="{PX0}" y="{TOP}" width="{PH}" height="{PH}" preserveAspectRatio="none" style="image-rendering:pixelated" href="data:image/png;base64,{img}"/>')
    a(f'<g class="scan"><rect x="{PX0}" y="{TOP + 2}" width="{PH}" height="3" fill="{PUMPKIN}" opacity=".85"/></g>')
    a(f'<rect x="{PX0}" y="{TOP}" width="{PH}" height="{PH}" class="ln"/>')
    # corner brackets
    for (cx, cy, dx, dy) in [(PX0 + 12, TOP + 12, 1, 1), (PX1 - 12, TOP + 12, -1, 1), (PX0 + 12, BOT - 12, 1, -1), (PX1 - 12, BOT - 12, -1, -1)]:
        a(f'<path d="M{cx} {cy + 18 * dy}V{cy}H{cx + 18 * dx}" stroke="{PAPER}" stroke-width="5" fill="none"/>')
        a(f'<path d="M{cx} {cy + 18 * dy}V{cy}H{cx + 18 * dx}" class="ln" stroke-width="2"/>')

    # data rows
    row_h = 38
    y0 = TOP
    for i, (k, v) in enumerate(rows):
        yb = y0 + (i + 1) * row_h
        yt = yb - 13
        a(f'<text x="{RX0 + 20}" y="{yt}" class="b soft" font-size="13" letter-spacing="1.5">{esc(k)}</text>')
        if k == "SIGNAL":
            a(f'<text x="{RX1 - 34}" y="{yt}" class="b" font-size="15" text-anchor="end">{esc(v)}</text>')
            a(f'<rect x="{RX1 - 28}" y="{yt - 13}" width="9" height="16" fill="{PUMPKIN}" class="cur"/>')
        else:
            a(f'<text x="{RX1 - 20}" y="{yt}" class="b" font-size="15" text-anchor="end">{esc(v)}</text>')
        a(f'<line x1="{RX0}" y1="{yb}" x2="{RX1}" y2="{yb}" class="hair"/>')

    # stats strip (bottom of right column)
    sy0 = y0 + len(rows) * row_h
    sh = BOT - sy0
    a(f'<rect x="{RX0}" y="{sy0}" width="{RX1 - RX0}" height="{sh}" fill="{INK}"/>')
    cw = (RX1 - RX0) / len(stats)
    for i, (k, v) in enumerate(stats):
        cx = RX0 + cw * i + 20
        a(f'<text x="{cx}" y="{sy0 + 50}" class="x" font-size="38" fill="{PAPER}" style="fill:{PAPER}">{v}</text>')
        a(f'<text x="{cx}" y="{sy0 + 76}" class="b" font-size="11" letter-spacing="1.5" style="fill:{PAPER};opacity:.75">{esc(k)}</text>')
        if i:
            a(f'<line x1="{RX0 + cw * i}" y1="{sy0 + 18}" x2="{RX0 + cw * i}" y2="{sy0 + sh - 18}" stroke="{PAPER}" stroke-width="1" stroke-dasharray="2 3" opacity=".5"/>')

    # activity: ascii histogram, one column per week
    ax0, ax1 = P + 20, W - P - 20
    a(f'<text x="{ax0}" y="{BOT + 26}" class="b soft" font-size="12" letter-spacing="1.5">ACTIVITY // 52 WEEKS</text>')
    a(f'<text x="{ax1}" y="{BOT + 26}" class="b soft" font-size="12" letter-spacing="1.5" text-anchor="end">PEAK {max(data["weeks"])} / WK</text>')
    colw = (ax1 - ax0) / act_cols
    for r, line in enumerate(act):
        y = BOT + 50 + r * 14
        for c, ch in enumerate(line):
            if ch == " ":
                continue
            fill = PUMPKIN if c == peak else INK
            a(f'<text x="{ax0 + c * colw + colw / 2:.1f}" y="{y}" class="x" font-size="15" text-anchor="middle" style="fill:{fill}">{ch}</text>')
    # baseline
    a(f'<line x1="{ax0}" y1="{BOT + 50 + act_rows * 14 - 6}" x2="{ax1}" y2="{BOT + 50 + act_rows * 14 - 6}" class="hair"/>')

    # footer
    a(f'<text x="{P + 20}" y="{H - P - 12}" class="t" font-size="11.5" letter-spacing="1.2">&gt; LAST SYNC {now} · AUTO-SYNCED HOURLY</text>')
    a(f'<text x="{W - P - 20}" y="{H - P - 12}" class="t" font-size="11.5" letter-spacing="1.2" text-anchor="end">github.com/{USER}</text>')
    a("</svg>")
    return "\n".join(s)


# ── project tiles ────────────────────────────────────────────────────────
def wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for w in text.split():
        if len(line) + len(w) + (1 if line else 0) > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}" if line else w
    if line:
        out.append(line)
    return out


def ago(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "TODAY"
    if days < 30:
        return f"{days}D AGO"
    return f"{days // 30}MO AGO"


def tile(repo: dict, idx: int) -> str:
    W, H = 460, 168
    name = repo["name"]
    desc = repo.get("description") or ""
    lines = wrap(desc, 46)
    if len(lines) > 2:
        lines = lines[:2]
        lines[1] = lines[1][:43].rstrip(" .,;:") + "…"
    lang = " · ".join(l.upper() for l in repo.get("langs", [])[:3]) or "—"
    stars = repo.get("stargazers_count", 0)
    upd = ago(repo["pushed_at"])
    tag = f"FORK OF {repo['parent']}" if repo.get("fork") and repo.get("parent") else ("FORK" if repo.get("fork") else "ORIGINAL")
    if len(tag) > 56:
        tag = tag[:55] + "…"
    text = name.upper() + name + desc + lang + str(stars) + upd + tag + "★·/0123456789…—"
    style = (font_face("JBM", "JetBrainsMono-Regular.ttf", text)
             + font_face("JBMB", "JetBrainsMono-Bold.ttf", text)
             + font_face("JBMX", "JetBrainsMono-ExtraBold.ttf", text))
    # dithered gradient stripe down the left edge, unique per tile
    stripe = []
    for y in range(0, H - 20, 4):
        for x in range(0, 16, 4):
            v = (y / (H - 20)) * 64
            if BAYER8[(y // 4 + idx) % 8][(x // 4) % 8] < v:
                stripe.append(f'<rect x="{10 + x}" y="{10 + y}" width="4" height="4"/>')
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(name)}">',
         f"<title>{esc(name)}</title>",
         "<style>" + style + f".t{{font-family:JBM,monospace;fill:{INK}}}.b{{font-family:JBMB,monospace;fill:{INK}}}.x{{font-family:JBMX,monospace;fill:{INK}}}</style>",
         f'<rect width="{W}" height="{H}" rx="12" fill="{PAPER}"/>',
         f'<rect x="10" y="10" width="{W - 20}" height="{H - 20}" fill="none" stroke="{INK}" stroke-width="1.5"/>',
         f'<g fill="{INK}">{"".join(stripe)}</g>',
         f'<line x1="26" y1="10" x2="26" y2="{H - 10}" stroke="{INK}" stroke-width="1.5"/>',
         f'<text x="42" y="34" class="b" font-size="10.5" letter-spacing="1.3" style="fill:{PUMPKIN if tag == "ORIGINAL" else INK_SOFT}">{esc(tag)}</text>',
         f'<text x="42" y="60" class="x" font-size="{24 if len(name) <= 16 else 20}" letter-spacing=".5">{esc(name)}</text>']
    for i, ln in enumerate(lines):
        s.append(f'<text x="42" y="{86 + i * 18}" class="t" font-size="13">{esc(ln)}</text>')
    s.append(f'<line x1="26" y1="{H - 44}" x2="{W - 10}" y2="{H - 44}" stroke="{INK}" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/>')
    s.append(f'<text x="42" y="{H - 21}" class="b" font-size="12" letter-spacing="1.2">{esc(lang)}</text>')
    s.append(f'<text x="{W - 26}" y="{H - 21}" class="b" font-size="12" letter-spacing="1.2" text-anchor="end">★ {stars}  ·  {upd}</text>')
    s.append("</svg>")
    return "\n".join(s)


# ── panels: stack (discovered) and notes (owner's words) ────────────────
def _svg_open(W: int, H: int, label: str, text: str) -> list[str]:
    style = (font_face("JBM", "JetBrainsMono-Regular.ttf", text)
             + font_face("JBMB", "JetBrainsMono-Bold.ttf", text)
             + font_face("JBMX", "JetBrainsMono-ExtraBold.ttf", text))
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}">',
            f"<title>{esc(label)}</title>",
            "<style>" + style + f".t{{font-family:JBM,monospace;fill:{INK}}}.b{{font-family:JBMB,monospace;fill:{INK}}}.x{{font-family:JBMX,monospace;fill:{INK}}}.soft{{fill:{INK_SOFT}}}</style>",
            f'<rect width="{W}" height="{H}" rx="12" fill="{PAPER}"/>',
            f'<rect x="10" y="10" width="{W - 20}" height="{H - 20}" fill="none" stroke="{INK}" stroke-width="1.5"/>']


def _chips(items: list[str], x0: float, y: float, x1: float, size: float = 12.5, gap: float = 8):
    """Lay out inked chips left to right, wrapping. Returns (svg parts, bottom y)."""
    out, x = [], x0
    cw = size * 0.6
    h = size + 12
    for it in items:
        w = len(it) * cw + 18
        if x + w > x1:
            x = x0
            y += h + gap
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" fill="none" stroke="{INK}" stroke-width="1.3"/>')
        out.append(f'<text x="{x + 9:.1f}" y="{y + h - 8}" class="b" font-size="{size}">{esc(it)}</text>')
        x += w + gap
    return out, y + h


def stack_card(data: dict) -> str:
    W = 960
    langs = data["langs"]
    tools = [t for t, _ in data["tools"]]
    top = max((n for _, n in langs), default=1)
    rows = len(langs)
    text = "STACK//LANGUAGESTOOLSDETECTEDFROMPUBLICREPOSREPOSOF0123456789·" + "".join(k for k, _ in langs) + "".join(tools)
    # tools block height estimate
    tool_lines = 1
    x = 0
    for t in tools:
        w = len(t) * 7.5 + 26
        if x + w > W - 68:
            tool_lines += 1
            x = 0
        x += w
    H = 116 + rows * 26 + 32 + tool_lines * 33 + 20
    s = _svg_open(W, H, "Stack", text)
    s.append(f'<text x="34" y="46" class="x" font-size="20" letter-spacing="1">STACK //</text>')
    s.append(f'<rect x="146" y="32" width="11" height="11" fill="{PUMPKIN}"/>')
    s.append(f'<text x="{W - 34}" y="45" class="b soft" font-size="11.5" letter-spacing="1.3" text-anchor="end">DETECTED FROM {len(data["repos_list"])} PUBLIC REPOS</text>')
    s.append(f'<line x1="10" y1="64" x2="{W - 10}" y2="64" stroke="{INK}" stroke-width="1.5"/>')
    s.append(f'<text x="34" y="90" class="b soft" font-size="11.5" letter-spacing="1.3">LANGUAGES</text>')
    s.append(f'<text x="{W - 34}" y="90" class="b soft" font-size="11.5" letter-spacing="1.3" text-anchor="end">REPOS</text>')
    bx0, bx1 = 250, W - 90
    for i, (k, n) in enumerate(langs):
        y = 116 + i * 26
        s.append(f'<text x="34" y="{y}" class="b" font-size="13.5">{esc(k)}</text>')
        cells = 56
        filled = max(1, round(n / top * cells))
        cw = (bx1 - bx0) / cells
        for c in range(cells):
            if c < filled:
                s.append(f'<rect x="{bx0 + c * cw:.1f}" y="{y - 11}" width="{cw - 2.5:.1f}" height="12" fill="{PUMPKIN if i == 0 else INK}"/>')
            elif c % 2 == 0:
                s.append(f'<rect x="{bx0 + c * cw + cw / 2 - 1.5:.1f}" y="{y - 6.5}" width="2.5" height="2.5" fill="{INK}" opacity=".35"/>')
        s.append(f'<text x="{W - 34}" y="{y}" class="x" font-size="13.5" text-anchor="end">{n}</text>')
    ty = 116 + rows * 26 + 8
    s.append(f'<line x1="34" y1="{ty - 14}" x2="{W - 34}" y2="{ty - 14}" stroke="{INK}" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/>')
    s.append(f'<text x="34" y="{ty + 12}" class="b soft" font-size="11.5" letter-spacing="1.3">TOOLS</text>')
    chips, _ = _chips(tools, 34, ty + 24, W - 34)
    s += chips
    s.append("</svg>")
    return "\n".join(s)


def notes_card() -> str:
    W = 960
    cols = [("NOW", NOW), ("OFFLINE", OFFLINE)]
    text = "NOW//OFFLINE//" + "".join(k + "".join(v) for _, g in cols for k, v in g) + PROFILE["tagline"] + "·>"
    rows_h = max(sum(1 + (1 if v else 0) for _, v in g) for _, g in cols)
    H = 74 + rows_h * 30 + 70
    s = _svg_open(W, H, "Now and offline", text)
    mid = W / 2
    s.append(f'<line x1="{mid}" y1="10" x2="{mid}" y2="{H - 56}" stroke="{INK}" stroke-width="1.5"/>')
    s.append(f'<line x1="10" y1="{H - 56}" x2="{W - 10}" y2="{H - 56}" stroke="{INK}" stroke-width="1.5"/>')
    for ci, (title, group) in enumerate(cols):
        x0 = 34 + ci * mid
        s.append(f'<text x="{x0}" y="46" class="x" font-size="20" letter-spacing="1">{title} //</text>')
        y = 86
        for k, vals in group:
            s.append(f'<rect x="{x0}" y="{y - 10}" width="8" height="8" fill="{PUMPKIN if ci == 0 else INK}"/>')
            s.append(f'<text x="{x0 + 18}" y="{y}" class="b" font-size="13" letter-spacing="1.3">{esc(k)}</text>')
            y += 30
            if vals:
                s.append(f'<text x="{x0 + 18}" y="{y - 6}" class="t" font-size="13.5">{esc(" · ".join(vals))}</text>')
                y += 30
    s.append(f'<text x="34" y="{H - 26}" class="t" font-size="13.5">&gt; {esc(PROFILE["tagline"])}</text>')
    s.append("</svg>")
    return "\n".join(s)


def readme(data: dict) -> str:
    cells = []
    for r in data["repos_list"]:
        n = r["name"]
        cells.append(f'<td width="50%"><a href="https://github.com/{USER}/{n}"><img src="assets/projects/{n}.svg" width="100%" alt="{esc(n)}"></a></td>')
    rows = []
    for i in range(0, len(cells), 2):
        pair = cells[i:i + 2]
        if len(pair) == 1:
            pair.append('<td width="50%"></td>')
        rows.append("  <tr>\n    " + "\n    ".join(pair) + "\n  </tr>")
    own = sum(1 for r in data["repos_list"] if not r.get("fork"))
    forks = len(data["repos_list"]) - own
    return f"""<!-- generated by scripts/build.py; edit PROFILE / NOW / OFFLINE there, not here -->
<a href="https://github.com/{USER}">
  <img src="assets/card.svg" width="100%" alt="{USER}: CS student. Always building things, breaking things and learning. Repos, stars, yearly contributions and a 52-week activity chart.">
</a>

<img src="assets/stack.svg" width="100%" alt="Languages and tools detected from public repositories: {esc(', '.join(k for k, _ in data['langs']))}.">

<img src="assets/notes.svg" width="100%" alt="Now: building new projects; reading about programming, programming languages, tools and Linux. Offline: video games, books (programming, classic literature), learning English.">

### Repositories

<sub>{own} original · {forks} forks · originals first, newest push first</sub>

<table>
{chr(10).join(rows)}
</table>

<sub>Everything above is generated by <a href="scripts/build.py"><code>scripts/build.py</code></a> from live GitHub data and refreshed every hour, so new repositories and forks show up on their own. Font: JetBrains Mono (OFL).</sub>
"""


def main() -> None:
    data = fetch()
    (ASSETS / "card.svg").write_text(card(data))
    (ASSETS / "stack.svg").write_text(stack_card(data))
    (ASSETS / "notes.svg").write_text(notes_card())
    pdir = ASSETS / "projects"
    pdir.mkdir(exist_ok=True)
    keep = set()
    for i, repo in enumerate(data["repos_list"]):
        f = pdir / f"{repo['name']}.svg"
        f.write_text(tile(repo, i))
        keep.add(f.name)
    for f in pdir.glob("*.svg"):
        if f.name not in keep:
            f.unlink()  # repo deleted or made private
    (ROOT / "README.md").write_text(readme(data))
    print("ok", len(data["repos_list"]), "repos", [k for k, _ in data["langs"]], [t for t, _ in data["tools"]])


if __name__ == "__main__":
    main()
