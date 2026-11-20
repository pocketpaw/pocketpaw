# tests/ee/agent/test_studio_motion_direction_skill.py — the direction and effects
# skills for /studio/editor motion graphics.
#
# The editor profile loads both; every reference SKILL.md links is a resource the
# cloud backend actually serves; every world names a real file; and every full
# example passes the timeline's own validator, keeps the render contract, and
# differs from its siblings, so the skills cannot drift back to one look.

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pocketpaw_ee.agent.mcp_servers.timeline import validate_motion_graphic
from pocketpaw_ee.cloud.surface import service
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta

from pocketpaw.agents.pydantic_ai import PydanticAIBackend
from pocketpaw.bundled_skills.installer import _SKILLS_DIR
from pocketpaw.config import Settings
from pocketpaw.skills.loader import parse_skill_md

DIRECTION = _SKILLS_DIR / "studio-motion-direction"
EFFECTS = _SKILLS_DIR / "studio-motion-effects"
WORLDS = ("calm-demo", "one-take", "beat-cut", "editorial-collage", "launch-beats", "data-morph")
NEW_SKILLS = (DIRECTION, EFFECTS)
MOTION_SKILLS = ("studio-motion", "hyperframes-core", *(d.name for d in NEW_SKILLS))


def _examples(path: Path) -> list[str]:
    return re.findall(r"```html\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)


def _world_examples() -> list[tuple[str, str]]:
    return [
        (world, html)
        for world in WORLDS
        for html in _examples(DIRECTION / "references" / f"world-{world}.md")
    ]


def _effect_examples() -> list[str]:
    return _examples(EFFECTS / "references" / "recipes.md")


def _root(html: str) -> str:
    match = re.search(r"<div\b[^>]*\bdata-composition-id=[^>]*>", html)
    assert match, "no root element"
    return match.group(0)


def test_the_editor_profile_loads_the_new_skills() -> None:
    profile = service.resolve_profile(SurfaceKind.STUDIO_EDITOR, SurfaceMeta())
    assert {d.name for d in NEW_SKILLS} <= set(profile.skill_names or ())


@pytest.mark.parametrize("skill_dir", NEW_SKILLS, ids=lambda d: d.name)
def test_frontmatter_names_its_directory_and_a_license_ships(skill_dir: Path) -> None:
    skill = parse_skill_md(skill_dir / "SKILL.md")
    assert skill is not None and skill.name == skill_dir.name
    assert skill.description.strip()
    assert "MIT License" in (skill_dir / "LICENSE").read_text(encoding="utf-8")
    assert len((skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines()) < 500


def test_every_world_in_the_table_has_its_file() -> None:
    body = (DIRECTION / "SKILL.md").read_text(encoding="utf-8")
    for world in WORLDS:
        assert f"`{world}`" in body, world
        assert f"(references/world-{world}.md)" in body, world


@pytest.mark.parametrize("skill", MOTION_SKILLS)
def test_every_linked_reference_is_served_on_the_cloud_backend(skill: str) -> None:
    """A link the pydantic_ai backend cannot serve is a dead link for the agent."""
    pytest.importorskip("pydantic_ai_skills")
    backend = PydanticAIBackend(Settings(pydantic_ai_skills_enabled=True))
    capability = backend._build_skills_capability(frozenset({skill}))
    assert capability is not None
    (loaded,) = capability.skills
    served = {r.name for r in loaded.resources}
    body = (_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    links = set(re.findall(r"[(`](references/[\w./-]+\.md)[)`]", body))
    assert links, f"{skill} links no references"
    assert links <= served, links - served


def test_each_world_has_exactly_one_full_example() -> None:
    worlds = [w for w, _ in _world_examples()]
    assert sorted(worlds) == sorted(WORLDS)


def test_effects_ship_three_full_examples() -> None:
    assert len(_effect_examples()) == 3


def _contract(html: str, world: str) -> None:
    shape, error = validate_motion_graphic(html, None)
    assert error is None, error
    root = _root(html)
    assert 'data-composition-id="mg"' in root
    assert f'data-world="{world}"' in root
    for var in ("bg", "fg", "accent", "muted", "font-display", "font-body", "display-weight"):
        assert f"--mg-{var}:" in root, var
    duration = re.search(r'data-duration="([^"]+)"', root).group(1)
    assert re.findall(r'<section class="clip" data-start="0" data-duration="([^"]+)"', html) == [
        duration
    ]
    assert re.search(rf"const D\s*=\s*{re.escape(duration)}\s*[;,]", html)
    assert html.count("gsap.timeline({paused:true") == 1
    assert 'window.__timelines["mg"]=tl' in html.replace(" ", "")
    compact = html.replace(" ", "")
    assert "<canvas" not in html
    for banned in ("Math.random", "Date.now", "performance.now", "repeat:-1"):
        assert banned not in compact, banned
    assert len(html) < 20_000
    fonts = re.findall(r"fonts\.googleapis\.com/css2\?[^\"']+", html)
    assert len(fonts) <= 1
    assert sum(f.count("family=") for f in fonts) <= 2


@pytest.mark.parametrize(("world", "html"), _world_examples(), ids=lambda v: str(v)[:20])
def test_each_world_example_keeps_the_contract(world: str, html: str) -> None:
    _contract(html, world)


@pytest.mark.parametrize("html", _effect_examples())
def test_each_effects_example_keeps_the_contract(html: str) -> None:
    _contract(html, "effects")


def test_the_examples_do_not_share_one_look() -> None:
    """The bug this skill exists for: everything came out the same."""
    looks = [_root(html) for _, html in _world_examples()] + [_root(h) for h in _effect_examples()]
    backgrounds = [re.search(r"--mg-bg:\s*([^;\"]+)", r).group(1).strip().lower() for r in looks]
    fonts = [re.search(r"--mg-font-display:\s*([^;\"]+)", r).group(1).strip() for r in looks]
    assert len(set(backgrounds)) >= len(looks) - 1, backgrounds
    assert len(set(fonts)) >= len(looks) - 2, fonts


def test_the_house_palettes_stay_with_the_presets() -> None:
    house = {"#f4f4f0", "#14110f", "#ffde00", "#05060f", "#fbf3ec", "#000000"}
    for _, html in _world_examples():
        bg = re.search(r"--mg-bg:\s*([^;\"]+)", _root(html)).group(1).strip().lower()
        assert bg not in house, bg
