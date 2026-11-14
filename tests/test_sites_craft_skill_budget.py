# tests/test_sites_craft_skill_budget.py: bounds the byte size of the sites-craft
# skill, the construction method the /sites agent loads before it writes tokens
# on create and before a restyle on refine. Same shape as
# test_sites_check_skill_budget.py; the mutations in
# tests/mutations/skill_budget.json trip the ceiling and the fixture.
"""``sites-craft/SKILL.md`` stays under a stated byte ceiling.

WHY IT IS MEASURED. Create names this skill in ``<design-skills>`` and refine
embeds its floor slice, so the full file is loaded on most turns that touch the
look. It is the canonical owner of construction values (the press scale, the
easing default, the ramps), which makes it the natural place for every other
skill's numbers to drift into.

WHY 24,000. The design doc sets the budget at 24,000 bytes. The file landed
near 23.7 KB with browser-surface theming, the accent ink pair, heading spacing
and shared form heights added, paid for by trimming explanation elsewhere.
Raising the ceiling is allowed and should be argued for in the commit body.

WHAT IT DOES NOT COVER. Only this skill. ``wc -c`` units on the whole file,
frontmatter included, so the assertion is slightly stricter than the runtime
cost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

# The ceiling, in bytes, for the craft skill. See the module docstring.
SITES_CRAFT_MAX_BYTES = 24_000

# A read that returns far less than this is a broken path, not a lean skill.
_IMPLAUSIBLY_SMALL = 10_000


def _craft_skill() -> Path:
    """Resolve the skill file through the accessor the SHIPPING code uses."""

    plugin_dir = bundled_skills_plugin_dir()
    if plugin_dir is None:
        pytest.fail(
            "bundled_skills_plugin_dir() returned None: the bundled plugin "
            "manifest or skills/ directory is missing from the installed package."
        )
    return plugin_dir / "skills" / "sites-craft" / "SKILL.md"


def _skill_bytes(path: Path) -> int:
    """Measure the skill the way ``wc -c`` does: bytes on disk."""

    return path.stat().st_size


def test_the_fixture_reads_the_real_shipped_skill() -> None:
    """MUTATION: point ``_craft_skill`` at a name that does not exist.

    Without this the ceiling test would pass vacuously on a missing file.
    """

    path = _craft_skill()
    assert path.is_file(), f"sites-craft SKILL.md not found at {path}"
    size = path.stat().st_size
    assert size > _IMPLAUSIBLY_SMALL, (
        f"{path} is only {size} bytes. That is a broken read, not a lean skill."
    )


def test_sites_craft_skill_stays_under_its_ceiling() -> None:
    """MUTATION: append ~1,500 bytes of plausible construction rules to SKILL.md."""

    path = _craft_skill()
    size = _skill_bytes(path)
    assert size <= SITES_CRAFT_MAX_BYTES, (
        f"{path.name} is {size} bytes, over the {SITES_CRAFT_MAX_BYTES}-byte "
        f"ceiling by {size - SITES_CRAFT_MAX_BYTES}. The /sites agent loads this "
        "file whole before it writes tokens. Cut something before raising the "
        "ceiling, and if you do raise it, say why in the commit body."
    )


def test_the_ceiling_is_measured_in_bytes_not_decoded_characters() -> None:
    """MUTATION: ``return len(path.read_text(encoding="utf-8"))`` in ``_skill_bytes``.

    The file carries multi-byte characters (the true ellipsis it teaches), so
    bytes and characters differ and the swap is visible.
    """

    path = _craft_skill()
    assert _skill_bytes(path) == len(path.read_bytes())
    assert _skill_bytes(path) > len(path.read_text(encoding="utf-8"))


def test_press_scale_has_one_canonical_value() -> None:
    """Craft owns the press scale. The design doc settled 0.96 against 0.98 in
    its favour and design-taste dropped its own number, so the value lives here
    and nowhere else in the design-taste file.

    MUTATION: change ``scale(0.96)`` in sites-craft, or re-add a press scale
    number to design-taste.
    """

    skills = _craft_skill().parent.parent
    craft = _craft_skill().read_text(encoding="utf-8")
    taste = (skills / "pocketpaw-design-taste" / "SKILL.md").read_text(encoding="utf-8")
    assert "scale(0.96)" in craft
    assert "scale(0.9" not in taste and "0.98" not in taste
