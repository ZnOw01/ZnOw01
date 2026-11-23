from __future__ import annotations

import base64
import functools
import io
import re
import sys
import urllib.error
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402

_FONT_FACE_RE = re.compile(
    r"@font-face\{font-family:'([^']+)';"
    r"src:url\(data:font/woff2;base64,([^)]+)\) format\('woff2'\);\}"
)


def _sample_repo(
    name: str = "Ryliox",
    *,
    description: str = "Your O'Reilly Learning library, yours to keep.",
    fork: bool = False,
    parent: str | None = None,
    stars: int = 4,
    pushed_at: str = "2026-11-23T10:00:00Z",
    langs: tuple[str, ...] = ("Python", "TypeScript", "CSS"),
    tools: tuple[str, ...] = ("uv", "Ruff"),
) -> build.Repo:
    return build.Repo(
        name=name,
        description=description,
        fork=fork,
        parent=parent,
        stars=stars,
        pushed_at=pushed_at,
        langs=langs,
        tools=tools,
    )


def _sample_data(
    *,
    weeks: tuple[int, ...] = (0, 2, 10, 40, 77),
    followers: int = 7,
    stars: int = 11,
    contrib: int = 365,
    langs: tuple[tuple[str, int], ...] = (("Python", 5), ("C++", 4), ("Kotlin", 3)),
    tools: tuple[tuple[str, int], ...] = (("GitHub Actions", 4), ("CMake", 2), ("Ruff", 2)),
    repos: tuple[build.Repo, ...] | None = None,
) -> build.ProfileData:
    if repos is None:
        repos = (
            _sample_repo(),
            _sample_repo(
                "SnowBooks",
                description="Readest ebook reader fork.",
                fork=True,
                parent="readest/readest",
                stars=0,
                langs=("TypeScript", "Rust", "Lua"),
            ),
        )
    return build.ProfileData(
        name="Pumpkin",
        bio="CS & compilers",
        avatar_url="https://avatars.githubusercontent.com/u/1?v=4",
        followers=followers,
        stars=stars,
        contrib=contrib,
        weeks=weeks,
        langs=langs,
        tools=tools,
        repos=repos,
    )


def _extract_embedded_cmaps(svg_text: str) -> dict[str, set[int]]:
    cmaps: dict[str, set[int]] = {}
    for family, b64 in _FONT_FACE_RE.findall(svg_text):
        font = TTFont(io.BytesIO(base64.b64decode(b64)))
        cmap = font.getBestCmap()
        assert cmap is not None
        cmaps[family] = set(cmap.keys())
    return cmaps


@functools.cache
def _source_cmap(filename: str) -> set[int]:
    cmap = TTFont(build.FONTS / filename).getBestCmap()
    assert cmap is not None
    return set(cmap.keys())


def _assert_svg_valid_and_glyphs_covered(svg_text: str) -> ET.Element:
    root = ET.fromstring(svg_text)
    cmaps = _extract_embedded_cmaps(svg_text)
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "text":
            continue
        classes = (el.attrib.get("class") or "").split()
        font_cls = next((c for c in classes if c in build.FONT_SPECS), None)
        assert font_cls is not None, f"text element missing font class: {el.attrib}"
        family, filename = build.FONT_SPECS[font_cls]
        assert family in cmaps, f"missing @font-face for {family}"
        source_cmap = _source_cmap(filename)
        for ch in "".join(el.itertext()):
            if ord(ch) in source_cmap:
                assert ord(ch) in cmaps[family], (
                    f"glyph {ch!r} (U+{ord(ch):04X}) missing in {family} subset"
                )
    return root


def test_activity_rows_aligns_right_and_handles_zero_and_empty() -> None:
    empty = build.activity_rows((), 52, 5)
    assert empty == [" " * 52] * 5

    zeros = build.activity_rows([0, 0, 0], 10, 4)
    assert zeros == [" " * 10] * 4

    short = build.activity_rows([1, 10], 5, 3)
    assert len(short) == 3
    assert all(len(row) == 5 for row in short)
    assert [row[:3] for row in short] == ["   ", "   ", "   "]
    assert short[2][3] == "."
    assert [row[4] for row in short] == ["#", "#", "#"]


def test_ago_formats_today_future_days_months_and_years() -> None:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert build.ago("2026-11-23T12:00:00Z", now=now) == "TODAY"
    assert build.ago("2026-11-24T12:00:00Z", now=now) == "TODAY"
    assert build.ago("2026-11-10T12:00:00Z", now=now) == "13D AGO"
    assert build.ago("2026-09-01T12:00:00Z", now=now) == "2MO AGO"
    assert build.ago("2025-10-01T12:00:00Z", now=now) == "1Y AGO"


def test_extract_readme_tagline_skips_non_prose_and_strips_markdown() -> None:
    md = """<!--
    multi-line html comment
    should be ignored entirely
    -->
# Heading
[![badge](https://img.shields.io/badge/x-y-blue)](https://example.com)
- bullet item ignored
* another bullet ignored

```python
print("inside code fence should be ignored")
```

A **fast** `CLI` [utility](https://example.com) for local builds.<br/>
"""
    assert build._extract_readme_tagline(md) == "A fast CLI utility for local builds."


