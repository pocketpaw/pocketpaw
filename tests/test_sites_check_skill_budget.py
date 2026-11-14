# tests/test_sites_check_skill_budget.py — bounds the byte size of the sites-check
# skill, the merged defect list the /sites agent loads before showing a draft and
# when the user asks for a review. Same shape as test_bundled_skill_budget.py; the
# mutations in tests/mutations/skill_budget.json trip the ceiling and the fixture.
"""``sites-check/SKILL.md`` stays under a stated byte ceiling.

WHY IT IS MEASURED. The /sites preamble names this skill in ``<design-skills>``
and in create step 5b, so the agent loads it on nearly every create turn and on
any refine that moves layout. It replaced two skills (ship-fixes and
interface-review) on the promise that one file costs less than both. Without a
ceiling, every new rule id would quietly grow it back.

WHY 26,000. The design doc sets the budget at 26,000 bytes for the merged file:
the two skills it replaced came to about 26.7 KB before their duplicated craft
rules were cut, and the merged file landed under 25 KB with the new defect
rules added. The headroom is for a few more ids, not for a new section. Raising
the ceiling is allowed and should be argued for in the commit body.

WHAT IT DOES NOT COVER. Only this skill. ``wc -c`` units on the whole file,
frontmatter included, so the assertion is slightly stricter than the runtime
cost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

# The ceiling, in bytes, for the sites-check skill. See the module docstring.
SITES_CHECK_MAX_BYTES = 26_000

# A read that returns far less than this is a broken path, not a lean skill.
_IMPLAUSIBLY_SMALL = 10_000


def _sites_check_skill() -> Path:
    """Resolve the skill file through the accessor the SHIPPING code uses."""

    plugin_dir = bundled_skills_plugin_dir()
    if plugin_dir is None:
        pytest.fail(
            "bundled_skills_plugin_dir() returned None: the bundled plugin "
            "manifest or skills/ directory is missing from the installed package."
        )
    return plugin_dir / "skills" / "sites-check" / "SKILL.md"


def _skill_bytes(path: Path) -> int:
    """Measure the skill the way ``wc -c`` does: bytes on disk."""

    return path.stat().st_size


def test_the_fixture_reads_the_real_shipped_skill() -> None:
    """MUTATION: point ``_sites_check_skill`` at a name that does not exist.

    Without this the ceiling test would pass vacuously on a missing file.
    """

    path = _sites_check_skill()
    assert path.is_file(), f"sites-check SKILL.md not found at {path}"
    size = path.stat().st_size
    assert size > _IMPLAUSIBLY_SMALL, (
        f"{path} is only {size} bytes. That is a broken read, not a lean skill."
    )


def test_sites_check_skill_stays_under_its_ceiling() -> None:
    """MUTATION: append ~1,500 bytes of plausible rules to SKILL.md."""

    path = _sites_check_skill()
    size = _skill_bytes(path)
    assert size <= SITES_CHECK_MAX_BYTES, (
        f"{path.name} is {size} bytes, over the {SITES_CHECK_MAX_BYTES}-byte "
        f"ceiling by {size - SITES_CHECK_MAX_BYTES}. The /sites agent loads this "
        "file whole before showing a draft. Cut something before raising the "
        "ceiling, and if you do raise it, say why in the commit body."
    )


def test_the_ceiling_is_measured_in_bytes_not_decoded_characters() -> None:
    """MUTATION: ``return len(path.read_text(encoding="utf-8"))`` in ``_skill_bytes``.

    The file carries at least one multi-byte character (the middle dot in the
    meta-string tell), so bytes and characters differ and the swap is visible.
    """

    path = _sites_check_skill()
    assert _skill_bytes(path) == len(path.read_bytes())
    assert _skill_bytes(path) > len(path.read_text(encoding="utf-8"))


def test_the_skill_has_no_em_dashes() -> None:
    """The skill bans em dashes in generated copy, so its own prose and examples
    carry none: the model copies the punctuation of the text that teaches it."""

    text = _sites_check_skill().read_text(encoding="utf-8")
    assert "—" not in text
