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

FEATURED = {
    "Primrose": "Experimental Nintendo Switch 2 emulator. Fork of Eglantine.",
    "Pomegranate": "Experimental Nintendo Switch 2 emulator. Fork of Lichii.",
    "Ryliox": "Your O'Reilly Learning library, yours to keep. Export any book to EPUB 3 or PDF.",
    "Veilleuse": "Native Omarchy bar widget for brightness, night light and day/night schedules.",
    "nupFZ": "NUP: a clean-room container format for unencrypted NX (Switch 1/2) game dumps.",
    "PS5Flake": "PlayStation 5 emulator for Windows, Linux and macOS.",
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
    langs: dict[str, int] = {}
    for r in repos:
        if r.get("language"):
            langs[r["language"]] = langs.get(r["language"], 0) + 1
    return {
        "name": user.get("name") or USER,
        "bio": (user.get("bio") or "").strip(),
        "avatar": user["avatar_url"],
        "repos": user["public_repos"],
        "followers": user["followers"],
        "stars": sum(r["stargazers_count"] for r in repos),
        "contrib": cal["totalContributions"],
        "weeks": weeks[-52:],
        "langs": [k for k, _ in sorted(langs.items(), key=lambda kv: -kv[1])][:3],
        "repo_map": {r["name"]: r for r in repos},
    }


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
        ("FUNCTION", "EMULATORS / TOOLING"),
        ("CURRENT", "SWITCH 2 EMULATION"),
        ("STACK", " · ".join(l.upper() for l in data["langs"]) or "C++"),
        ("HABITAT", "ARCH · OMARCHY"),
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
    text_all += "".join(act) + now + bio + "SUBJECT//ZNOW01PUMPKIN.SYSACTIVITY52WEEKSPEAKLASTSYNCREGENERATEDDAILY·[]_>0123456789/:-"
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
    a(f'<text x="{P + 20}" y="{H - P - 12}" class="t" font-size="11.5" letter-spacing="1.2">&gt; LAST SYNC {now} · REGENERATED DAILY</text>')
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


def tile(name: str, desc: str, repo: dict, idx: int) -> str:
    W, H = 460, 168
    lines = wrap(desc, 46)[:2]
    lang = (repo.get("language") or "SPEC").upper()
    stars = repo.get("stargazers_count", 0)
    upd = ago(repo["pushed_at"])
    tag = "FORK" if repo.get("fork") else "ORIGINAL"
    text = name.upper() + name + desc + lang + str(stars) + upd + tag + "★·/0123456789FILE"
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
         f'<text x="42" y="46" class="x" font-size="24" letter-spacing=".5">{esc(name)}</text>',
         f'<text x="{W - 26}" y="44" class="b" font-size="11" letter-spacing="1.5" text-anchor="end" style="fill:{PUMPKIN if tag == "ORIGINAL" else INK_SOFT}">{tag}</text>']
    for i, ln in enumerate(lines):
        s.append(f'<text x="42" y="{76 + i * 19}" class="t" font-size="13.5">{esc(ln)}</text>')
    s.append(f'<line x1="26" y1="{H - 44}" x2="{W - 10}" y2="{H - 44}" stroke="{INK}" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/>')
    s.append(f'<text x="42" y="{H - 21}" class="b" font-size="12" letter-spacing="1.2">{esc(lang)}</text>')
    s.append(f'<text x="{W - 26}" y="{H - 21}" class="b" font-size="12" letter-spacing="1.2" text-anchor="end">★ {stars}  ·  {upd}</text>')
    s.append("</svg>")
    return "\n".join(s)


def main() -> None:
    data = fetch()
    (ASSETS / "card.svg").write_text(card(data))
    pdir = ASSETS / "projects"
    pdir.mkdir(exist_ok=True)
    for i, (name, desc) in enumerate(FEATURED.items()):
        repo = data["repo_map"].get(name)
        if repo:
            (pdir / f"{name}.svg").write_text(tile(name, desc, repo, i))
    print("ok", data["repos"], data["stars"], data["contrib"])


if __name__ == "__main__":
    main()
