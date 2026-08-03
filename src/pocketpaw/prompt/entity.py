"""One way to name a thing the agent can act on.

Created: 2026-08-03 (feat/prompt-entity-ids).

THE BUG THIS CLOSES is not in any one handler. Every prompt block that lists
entities was written independently, and they disagree about whether to carry the
identifier the agent needs to act:

    handlers/agents.py       - Research Bot (slug=research-bot)     <- carries one
    handlers/pockets_list.py - Sales (type=custom, widgets=3)       <- does not
    handlers/files.py        - report.pdf (application/pdf)         <- does not
    _helpers.format_widget_line
                             - Revenue (native)                     <- does not

Meanwhile ``update_widget`` declares ``"required": ["pocket_id", "widget_id",
"fields"]``. So the prompt names a widget the agent cannot address, and the agent
either burns a round-trip re-fetching what it was just told, or guesses. Two
pockets called Sales and the guess is a coin flip that fails silently — the tool
call succeeds against the wrong entity.

The same shape bit the ``<about-member>`` block (fixed 2026-08-03): it named
people by name in rooms that can hold two people with one name.

WHY A RENDERER AND NOT FOUR PATCHES. Patching the four known sites leaves the
fifth handler — the one nobody has written yet — free to make the same mistake,
and it will, because ``rows.append(f"- {name} ...")`` is the obvious thing to
type. This module is the one spelling, and ``entity_id`` is a REQUIRED parameter:
there is no way to call it that silently drops the id. A caller that genuinely
has no id must pass ``None`` and gets ``id=?`` in the output — wrong, but VISIBLE
in the prompt and greppable in the source, which is what "no id here" should cost.

The enforcement half lives in ``tests/cloud/surface/test_entity_id_contract.py``:
an AST scan that fails any handler hand-rolling a row, plus a check that derives
the set of addressable entity kinds from the TOOL SCHEMAS rather than a list
someone has to remember to update. Add a tool with a required ``site_id`` and the
sites preamble is required to carry ids from that commit on.

WHAT THIS DELIBERATELY DOES NOT DO is truncate the id. Every other field here is
advisory and a shortened one still reads; an id is either exact or it is a failed
tool call, so it is passed through whole and the caps are left to bite elsewhere.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MISSING_ID", "entity_line"]

# What renders in place of an id the caller could not supply. Deliberately short,
# deliberately not empty, and deliberately not something that reads like a real
# id: the agent must not pass it to a tool, and a human reading a prompt dump
# should see the hole immediately. Grep for it to find call sites still missing.
MISSING_ID = "?"


def entity_line(label: Any, entity_id: Any, /, **facts: Any) -> str:
    """Render one entity row for a prompt block.

    ``- {label} (id={entity_id}, {k}={v}, ...)``

    ``entity_id`` is positional and has NO default, which is the whole point of
    the function — see the module docstring. Both leading arguments are
    positional-only so a caller cannot pass ``label=`` / ``entity_id=`` and
    shadow a fact of the same name.

    Facts render in call order, so a handler controls what a reader sees first
    after the id. Values are coerced through ``str``; a ``None`` or empty fact
    value renders as ``?`` for the same reason ``MISSING_ID`` does — an empty
    ``mime=`` reads as a formatting bug rather than as missing data.
    """
    name = _clean(label) or "(unnamed)"
    ident = _clean(entity_id) or MISSING_ID
    parts = [f"id={ident}"]
    parts.extend(f"{key}={_clean(value) or MISSING_ID}" for key, value in facts.items())
    return f"- {name} ({', '.join(parts)})"


def _clean(value: Any) -> str:
    """Collapse a value to one tidy line.

    A newline inside a row would split it into two rows, and a row that is
    really half a row is worse than a truncated one — the line-aware preamble
    cap would then cut at a boundary that does not exist in the data. Filenames
    and pocket names are user-supplied, so this is reachable, not theoretical.
    """
    if value is None:
        return ""
    return " ".join(str(value).split())
