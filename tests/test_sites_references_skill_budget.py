# tests/test_sites_references_skill_budget.py — bounds the byte size of the
# sites-references skill, the research loop and technique source list the /sites
# agent loads on demand. Same shape as test_sites_check_skill_budget.py; the
# mutations in tests/mutations/skill_budget.json trip the ceiling and the fixture.
"""``sites-references/SKILL.md`` stays under a stated byte ceiling.

WHY IT IS MEASURED. The /sites preamble advertises this skill in
``<design-skills>`` on create and on refine, so the agent loads it whenever the
look is open or it is about to hand-write a ground, shader or effect. It
replaced two skills (design-research and design-sources) on the promise that
one file costs less than both. Without a ceiling, every new source row would
quietly grow it back.

WHY 13,000. The design doc sets the budget at 13,000 bytes for the merged file:
the two skills it replaced came to about 15.6 KB, and the merged file landed
near 10.5 KB once their duplicated ownership tables, the Google Fonts face list
(owned by design-taste 2.F) and the overlapping tool notes were cut. Raising
the ceiling is allowed and should be argued for in the commit body.

WHAT IT DOES NOT COVER. Only this skill. ``wc -c`` units on the whole file,
frontmatter included, so the assertion is slightly stricter than the runtime
cost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

# The ceiling, in bytes, for the sites-references skill. See the module docstring.
SITES_REFERENCES_MAX_BYTES = 13_000

# A read that returns far less than this is a broken path, not a lean skill.
_IMPLAUSIBLY_SMALL = 5_000


def _sites_references_skill() -> Path:
    """Resolve the skill file through the accessor the SHIPPING code uses."""

    plugin_dir = bundled_skills_plugin_dir()
    if plugin_dir is None:
        pytest.fail(
            "bundled_skills_plugin_dir() returned None: the bundled plugin "
            "manifest or skills/ directory is missing from the installed package."
        )
    return plugin_dir / "skills" / "sites-references" / "SKILL.md"


def _skill_bytes(path: Path) -> int:
    """Measure the skill the way ``wc -c`` does: bytes on disk."""

    return path.stat().st_size


def test_the_fixture_reads_the_real_shipped_skill() -> None:
    """MUTATION: point ``_sites_references_skill`` at a name that does not exist.

    Without this the ceiling test would pass vacuously on a missing file.
    """

    path = _sites_references_skill()
    assert path.is_file(), f"sites-references SKILL.md not found at {path}"
    size = path.stat().st_size
    assert size > _IMPLAUSIBLY_SMALL, (
        f"{path} is only {size} bytes. That is a broken read, not a lean skill."
    )


def test_sites_references_skill_stays_under_its_ceiling() -> None:
    """MUTATION: append ~3,000 bytes of plausible source rows to SKILL.md."""

    path = _sites_references_skill()
    size = _skill_bytes(path)
    assert size <= SITES_REFERENCES_MAX_BYTES, (
        f"{path.name} is {size} bytes, over the {SITES_REFERENCES_MAX_BYTES}-byte "
        f"ceiling by {size - SITES_REFERENCES_MAX_BYTES}. The /sites agent loads "
        "this file whole. Cut something before raising the ceiling, and if you do "
        "raise it, say why in the commit body."
    )


def test_the_ceiling_is_measured_in_bytes_on_disk() -> None:
    """The unit is ``wc -c`` bytes, not decoded characters.

    The file is plain ASCII today, so bytes and characters agree and a
    characters-for-bytes swap is not observable yet; no mutation plan entry
    claims otherwise. This pins the measure against the raw bytes so a
    multi-byte character added later still counts at its real cost.
    """

    path = _sites_references_skill()
    assert _skill_bytes(path) == len(path.read_bytes())


def test_the_skill_has_no_em_dashes() -> None:
    """Generated copy carries no em dashes, so the prose that teaches it carries
    none either: the model copies the punctuation of the text it reads."""

    text = _sites_references_skill().read_text(encoding="utf-8")
    assert "—" not in text


def test_the_old_skill_dirs_are_gone() -> None:
    """The two skills this one replaced, and the deleted theme-system skill, are
    not shipped beside it. A stale dir would be advertised by nothing but still
    reachable through the wholesale plugin on refine, teaching rules that moved."""

    skills = _sites_references_skill().parent.parent
    for name in ("sites-design-research", "sites-design-sources", "sites-theme-system"):
        assert not (skills / name).exists(), name
