# tests/test_sites_reference_build_skill_budget.py — bounds the byte size of the
# sites-reference-build skill (build a site TO a supplied design) and pins the
# contract it shares with design-taste, sites-check and sites-craft: the four
# verdicts, the seven responsive transforms, the precedence sentence and the
# SC-REF ids. Mutations live in tests/mutations/skill_budget.json.
"""``sites-reference-build/SKILL.md`` stays under a stated byte ceiling.

WHY IT IS MEASURED. The /sites preamble names this skill in ``<design-skills>``
on create and refine with an input trigger: it loads only when the user hands
over a screenshot, mockup or Figma frame. On those turns the agent pays for the
whole file, and /sites denies ``Read``, so nothing can live in a reference file
beside it. Without a ceiling the method would grow toward the ~70 KB upstream it
was adapted from.

WHY 10,240. The PRD (Track C, section 5.3) budgets the skill at about 10 KB. The
file landed just under that once the greenfield modes, the Geist default, the
live-site measuring steps and the scripts were left upstream. Raising the
ceiling is allowed and should be argued for in the commit body.

WHAT IT DOES NOT COVER. ``wc -c`` units on the whole file, frontmatter
included, so the assertion is slightly stricter than the runtime cost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

# The ceiling, in bytes, for the sites-reference-build skill.
SITES_REFERENCE_BUILD_MAX_BYTES = 10_240

# A read that returns far less than this is a broken path, not a lean skill.
_IMPLAUSIBLY_SMALL = 5_000

_VERDICTS = ("READY", "READY WITH SUBSTITUTIONS", "NOT READY", "UNVERIFIED")
_TRANSFORMS = ("preserve", "reflow", "collapse", "reorder", "crop", "simplify", "hide")


def _skills_dir() -> Path:
    plugin_dir = bundled_skills_plugin_dir()
    if plugin_dir is None:
        pytest.fail(
            "bundled_skills_plugin_dir() returned None: the bundled plugin "
            "manifest or skills/ directory is missing from the installed package."
        )
    return plugin_dir / "skills"


def _reference_build_skill() -> Path:
    """Resolve the skill file through the accessor the SHIPPING code uses."""

    return _skills_dir() / "sites-reference-build" / "SKILL.md"


def _skill_bytes(path: Path) -> int:
    """Measure the skill the way ``wc -c`` does: bytes on disk."""

    return path.stat().st_size


def test_the_fixture_reads_the_real_shipped_skill() -> None:
    """MUTATION: point ``_reference_build_skill`` at a name that does not exist."""

    path = _reference_build_skill()
    assert path.is_file(), f"sites-reference-build SKILL.md not found at {path}"
    size = path.stat().st_size
    assert size > _IMPLAUSIBLY_SMALL, (
        f"{path} is only {size} bytes. That is a broken read, not a lean skill."
    )


def test_sites_reference_build_skill_stays_under_its_ceiling() -> None:
    """MUTATION: append ~2,000 bytes of plausible guidance to SKILL.md."""

    path = _reference_build_skill()
    size = _skill_bytes(path)
    assert size <= SITES_REFERENCE_BUILD_MAX_BYTES, (
        f"{path.name} is {size} bytes, over the {SITES_REFERENCE_BUILD_MAX_BYTES}-byte "
        f"ceiling by {size - SITES_REFERENCE_BUILD_MAX_BYTES}. The /sites agent loads "
        "this file whole. Cut something before raising the ceiling, and if you do "
        "raise it, say why in the commit body."
    )


def test_the_ceiling_is_measured_in_bytes_on_disk() -> None:
    """The file carries multi-byte characters (font and name examples), so a
    characters-for-bytes swap would grant unearned headroom."""

    path = _reference_build_skill()
    assert _skill_bytes(path) == len(path.read_bytes())
    assert _skill_bytes(path) > len(path.read_text(encoding="utf-8"))


def test_the_skill_carries_the_method_it_promises() -> None:
    """The four verdicts, the seven transforms, the three ledger statuses, the
    two-pass minimum and the authority line are the contract the preamble row
    advertises. A rewrite that drops one loses the piece the PRD adopted it for."""

    text = _reference_build_skill().read_text(encoding="utf-8")
    for verdict in _VERDICTS:
        assert f"**{verdict}:**" in text, verdict
    for transform in _TRANSFORMS:
        assert f"**{transform}:**" in text, transform
    for status in ("observed", "inferred", "substituted"):
        assert f"**{status}:**" in text, status
    assert "At least two correction passes" in text
    assert "A supplied design is the spec" in text
    # Sites load faces from Google Fonts only; the upstream default never ships.
    assert "Google Fonts" in text
    assert "Geist Serif" not in text
    assert "—" not in text


def test_the_skill_ships_its_attribution() -> None:
    """MIT requires the notice to travel with the adapted work."""

    vendored = (_reference_build_skill().parent / "VENDORED.md").read_text(encoding="utf-8")
    assert "Atharvsinh-codez/skills" in vendored
    assert "Atharv Jadav" in vendored
    assert "emilkowalski/skills" in vendored


def test_the_sibling_skills_carry_their_share() -> None:
    """The precedence sentence, the SC-REF ids and the craft additions are the
    other half of the adoption; each points back at this skill or its terms."""

    skills = _skills_dir()
    taste = (skills / "pocketpaw-design-taste" / "SKILL.md").read_text(encoding="utf-8")
    assert "A supplied design" in taste and "outranks taste" in taste
    assert "`sites-reference-build`" in taste

    check = (skills / "sites-check" / "SKILL.md").read_text(encoding="utf-8")
    for rule in ("SC-REF-01", "SC-REF-02", "SC-REF-03"):
        assert rule in check, rule
    for verdict in _VERDICTS:
        assert verdict in check, verdict

    craft = (skills / "sites-craft" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Product surfaces" in craft
    assert "A newer request wins" in craft
    assert "font ledger" in craft
    for transform in _TRANSFORMS:
        assert f"**{transform}**" in craft, transform
    # Above the floor heading, so refine turns (which embed the floor slice)
    # do not pay for it.
    assert craft.index("## Product surfaces") < craft.index("## 5. The floor")
