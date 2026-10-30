# tests/test_bundled_skill_budget.py — bounds the byte size of the
# pocketpaw-design-taste skill. Nothing else in the repo measures it, so without
# this every edit to that file would silently change what a /sites turn costs
# when the agent loads it. The argument for the number is in the docstring; the
# mutations in tests/mutations/skill_budget.json trip the ceiling.
"""``pocketpaw-design-taste/SKILL.md`` stays under a stated byte ceiling.

WHY A SIZE TEST EXISTS FOR ONE MARKDOWN FILE. The /sites preamble names this
skill in ``<design-skills>`` and the agent loads it through the ``Skill`` tool
before the first UI file on create and before any new section or restyle on
refine. Nothing embeds it any more, but on the turns that load it, the agent
pays for the whole file in tokens. A prose edit to a SKILL.md reads like a docs
change and is priced like a prompt change.

The failure mode this guards is documented in ``pocketpaw/CLAUDE.md``: a file
in this exact class answered a lookup miss with 58,765 chars that did not
contain the answer. Growth here is not free and it is not visible in review:
a diff showing "+40 lines of guidance" looks like an improvement.

WHY 36,500 AND NOT THE CURRENT SIZE. A ceiling pinned to the current size fails
on the next honest one-line fix, which trains people to bump the constant
reflexively and turns the gate into a formality. The number was set to leave
roughly 2.8% of headroom over the file it was raised for: enough for ordinary
maintenance, far too little to absorb a new module. Raising it is allowed and
should be argued for in the commit body, not done in passing. Each past raise
(34,000, then 35,000, then 36,500) followed a new module that was paid for with
cuts elsewhere in the file; ``git log -p`` on this file has the arguments.

WHAT IT DOES NOT COVER. Only this one skill. The other bundled skills are
smaller or rarely loaded; if another one grows into the same class, it belongs
here too. The ceiling is measured on the WHOLE file, matching ``wc -c``, which
includes the frontmatter the loader strips, so the assertion is slightly
stricter than the runtime cost. That is the right direction for a budget.

Mutations: ``tests/mutations/skill_budget.json``. Run them; a size assertion
nobody watched fail is precisely the class of gate ``pocketpaw/CLAUDE.md``
warns about.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

# The ceiling, in bytes, for the design-taste skill. See the module docstring
# for why this number and not the file's current size.
DESIGN_TASTE_MAX_BYTES = 36_500

# A read that returns far less than this is a broken path, not a lean skill.
# Without it, renaming the skill directory would make every assertion below
# pass on an empty string.
_IMPLAUSIBLY_SMALL = 10_000


def _design_taste_skill() -> Path:
    """Resolve the skill file through the accessor the SHIPPING code uses.

    The installer and ``sites.py`` reach bundled skills via
    ``bundled_skills_plugin_dir()``. Reading
    it any other way here (a hand-built relative path, a copy under tests/)
    would measure a file that is not necessarily the one served.
    """

    plugin_dir = bundled_skills_plugin_dir()
    if plugin_dir is None:
        pytest.fail(
            "bundled_skills_plugin_dir() returned None — the bundled plugin "
            "manifest or skills/ directory is missing from the installed "
            "package, so the /sites agent could not load the design skills it "
            "is told to load."
        )
    return plugin_dir / "skills" / "pocketpaw-design-taste" / "SKILL.md"


def _skill_bytes(path: Path) -> int:
    """Measure the skill the way ``wc -c`` does: bytes on disk.

    The ceiling test goes through this helper rather than measuring inline, so
    the unit is a single named thing a mutation can flip. ``len(read_text())``
    would be short: the file is UTF-8 with arrows, typographic quotes and the
    literal dash characters its copy rules name, and a ceiling reviewed as "wc -c" but
    asserted in decoded characters grants unearned headroom.
    """

    return path.stat().st_size


def test_the_fixture_reads_the_real_shipped_skill() -> None:
    """The measurement is worth exactly what the path is worth.

    MUTATION: point ``_design_taste_skill`` at a name that does not exist, or
    have it return an empty file. Without this test the ceiling assertion goes
    green on 0 bytes and reports headroom that does not exist — the same shape
    as the ``test_preamble_caps.py`` fixture that measured id-less rows.
    """

    path = _design_taste_skill()
    assert path.is_file(), f"design-taste SKILL.md not found at {path}"

    size = path.stat().st_size
    assert size > _IMPLAUSIBLY_SMALL, (
        f"{path} is only {size} bytes. That is not a lean skill, it is a "
        "broken read — the ceiling below would pass vacuously."
    )


def test_design_taste_skill_stays_under_its_ceiling() -> None:
    """The skill's bytes stay inside the budget a /sites turn pays to load it.

    MUTATION: append ~1,500 chars of plausible guidance to SKILL.md (see
    ``tests/mutations/skill_budget.json``). Before this test, that edit was
    invisible: every other assertion on the file checks for the PRESENCE of
    text, so growth could only ever make them greener.
    """

    path = _design_taste_skill()
    size = _skill_bytes(path)

    assert size <= DESIGN_TASTE_MAX_BYTES, (
        f"{path.name} is {size} bytes, over the {DESIGN_TASTE_MAX_BYTES}-byte "
        f"ceiling by {size - DESIGN_TASTE_MAX_BYTES}. The /sites agent loads "
        "this file whole before it designs, so every byte is a cost on every "
        "turn that loads it. Cut something before raising the ceiling, and if "
        "you do raise it, say why in the commit body."
    )


def test_the_ceiling_is_measured_in_bytes_not_decoded_characters() -> None:
    """``_skill_bytes`` is ``wc -c``, and for this file that is not ``len()``.

    MUTATION: ``return len(path.read_text(encoding="utf-8"))`` in
    ``_skill_bytes``. The ceiling test alone does NOT catch that — the file is
    under the ceiling in either unit, so it stays green while silently
    measuring a smaller number. This test is what makes the swap visible.
    """

    path = _design_taste_skill()
    char_len = len(path.read_text(encoding="utf-8"))

    assert _skill_bytes(path) == len(path.read_bytes())
    assert _skill_bytes(path) > char_len, (
        "Expected multi-byte characters in the skill (arrows, quotes). If "
        "this ever becomes equal the file went plain-ASCII, and the ceiling "
        "unit no longer needs distinguishing — but check before relaxing it."
    )
