"""One way to name a thing the agent can act on.

Created: 2026-08-03 (feat/prompt-entity-ids).

Changes: 2026-08-03 (feat/prompt-entity-suffix) — two things, from review.

  * ``entity_line`` renders a SHORT id (:data:`ID_TAIL_CHARS` trailing chars)
    rather than the whole ObjectId, because the tools now resolve a tail. See
    "WHY THE TAIL AND NOT THE HEAD" below — the head of an ObjectId is a
    timestamp and is worthless for telling two rows apart.
  * ``unaddressed_line`` exists for rows whose entity NO tool addresses by id.
    It renders no id at all, and the kind it names is CHECKED against the tool
    schemas, so the claim cannot quietly go stale.

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

WHY THE TAIL AND NOT THE HEAD, and why a short id is safe at all. A Mongo
ObjectId is 4 bytes of timestamp, 5 bytes of per-process random, and a 3-byte
counter. Twelve widgets added to one pocket in one request therefore share the
first TWENTY hex characters and differ only in the counter — measured, not
assumed. A prefix is worse than useless for this job; the tail is perfect. Eight
tail chars is 4.3 billion values against a per-pocket population in the hundreds,
and the resolver refuses an ambiguous tail rather than picking, so the failure
mode is a loud error rather than the silent wrong-entity write this whole module
exists to prevent.

The saving is not cosmetic: the id was 24 of the ~70 chars in a widget row, and
that row is repeated 12 times inside a 1500-char cap.

WHAT THIS DELIBERATELY DOES NOT DO is invent a shortening the tools do not
understand. ``ID_TAIL_CHARS`` and the ``ID_TAIL_MARKER`` below are consumed by
``pocketpaw_ee.cloud.pockets.id_resolve``; changing either without changing that
resolver produces ids the agent cannot use. They are imported from here, not
re-declared there, so the two cannot drift.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ID_TAIL_CHARS",
    "ID_TAIL_MARKER",
    "MISSING_ID",
    "entity_line",
    "short_id",
    "unaddressed_line",
]

# What renders in place of an id the caller could not supply. Deliberately short,
# deliberately not empty, and deliberately not something that reads like a real
# id: the agent must not pass it to a tool, and a human reading a prompt dump
# should see the hole immediately. Grep for it to find call sites still missing.
MISSING_ID = "?"

# How many trailing characters of an id to render. Eight, and the number is a
# trade rather than a round figure: it is 16^8 = 4.3e9 values against a
# per-pocket widget population in the low hundreds and a per-workspace pocket
# population in the thousands, so a collision needs a deliberate effort. Going
# lower saves 1 char per row and buys real ambiguity; going higher spends chars
# on a collision rate already indistinguishable from zero.
ID_TAIL_CHARS = 8

# Prefixed to a shortened id so the agent can SEE it is a tail rather than a
# whole id, and so a human reading a prompt dump is not misled into pasting it
# somewhere that wants the real thing. The resolver strips it, and also strips a
# plain "..." because a model that retypes this will not reliably reproduce the
# single-character ellipsis.
ID_TAIL_MARKER = "…"


def short_id(entity_id: Any) -> str:
    """Render an id as a resolvable tail.

    Returns the id UNCHANGED when it is already at or under
    :data:`ID_TAIL_CHARS`, because shortening a short id only adds a marker and
    costs a character. Non-ObjectId ids (slugs, uuids, test fixtures) therefore
    pass through untouched, which is what the resolver expects.
    """
    text = _clean(entity_id)
    if len(text) <= ID_TAIL_CHARS:
        return text
    return f"{ID_TAIL_MARKER}{text[-ID_TAIL_CHARS:]}"


def entity_line(label: Any, entity_id: Any, /, **facts: Any) -> str:
    """Render one entity row for a prompt block.

    ``- {label} (id={tail}, {k}={v}, ...)``

    ``entity_id`` is positional and has NO default, which is the whole point of
    the function — see the module docstring. Both leading arguments are
    positional-only so a caller cannot pass ``label=`` / ``entity_id=`` and
    shadow a fact of the same name.

    The id is rendered as a tail (:func:`short_id`). Pass the WHOLE id here; the
    shortening is this function's job, so every row shortens the same way and a
    handler cannot invent its own.

    Facts render in call order, so a handler controls what a reader sees first
    after the id. Values are coerced through ``str``; a ``None`` or empty fact
    value renders as ``?`` for the same reason ``MISSING_ID`` does — an empty
    ``mime=`` reads as a formatting bug rather than as missing data.
    """
    name = _clean(label) or "(unnamed)"
    ident = short_id(entity_id) or MISSING_ID
    parts = [f"id={ident}"]
    parts.extend(f"{key}={_clean(value) or MISSING_ID}" for key, value in facts.items())
    return f"- {name} ({', '.join(parts)})"


def unaddressed_line(kind: str, label: Any, /, **facts: Any) -> str:
    """Render a row for an entity NO tool addresses by id.

    ``- {label} ({k}={v}, ...)`` — no id, because spending ~10 chars a row on an
    identifier nothing accepts is waste.

    ``kind`` is a CHECKED CLAIM, and that is the entire reason this function
    takes an argument it never renders. ``tests/cloud/surface/
    test_entity_id_contract.py`` reads these call sites, extracts the literal,
    and fails if that kind appears in the set of kinds derived from the MCP tool
    schemas. So the day somebody ships a tool with a required ``file_id``,
    ``files.py`` stops passing rather than quietly continuing to render rows the
    agent now needs ids for.

    That is the difference between this and an allow-list entry, which is what
    this replaced: an allow-list says "reviewed once, trust it", and this says
    "still true, re-checked every run". Pass a literal string — a computed kind
    cannot be read statically and the contract test rejects it.
    """
    name = _clean(label) or "(unnamed)"
    rendered = [f"{key}={_clean(value) or MISSING_ID}" for key, value in facts.items()]
    if not rendered:
        return f"- {name}"
    return f"- {name} ({', '.join(rendered)})"


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
