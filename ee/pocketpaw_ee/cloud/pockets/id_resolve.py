# id_resolve.py — turn the shortened id the prompt showed back into a real one.
#
# Created: 2026-08-03 (feat/prompt-entity-suffix).
#
# WHY THIS EXISTS. The prompt used to render whole 24-char ObjectIds on every
# entity row, which cost 24 of the ~70 chars in a widget row inside a 1500-char
# preamble cap. ``pocketpaw.prompt.entity`` now renders the last 8 characters
# instead, so the tools have to accept what the agent was shown.
#
# THE TAIL, NOT THE HEAD, and this is the load-bearing fact. An ObjectId is
# 4 bytes of timestamp + 5 bytes of per-process random + a 3-byte counter, so
# twelve widgets added to one pocket in one request share the first TWENTY hex
# characters and differ only in the counter. Measured 2026-08-03: of 12 ids
# created together, the first 12 hex chars gave 1 distinct value and the last 6
# gave 12. Any prefix scheme here would have been worse than useless.
#
# THE SAFETY PROPERTY, stated plainly because getting it wrong reintroduces the
# exact bug the id rendering was added to fix: an ambiguous tail RAISES. It never
# picks, never takes the first match, never falls back to "closest". A silent
# wrong-entity write is what "- Sales (type=custom)" already did; a short id that
# resolved loosely would be the same bug wearing a different hat.
#
# Resolution is always SCOPED to a candidate list the caller supplies — a
# workspace's pockets, a pocket's widgets. There is deliberately no "search all
# collections for this tail" entry point: an unscoped resolve would be a tenancy
# hole, since a tail from one workspace could land on another's document.

from __future__ import annotations

from typing import Any

from pocketpaw.prompt.entity import ID_TAIL_CHARS, ID_TAIL_MARKER

__all__ = ["AmbiguousId", "normalize_id_input", "resolve_id"]

# Markers a model might reproduce when echoing a shortened id back. The single
# character is what we render; "..." is what a model retyping it tends to
# produce, and stripping both costs nothing.
_STRIPPABLE_PREFIXES = (ID_TAIL_MARKER, "...")


class AmbiguousId(ValueError):
    """More than one candidate ends with the given tail.

    Carries the matches so a tool can tell the agent what to disambiguate with,
    rather than making it guess a second time.
    """

    def __init__(self, given: str, matches: list[str]) -> None:
        self.given = given
        self.matches = matches
        super().__init__(
            f"{given!r} matches {len(matches)} entities ({', '.join(matches[:5])}"
            f"{', …' if len(matches) > 5 else ''}). Use the full id."
        )


def normalize_id_input(raw: Any) -> str:
    """Strip the tail marker and whitespace off whatever the agent sent.

    The agent sees ``id=…3f9a1c07`` and may send it back with the marker, with
    an ASCII ``...``, with neither, or wrapped in quotes it picked up from the
    surrounding text. All four mean the same thing.
    """
    text = str(raw or "").strip().strip("'\"").strip()
    for prefix in _STRIPPABLE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    return text


def resolve_id(raw: Any, candidates: list[Any], *, key: str = "id") -> str:
    """Resolve a whole-or-shortened id against a scoped candidate list.

    ``candidates`` are the entities the caller has already tenancy-filtered —
    one workspace's pockets, one pocket's widgets. ``key`` names the id
    attribute (or dict key; both are accepted, because the pocket wire dict uses
    ``_id`` while the domain objects use ``id``).

    Returns the FULL id. Raises :class:`AmbiguousId` when a tail matches more
    than one candidate, and :class:`KeyError` when it matches none.

    An exact match always wins outright, even if the same string is also a tail
    of some longer id. That ordering matters: a caller passing a real id — the
    frontend, a stored reference, any pre-existing call site — must never be
    told its own id is ambiguous.
    """
    given = normalize_id_input(raw)
    if not given:
        raise KeyError("no id given")

    ids = [_id_of(c, key) for c in candidates]
    ids = [i for i in ids if i]

    if given in ids:
        return given

    # Only a plausible tail is worth matching. Anything longer than an id, or
    # longer than what we ever render, is a typo rather than a shortening — and
    # matching it loosely would be guessing.
    if len(given) > ID_TAIL_CHARS:
        raise KeyError(given)

    matches = [i for i in ids if i.endswith(given)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousId(given, matches)
    raise KeyError(given)


def _id_of(candidate: Any, key: str) -> str:
    """Read an id off a dict or an object, tolerating the ``_id`` wire spelling."""
    if isinstance(candidate, dict):
        value = candidate.get(key) or candidate.get("_id") or candidate.get("id")
    else:
        value = getattr(candidate, key, None) or getattr(candidate, "id", None)
    return "" if value is None else str(value)
