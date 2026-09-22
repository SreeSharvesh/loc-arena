"""The public docs site under ``site/`` exists, is self-consistent, and leaks no internal doc.

Pure file checks (no docker, no network): every page exists and is non-trivial HTML, each carries its
expected headings and keywords, every internal link resolves, every page is reachable from the home page,
no em or en dash appears anywhere under ``site/``, and no internal design doc or prompt path is referenced.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

SITE: Path = Path(__file__).resolve().parents[2] / "site"

# Framework pages plus the shipped setting (Meridian AI Lab and its Aurora efficiency push).
PAGES: tuple[str, ...] = (
    "index.html",
    "architecture.html",
    "settings.html",
    "meridian.html",
    "meridian-aurora.html",
    "monitoring.html",
    "extend.html",
)

# The unicode en dash (U+2013) and em dash (U+2014). Built with chr() so this file has neither literally.
EN_DASH: str = chr(0x2013)
EM_DASH: str = chr(0x2014)

# Internal design docs and prompt paths the PUBLIC site must never reference (it replaces them).
FORBIDDEN_REFS: tuple[str, ...] = (
    "prompts/",
    "DECISIONS.md",
    "SETTING_SPEC",
    "CLAUDE.md",
    "_PROGRESS",
    "COMPANY_CODEBASE_BLUEPRINT",
    "ENVIRONMENT_AND_TASKS",
    "AUDITING_AND_RUNNING",
    "IMPLEMENTATION_PLAN",
)

# Keywords each page must carry (matched case-insensitively against the rendered text).
PAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "index.html": ("rogue", "two measurement", "honest twin"),
    "architecture.html": ("services", "sealed", "model call"),
    "settings.html": ("what a setting defines", "meridian"),
    "meridian.html": ("meridian", "the team", "platform"),
    "meridian-aurora.html": ("aurora", "main task", "side task", "rogue"),
    "monitoring.html": ("tap point", "monitor", "caught"),
    "extend.html": ("run an episode", "repository"),
}

_LINK_RE = re.compile(r'href="([^"#?]+\.html)(?:\?[^"#]*)?(?:#[^"]*)?"')


def _read(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


def _site_files() -> list[Path]:
    return [p for p in SITE.rglob("*") if p.is_file()]


def _links(name: str) -> set[str]:
    return set(_LINK_RE.findall(_read(name)))


def test_pages_exist_and_are_nontrivial_html() -> None:
    for name in PAGES:
        page = SITE / name
        assert page.is_file(), f"missing page: {name}"
        text = page.read_text(encoding="utf-8")
        assert len(text) > 1500, f"page too small to be real: {name}"
        low = text.lower()
        assert "<!doctype html>" in low
        assert "<nav" in low and "</html>" in low
        assert "<title>" in low and "<h1" in low


def test_shared_stylesheet_exists() -> None:
    assert (SITE / "style.css").is_file()
    for name in PAGES:
        assert 'href="style.css' in _read(name)


def test_each_page_has_its_keywords() -> None:
    for name, keywords in PAGE_KEYWORDS.items():
        low = _read(name).lower()
        for kw in keywords:
            assert kw.lower() in low, f"{name} is missing keyword: {kw!r}"


def test_every_internal_link_resolves() -> None:
    for name in PAGES:
        assert "<nav" in _read(name).lower(), f"{name} has no nav"
        for target in _links(name):
            assert (SITE / target).is_file(), f"{name} links to a missing file: {target}"


def test_every_page_is_reachable_from_home() -> None:
    # Follow links breadth-first from index.html; a hub page (settings) may be the only route to a page.
    seen: set[str] = {"index.html"}
    queue: deque[str] = deque(["index.html"])
    while queue:
        for target in _links(queue.popleft()):
            if target not in seen and (SITE / target).is_file():
                seen.add(target)
                queue.append(target)
    for name in PAGES:
        assert name in seen, f"{name} is not reachable by following links from index.html"


def test_no_em_or_en_dash_anywhere_under_site() -> None:
    for path in _site_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert EN_DASH not in text, f"en dash found in {path.name}"
        assert EM_DASH not in text, f"em dash found in {path.name}"


def test_site_references_no_internal_doc_or_prompt() -> None:
    for path in _site_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for token in FORBIDDEN_REFS:
            assert token not in text, f"internal reference {token!r} found in {path.name}"
