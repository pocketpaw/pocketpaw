# tests/ee/agent/test_studio_motion_skill.py — the bundled studio-motion skill.
# The /studio/editor profile loads it, its frontmatter and links resolve, and
# every example composition in references/presets.md passes the timeline's own
# validator and keeps the house contract, so the docs cannot drift from it.

from __future__ import annotations

import re

import pytest
from pocketpaw_ee.agent.mcp_servers.timeline import validate_motion_graphic
from pocketpaw_ee.cloud.surface import service
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta

from pocketpaw.bundled_skills.installer import _SKILLS_DIR
from pocketpaw.skills.loader import parse_skill_md

SKILL_DIR = _SKILLS_DIR / "studio-motion"
PRESETS = ("title", "statement", "logo", "stat", "chart")
STYLES = ("swiss-pulse", "velvet", "maximalist", "data-drift", "soft-signal", "shadow-cut")


def _examples() -> list[str]:
    text = (SKILL_DIR / "references" / "presets.md").read_text(encoding="utf-8")
    return re.findall(r"```html\n(.*?)```", text, re.DOTALL)


def _root_tag(html: str) -> str:
    match = re.search(r"<div\b[^>]*\bdata-composition-id=[^>]*>", html)
    assert match, "no root element"
    return match.group(0)


def test_the_studio_editor_profile_loads_both_motion_skills() -> None:
    profile = service.resolve_profile(SurfaceKind.STUDIO_EDITOR, SurfaceMeta())
    assert {"studio-motion", "hyperframes-core"} <= set(profile.skill_names or ())


def test_the_skill_frontmatter_names_its_directory() -> None:
    skill = parse_skill_md(SKILL_DIR / "SKILL.md")
    assert skill is not None
    assert skill.name == SKILL_DIR.name
    assert skill.description.strip()


def test_every_linked_reference_exists() -> None:
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    links = set(re.findall(r"\]\((references/[^)#]+)\)", body))
    assert "references/presets.md" in links
    for link in links:
        assert (SKILL_DIR / link).is_file(), link


def test_there_is_one_example_per_preset() -> None:
    presets = [re.search(r'data-preset="([^"]+)"', html).group(1) for html in _examples()]
    assert sorted(presets) == sorted(PRESETS)


@pytest.mark.parametrize("html", _examples(), ids=PRESETS)
def test_each_example_passes_the_timeline_validator(html: str) -> None:
    shape, error = validate_motion_graphic(html, None)
    assert error is None, error
    assert shape["width"] == 1920 and shape["height"] == 1080


@pytest.mark.parametrize("html", _examples(), ids=PRESETS)
def test_each_example_keeps_the_house_contract(html: str) -> None:
    root = _root_tag(html)
    assert 'data-composition-id="mg"' in root
    assert 'data-start="0"' in root
    assert re.search(r'data-style="([^"]+)"', root).group(1) in STYLES
    for var in ("bg", "fg", "accent", "muted", "font-display", "font-body", "display-weight"):
        assert f"--mg-{var}:" in root, var

    clips = re.findall(r'<section class="clip" data-start="0" data-duration="([^"]+)"', html)
    duration = re.search(r'data-duration="([^"]+)"', root).group(1)
    assert clips == [duration]
    assert f"const D={duration};" in html

    assert html.count("gsap.timeline({paused:true") == 1
    assert 'window.__timelines["mg"]=tl;' in html
    assert "<canvas" not in html
    assert "Math.random" not in html and "Date.now" not in html
    assert len(html) < 20_000
