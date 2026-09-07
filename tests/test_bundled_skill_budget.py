# tests/test_bundled_skill_budget.py
# Created: 2026-08-07 (MT-3, feat/design-taste-real-webgl) — bounds the byte
# size of the one bundled skill that is embedded WHOLE into a system prompt.
# Nothing else in the repo measures it, so every edit to that file silently
# changed a per-turn runtime cost.
# Updated: 2026-09-06 (feat/fx-skill-amendments): ceiling raised 34,000 ->
# 34,500 so §2.C could teach the paw-fx effects registry instead of claiming
# libraries never resolve. The argument is in the module docstring; the
# mutations in tests/mutations/skill_budget.json still trip the new ceiling.
"""``pocketpaw-design-taste/SKILL.md`` stays under a stated byte ceiling.

WHY A SIZE TEST EXISTS FOR ONE MARKDOWN FILE. This skill is not merely
installed for on-demand invocation. ``sites.py``'s ``_design_system_block``
reads it off disk and inlines its body into the ``/sites`` surface preamble,
wrapped in ``<design-system name="pocketpaw-design-taste">`` and labelled
ALREADY LOADED so the agent applies it without a skill call. Every
site-authoring turn on that surface therefore pays for this file's length, in
tokens, forever. A prose edit to a SKILL.md reads like a docs change and is
priced like a prompt change.

The failure mode this guards is documented in ``pocketpaw/CLAUDE.md``: a file
in this exact class answered a lookup miss with 58,765 chars that did not
contain the answer. Growth here is not free and it is not visible in review —
a diff showing "+40 lines of guidance" looks like an improvement.

WHY A CEILING AND NOT THE CURRENT SIZE. The file measured 32,599 before MT-3
and 32,578 after. A ceiling pinned to the current size fails on the next honest
one-line fix, which trains people to bump the constant reflexively and turns
the gate into a formality. The original 34,000 left roughly 4% of headroom:
enough for ordinary maintenance, far too little to absorb a new module. Raising
it is allowed and should be argued for in the commit body, not done in passing.

RAISED TO 34,500 ON 2026-09-06 (feat/fx-skill-amendments). The file went
33,050 -> 33,978: the paw-fx effects registry shipped, and §2.C had to stop
telling the agent that libraries are impossible on every engine. It now sends
the agent to ``search_effects`` / ``get_effect`` before hand-writing a canvas
and states the per-engine rule (html serves any effect, vendored dependency and
all; svelte and react get the dependency-free ones only). That is roughly 930
bytes of new RULE, not new prose, and it landed 22 bytes under the old ceiling,
which is the pinned-to-current-size state this docstring warns about. 34,500
restores about 520 bytes of maintenance headroom while keeping the gate tight
enough to bite: it is still far too little to absorb another module.

WHAT IT DOES NOT COVER. Only this one skill. The other bundled skills are
invoked on demand rather than inlined, so their bytes are paid only when used;
if another skill is ever embedded whole into a preamble, it belongs here too.
The ceiling is measured on the WHOLE file, matching ``wc -c``, while the
preamble embeds the frontmatter-stripped body — so the assertion is slightly
stricter than the runtime cost, which is the right direction for a budget.

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
DESIGN_TASTE_MAX_BYTES = 34_500

# A read that returns far less than this is a broken path, not a lean skill.
# Without it, renaming the skill directory would make every assertion below
# pass on an empty string.
_IMPLAUSIBLY_SMALL = 10_000


def _design_taste_skill() -> Path:
    """Resolve the skill file through the accessor the SHIPPING code uses.

    ``sites.py`` reaches the file via ``bundled_skills_plugin_dir()``. Reading
    it any other way here (a hand-built relative path, a copy under tests/)
    would measure a file that is not necessarily the one served.
    """

    plugin_dir = bundled_skills_plugin_dir()
    if plugin_dir is None:
        pytest.fail(
            "bundled_skills_plugin_dir() returned None — the bundled plugin "
            "manifest or skills/ directory is missing from the installed "
            "package, so the /sites preamble would silently fall back to its "
            "one-line 'invoke the skill' stub."
        )
    return plugin_dir / "skills" / "pocketpaw-design-taste" / "SKILL.md"


def _skill_bytes(path: Path) -> int:
    """Measure the skill the way ``wc -c`` does: bytes on disk.

    The ceiling test goes through this helper rather than measuring inline, so
    the unit is a single named thing a mutation can flip. ``len(read_text())``
    would be several hundred short — the file is UTF-8 and full of em-dashes,
    arrows and typographic quotes — and a ceiling reviewed as "wc -c" but
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
    """The skill's bytes stay inside the budget the /sites preamble pays.

    MUTATION: append ~1,500 chars of plausible guidance to SKILL.md (see
    ``tests/mutations/skill_budget.json``). Before this test, that edit was
    invisible: every other assertion on the file checks for the PRESENCE of
    text, so growth could only ever make them greener.
    """

    path = _design_taste_skill()
    size = _skill_bytes(path)

    assert size <= DESIGN_TASTE_MAX_BYTES, (
        f"{path.name} is {size} bytes, over the {DESIGN_TASTE_MAX_BYTES}-byte "
        f"ceiling by {size - DESIGN_TASTE_MAX_BYTES}. This file is inlined "
        "whole into the /sites system prompt, so every byte is a cost on every "
        "site-authoring turn. Cut something before raising the ceiling, and if "
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
        "Expected multi-byte characters in the skill (em-dashes, arrows). If "
        "this ever becomes equal the file went plain-ASCII, and the ceiling "
        "unit no longer needs distinguishing — but check before relaxing it."
    )
