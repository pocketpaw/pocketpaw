# tests/test_sites_conversion_skill_budget.py: bounds the byte size of the
# sites-conversion-structure skill, the argument and positive copy method the
# /sites agent loads on create and on refine. Same shape as
# test_sites_check_skill_budget.py; the mutations in
# tests/mutations/skill_budget.json trip the ceiling and the fixture.
"""``sites-conversion-structure/SKILL.md`` stays under a stated byte ceiling.

WHY IT IS MEASURED. The /sites preamble names this skill in ``<design-skills>``
on create and, since its scope moved to both, on refine whenever an edit
rewrites copy or adds a section. It handed its banned-word lists to
``sites-check`` on the promise that copy tells live in one place. Without a
ceiling, those lists would quietly grow back here.

WHY 18,500. The design doc sets the budget at 18,500 bytes. The file landed
near 17.7 KB with the About page shape, the voice carve-out for legal and
booking text, and the never-say intake line added. The headroom is for a few
lines, not a new section. Raising the ceiling is allowed and should be argued
for in the commit body.

WHAT IT DOES NOT COVER. Only this skill. ``wc -c`` units on the whole file,
frontmatter included, so the assertion is slightly stricter than the runtime
cost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

# The ceiling, in bytes, for the conversion skill. See the module docstring.
SITES_CONVERSION_MAX_BYTES = 18_500

# A read that returns far less than this is a broken path, not a lean skill.
_IMPLAUSIBLY_SMALL = 10_000


def _conversion_skill() -> Path:
    """Resolve the skill file through the accessor the SHIPPING code uses."""

    plugin_dir = bundled_skills_plugin_dir()
    if plugin_dir is None:
        pytest.fail(
            "bundled_skills_plugin_dir() returned None: the bundled plugin "
            "manifest or skills/ directory is missing from the installed package."
        )
    return plugin_dir / "skills" / "sites-conversion-structure" / "SKILL.md"


def _skill_bytes(path: Path) -> int:
    """Measure the skill the way ``wc -c`` does: bytes on disk."""

    return path.stat().st_size


def test_the_fixture_reads_the_real_shipped_skill() -> None:
    """MUTATION: point ``_conversion_skill`` at a name that does not exist.

    Without this the ceiling test would pass vacuously on a missing file.
    """

    path = _conversion_skill()
    assert path.is_file(), f"sites-conversion-structure SKILL.md not found at {path}"
    size = path.stat().st_size
    assert size > _IMPLAUSIBLY_SMALL, (
        f"{path} is only {size} bytes. That is a broken read, not a lean skill."
    )


def test_sites_conversion_skill_stays_under_its_ceiling() -> None:
    """MUTATION: append ~1,500 bytes of plausible copy rules to SKILL.md."""

    path = _conversion_skill()
    size = _skill_bytes(path)
    assert size <= SITES_CONVERSION_MAX_BYTES, (
        f"{path.name} is {size} bytes, over the {SITES_CONVERSION_MAX_BYTES}-byte "
        f"ceiling by {size - SITES_CONVERSION_MAX_BYTES}. The /sites agent loads "
        "this file whole before it writes the argument. Cut something before "
        "raising the ceiling, and if you do raise it, say why in the commit body."
    )


def test_the_skill_has_no_em_dashes() -> None:
    """The skill teaches copy, and the model copies the punctuation of the text
    that teaches it, so its own prose and examples carry no em dash."""

    text = _conversion_skill().read_text(encoding="utf-8")
    assert "\u2014" not in text


def test_the_skill_prescribes_no_headline_template() -> None:
    """The mad-lib headline formulas produced the same sentence on every page;
    the skill teaches the plainest true shape instead.

    MUTATION: re-add a ``{Outcome} without {pain}`` formula line.
    """

    text = _conversion_skill().read_text(encoding="utf-8")
    assert "plainest true shape" in text
    assert "{Outcome}" not in text and "{pain}" not in text
