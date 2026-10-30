# fabric/resolver.py — Trust-ladder resolution over Statements (FST-2).
# Created: 2026-07-10 (feat/fst-2-resolver) — the brain of the source-truth
#   chain as a PURE function.
# Updated: 2026-07-10 (FST-2 spec review) — rank + validity joined the
#   within-tier ordering so FST-5's CHANGE verb (close the old statement's
#   valid_to + append a new PREFERRED statement) is visible to resolution;
#   is_disputed and unresolvable tightened accordingly (closed-validity
#   losers are resolved history, not live disputes).
# Updated: 2026-07-11 (FST-7 — within-FAMILY staleness demotion) — the ``now``
#   seam is live. ``now=None`` (every legacy caller) skips freshness ENTIRELY:
#   resolution is byte-identical to FST-2..6. With ``now`` passed, the EXACT
#   algorithm, applied on the ladder path only (a pin stays authoritative —
#   the pinned short-circuit runs before and ignores freshness):
#   1. Each unpinned survivor is classified fresh/aging/stale via
#      ``trust.freshness`` with the property's TTL class
#      (``rules.max_age_for`` — per-(object_type, property) overridable,
#      default class "default" = 30d). Timezone convention lives in
#      ``trust._as_utc``: compare in UTC, naive datetimes read AS UTC.
#   2. DEMOTION AT TIER SELECTION: if the ladder-order top candidate is
#      STALE, the winner becomes the FIRST candidate in ladder order that is
#      (a) in the SAME writer FAMILY as that top (FST-4 families: connector +
#      mirror = one machine-sync family; human, agent, inferred each their
#      own) and (b) fresh or aging. Everything else keeps its ladder order,
#      so the stale ex-top becomes the first loser. No same-family non-stale
#      candidate → NO demotion: a stale value never loses to a fresh value
#      from ANOTHER family (a stale connector beats a fresh inferred), and a
#      lone stale value still wins (reads never block). One pass — the
#      replacement is non-stale by construction, so demotion cannot cascade.
#   3. FRESHNESS-DEMOTED LOSERS ARE RESOLVED: when the (possibly demoted)
#      winner is non-stale, a STALE loser in the winner's family is treated
#      like closed-validity history — excluded from both the dispute and the
#      un-rankable checks (no conflict noise for "the sync moved on").
#      Cross-family stale losers and every loser under a STALE winner keep
#      the FST-2 semantics unchanged.
#   ``_family`` replicates store._writer_family's FST-4 mapping (the store
#   imports THIS module, so importing it back would cycle) — keep in sync.
#
# ``resolve(statements, rules)`` takes every Statement recorded for ONE
# (object_id, property) and picks the value the flat properties dict SHOULD
# hold, in this order:
#   1. deprecated statements are filtered out entirely (never winner, never
#      loser, never counted for disputes);
#   2. pinned short-circuit — a pinned survivor wins outright; multiple
#      pinned → newest recorded_at wins (id tiebreak), and multiple pins
#      always flag is_disputed (two humans pinning is a curation conflict
#      the lifecycle slice must surface, even when the values agree);
#   3. writer-class precedence from the TrustRules ladder (global default
#      human > connector > mirror > agent > inferred, per-(object_type,
#      property) overridable);
#   4. WITHIN the winning tier, in order:
#      a. rank: preferred > normal — within the tier ONLY; rank never
#         crosses tiers (a low-tier writer marking itself preferred must
#         never beat a higher-tier normal statement — that would break the
#         trust model);
#      b. validity: open (valid_to is None) > closed (valid_to set) — a
#         closed statement is superseded history but STAYS a candidate, so
#         reads never block even when every statement is closed;
#      c. recency on observed_at (when the source says it was true — NEVER
#         recorded_at/ingest time);
#      d. deterministic tiebreak: lexicographically smallest statement id.
#
# "Materially different" = normalized inequality, where normalization is:
# str values are .strip()ed, everything else compared with plain Python
# ``==``. is_disputed = any OPEN-validity survivor materially differs from
# the winner (closed-validity losers are RESOLVED history — e.g. a CHANGE's
# closed loser — and are excluded from the dispute check while remaining in
# losers[] for audit), or multiple pins per step 2. Un-rankable: on the
# ladder path, a survivor at the SAME tier and SAME rank as the winner, with
# BOTH open validity, a materially different value, and observed_at within
# rules.recency_epsilon_seconds of the winner's → the ordering is not
# trustworthy → unresolvable=True (a rank or validity difference IS a
# ranking — resolvable by definition). A deterministic provisional winner is
# STILL returned (reads never block; the conflict-lifecycle slice consumes
# the flag later).
#
# Pure and deterministic: no store access, no settings, no clock reads —
# the optional ``now`` parameter (FST-7) is the caller-supplied clock for
# freshness demotion; ``now=None`` keeps the pre-FST-7 behavior exactly.

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from pocketpaw.fabric.models import Statement
from pocketpaw.fabric.trust import Freshness, TrustRules, WriterClass, freshness