def test_extract_tools_respects_depth_and_markers() -> None:
    tree = {
        "entries": [
            {"name": "Dockerfile", "object": {}},
            {
                "name": ".github",
                "object": {
                    "entries": [
                        {"name": "workflows"},
                        {"name": "ISSUE_TEMPLATE"},
                    ]
                },
            },
            {
                "name": "backend",
                "object": {
                    "entries": [
                        {"name": "pyproject.toml"},
                        {"name": "ruff.toml"},
                    ]
                },
            },
            {"name": "README.md", "object": {}},
        ]
    }
    assert build.extract_tools(tree) == {"Docker", "GitHub Actions", "Ruff"}
    assert build.extract_tools(None) == set()


def test_safe_repo_name_accepts_valid_and_rejects_traversal() -> None:
    assert build._safe_repo_name("watermarks_removerFk") == "watermarks_removerFk"
    assert build._safe_repo_name("my.repo-1") == "my.repo-1"
    for bad in ("", ".", "..", "../secret", "dir/repo", "repo name"):
        with pytest.raises(ValueError):
            build._safe_repo_name(bad)


def test_card_renders_valid_svg_and_covers_all_glyphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build, "portrait", lambda *_args, **_kwargs: "AAAA")
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    data = _sample_data()

    svg1 = build.card(data, now=now)
    svg2 = build.card(data, now=now)
    assert svg1 == svg2
    assert svg1.endswith("\n")
    _assert_svg_valid_and_glyphs_covered(svg1)


def test_card_handles_empty_weeks_and_five_digit_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build, "portrait", lambda *_args, **_kwargs: "AAAA")
    data = _sample_data(weeks=(), contrib=12345, stars=99999)
    svg = build.card(data, now=datetime(2026, 11, 23, 12, 0, tzinfo=UTC))
    root = _assert_svg_valid_and_glyphs_covered(svg)
    sizes = {
        "".join(el.itertext()): el.attrib.get("font-size")
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "text"
    }
    assert sizes["12345"] == "32"
    assert sizes["99999"] == "32"


def test_stack_card_omits_unused_regular_font_and_handles_empty_tools() -> None:
    data = _sample_data()
    svg = build.stack_card(data)
    _assert_svg_valid_and_glyphs_covered(svg)
    cmaps = _extract_embedded_cmaps(svg)
    assert "JBM" not in cmaps
    assert set(cmaps) == {"JBMB", "JBMX"}

    no_tools_svg = build.stack_card(_sample_data(tools=()))
    _assert_svg_valid_and_glyphs_covered(no_tools_svg)
    assert "TOOLS" not in no_tools_svg


def test_tile_handles_escaping_long_tags_names_and_languages() -> None:
    repo = _sample_repo(
        name="super-long-repository-name-that-keeps-going-and-going",
        description="Checks XML <escaping> & 'quotes' plus unbrokenwordthatmightexceedwidthlimits.",
        fork=True,
        parent="very-long-organization-name/very-long-upstream-repository-name",
        langs=("TypeScript", "JavaScript", "C++"),
    )
    svg = build.tile(repo, 0, now=datetime(2026, 11, 23, 12, 0, tzinfo=UTC))
    root = _assert_svg_valid_and_glyphs_covered(svg)
    texts = [
        "".join(el.itertext())
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "text"
    ]
    assert len(texts[0]) <= 50
    assert texts[0].endswith("…")
    assert len(texts[1]) <= 34
    assert len(texts[-2]) <= 28


def test_notes_card_wraps_long_items_without_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build,
        "NOW",
        [("READING", ["A" * 30, "B" * 30, "C" * 30])],
    )
    svg = build.notes_card()
    root = _assert_svg_valid_and_glyphs_covered(svg)
    assert int(root.attrib["height"]) > 200


def test_readme_singular_plural_forks_and_escaping() -> None:
    one_fork = _sample_data(
        repos=(
            _sample_repo("Alpha"),
            _sample_repo("Beta&1", fork=True, parent="a/b"),
        )
    )
    out_one = build.readme(one_fork)
    assert "1 original · 1 fork · originals first" in out_one
    assert "Beta&amp;1" in out_one
    assert "Beta%261.svg" in out_one

    two_forks = _sample_data(
        repos=(
            _sample_repo("Alpha"),
            _sample_repo("Fork1", fork=True, parent="a/b"),
            _sample_repo("Fork2", fork=True, parent="a/c"),
        )
    )
    assert "1 original · 2 forks · originals first" in build.readme(two_forks)


def test_portrait_source_falls_back_to_cached_avatar_on_network_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    cache = assets_dir / "avatar.jpg"
    img = build.Image.new("RGB", (64, 64), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    original_bytes = buf.getvalue()
    cache.write_bytes(original_bytes)

    def fail_urlopen(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(build, "ASSETS", assets_dir)
    monkeypatch.setattr(build.urllib.request, "urlopen", fail_urlopen)

    loaded = build._portrait_source("https://avatars.githubusercontent.com/u/1?v=4")
    assert loaded.size == (64, 64)
    assert cache.read_bytes() == original_bytes