class Resolution(BaseModel):
    """The outcome of resolving one (object_id, property)'s statements.

    ``winner_statement is None`` means "no value could be resolved" (empty
    input, or every statement deprecated) — distinct from a winner whose
    ``value`` happens to be None. ``losers`` holds every non-deprecated,
    non-winning statement, ordered best-to-worst by the same ranking that
    picked the winner (so the whole Resolution is deterministic).
    ``unresolvable=True`` still carries a provisional winner: reads never
    block; the flag is the hand-off to the conflict lifecycle slice.
    """

    value: Any = None
    winner_statement: Statement | None = None
    is_disputed: bool = False
    losers: list[Statement] = Field(default_factory=list)
    unresolvable: bool = False
    # FST-7: the winner's freshness tri-state when ``now`` was passed to
    # resolve() on the ladder path; None on the pinned path (a pin ignores
    # freshness) and for legacy ``now=None`` callers. Consumed by the
    # divergence log line and the provenance read surface.
    winner_freshness: Freshness | None = None


def _normalize(value: Any) -> Any:
    """The small normalization behind "materially different".

    Exact rule: strings are compared after ``.strip()`` (leading/trailing
    whitespace is immaterial; case and inner whitespace ARE material);
    every other type is returned as-is and compared with plain Python
    ``==`` (so ``1 == 1.0`` is the same value, ``None`` only equals
    ``None``, dicts/lists compare structurally).
    """
    if isinstance(value, str):
        return value.strip()
    return value


def _materially_different(a: Any, b: Any) -> bool:
    """True when two values differ after :func:`_normalize`."""
    return _normalize(a) != _normalize(b)


def _tier(writer_class: str, ladder: list[WriterClass]) -> int:
    """Rank of a writer_class on the effective ladder (0 = strongest).

    A class missing from the ladder (possible under a partial override)
    ranks BELOW every listed class, all unlisted classes sharing one bottom
    tier — statements are never dropped just because an override forgot a
    class.
    """
    try:
        return ladder.index(writer_class)  # type: ignore[arg-type]
    except ValueError:
        return len(ladder)


def _family(writer_class: str) -> str:
    """Writer FAMILY for FST-7 staleness demotion — the demotion boundary.

    Replicates :func:`store._writer_family` (FST-4): "connector" and
    "mirror" are ONE machine-sync family; "human", "agent", "inferred" are
    each their own. Replicated rather than imported because the store
    imports THIS module (importing it back would cycle) — keep in sync.
    """
    return "sync" if writer_class in ("connector", "mirror") else writer_class


def _rank_key(statement: Statement) -> int:
    """Within-tier rank leg: preferred (0) beats normal (1).

    Deprecated never reaches a sort (filtered in step 1). Because this leg
    sits AFTER the tier leg in the key tuple, rank never crosses tiers.
    """
    return 0 if statement.rank == "preferred" else 1


def _validity_key(statement: Statement) -> int:
    """Within-tier validity leg: open (valid_to is None, 0) beats closed (1).

    A closed statement is superseded history but stays a candidate — reads
    never block even when every survivor is closed.
    """
    return 0 if statement.valid_to is None else 1


def _ladder_sort_key(
    statement: Statement, ladder: list[WriterClass]
) -> tuple[int, int, int, float, str]:
    """Total order for the ladder path: tier asc, then within the tier
    rank (preferred > normal), validity (open > closed), observed_at desc,
    id asc.

    observed_at is keyed via ``timestamp()`` so it can be negated for the
    descending leg; naive datetimes are interpreted in local time (the
    store's default), aware ones absolutely.
    """
    return (
        _tier(statement.writer_class, ladder),
        _rank_key(statement),
        _validity_key(statement),
        -statement.observed_at.timestamp(),
        statement.id,
    )


def _pin_sort_key(statement: Statement) -> tuple[float, str]:
    """Total order among pinned survivors: recorded_at desc, id asc."""
    return (-statement.recorded_at.timestamp(), statement.id)


def resolve(
    statements: list[Statement],
    rules: TrustRules,
    *,
    object_type: str | None = None,
    now: datetime | None = None,
) -> Resolution:
    """Resolve one (object_id, property)'s statements to a single value.

    Pure and deterministic: same input (in any order) → same Resolution.
    ``rules`` is passed in — no settings access. ``object_type`` selects
    per-property ladder overrides (``None`` = global ladder only). ``now``
    (FST-7) is the caller-supplied clock for within-family staleness
    demotion — see the module comment for the exact algorithm; ``None``
    (every legacy caller) skips freshness entirely.

    Raises ``ValueError`` if the statements span more than one
    (object_id, property) — the resolver is per-property by contract.
    An empty list is NOT an error: it returns the no-value Resolution.
    """
    if not statements:
        return Resolution()

    keys = {(s.object_id, s.property) for s in statements}
    if len(keys) > 1:
        raise ValueError(
            f"resolve() expects statements for ONE (object_id, property); got {sorted(keys)}"
        )

    # Step 1 — deprecated statements are out entirely.
    survivors = [s for s in statements if s.rank != "deprecated"]
    if not survivors:
        return Resolution()

    prop = survivors[0].property
    ladder = rules.ladder_for(object_type, prop)

    # Step 2 — pinned short-circuit.
    pinned = sorted((s for s in survivors if s.pinned), key=_pin_sort_key)
    unpinned = sorted(
        (s for s in survivors if not s.pinned), key=lambda s: _ladder_sort_key(s, ladder)
    )

    if pinned:
        ranked = pinned + unpinned
        winner = ranked[0]
        losers = ranked[1:]
        # Multiple pins always dispute (curation conflict), and any
        # OPEN-validity survivor materially differing from the pinned winner
        # disputes too (closed losers are resolved history). A pin is
        # authoritative, so the pinned path is never unresolvable.
        disputed = len(pinned) > 1 or any(
            s.valid_to is None and _materially_different(s.value, winner.value) for s in losers
        )
        return Resolution(
            value=winner.value,
            winner_statement=winner,
            is_disputed=disputed,
            losers=losers,
            unresolvable=False,
        )

    # Steps 3–4 — ladder tier, then within-tier rank > validity > recency > id.
    ranked = unpinned

    # FST-7 — within-FAMILY staleness demotion, only when the caller passed
    # ``now`` (legacy callers skip freshness entirely; resolution is
    # byte-identical to pre-FST-7). If the ladder-order top is STALE, the
    # first fresh/aging candidate in ladder order FROM THE SAME WRITER
    # FAMILY (see _family — connector+mirror are one machine-sync family)
    # takes the win; everything else keeps ladder order, so the stale ex-top
    # is the first loser. Demotion never crosses families, and a lone stale
    # value still wins (reads never block). One pass — the replacement is
    # non-stale by construction, so demotion cannot cascade.
    freshness_state: dict[str, Freshness] = {}
    if now is not None:
        max_age = rules.max_age_for(object_type, prop)
        freshness_state = {s.id: freshness(s.observed_at, max_age, now) for s in ranked}
        top = ranked[0]
        if freshness_state[top.id] == "stale":
            replacement = next(
                (
                    s
                    for s in ranked[1:]
                    if _family(s.writer_class) == _family(top.writer_class)
                    and freshness_state[s.id] != "stale"
                ),
                None,
            )
            if replacement is not None:
                ranked = [replacement, *(s for s in ranked if s is not replacement)]

    winner = ranked[0]
    losers = ranked[1:]

    def _freshness_resolved(s: Statement) -> bool:
        """FST-7: a STALE loser in a NON-STALE winner's family is resolved
        history ("the sync moved on"), not conflict noise — excluded from
        the dispute and un-rankable checks. Always False when ``now`` was
        not passed (empty freshness_state) or when the winner is stale."""
        return (
            bool(freshness_state)
            and freshness_state[winner.id] != "stale"
            and freshness_state[s.id] == "stale"
            and _family(s.writer_class) == _family(winner.writer_class)
        )

    # Disputes count OPEN-validity losers only: a closed-validity statement
    # that lost (e.g. the old half of a CHANGE) is resolved history, not a
    # live dispute. It stays in losers[] for audit. FST-7 adds the
    # freshness-resolved exclusion (see _freshness_resolved).
    disputed = any(
        s.valid_to is None
        and _materially_different(s.value, winner.value)
        and not _freshness_resolved(s)
        for s in losers
    )

    # Un-rankable detection: a contender at the SAME tier and SAME rank as
    # the winner, BOTH open-validity, materially different value, observed_at
    # within the recency epsilon — recency can't be trusted to order them.
    # A rank or validity difference IS a ranking: resolvable by definition.
    top_tier = _tier(winner.writer_class, ladder)
    epsilon = rules.recency_epsilon_seconds
    unresolvable = winner.valid_to is None and any(
        _tier(s.writer_class, ladder) == top_tier
        and s.rank == winner.rank
        and s.valid_to is None
        and _materially_different(s.value, winner.value)
        and abs((winner.observed_at - s.observed_at).total_seconds()) <= epsilon
        and not _freshness_resolved(s)
        for s in losers
    )

    return Resolution(
        value=winner.value,
        winner_statement=winner,
        is_disputed=disputed,
        losers=losers,
        unresolvable=unresolvable,
        winner_freshness=freshness_state.get(winner.id),
    )
