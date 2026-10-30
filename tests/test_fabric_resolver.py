# tests/test_fabric_resolver.py
# Created: 2026-07-10 (FST-2 — trust-ladder resolver).
# Updated: 2026-07-10 (FST-2 spec review — rank + validity join the
#   within-tier order) — added the CHANGE-verb simulation (old normal+closed
#   loses to new preferred+open, clean: not disputed, not unresolvable),
#   preferred > normal within a tier, rank never crossing tiers, open >
#   closed validity, all-closed still resolving, and the tightened
#   dispute/unresolvable semantics; the determinism shuffle now exercises
#   the new sort keys too.
#
# Exhaustive coverage of the PURE resolution brain (fabric/resolver.py +
# fabric/trust.py) before it touches any write path:
#   * every ladder rung — each adjacent writer-class pair, stronger beats
#     weaker even when the weaker statement is newer,
#   * pinned short-circuit — a single pin beats any unpinned tier; multiple
#     pins → newest recorded_at wins (id tiebreak on equal recorded_at) and
#     is_disputed is always flagged; a deprecated pin does NOT short-circuit,
#   * deprecated exclusion — deprecated statements are never winner, never
#     loser, never counted; all-deprecated → no-value Resolution,
#   * within-tier order: rank (preferred > normal, never crossing tiers) →
#     validity (open > closed, closed stays a candidate) → observed_at
#     recency (never recorded_at) → lexicographic-id tiebreak,
#   * un-rankable detection — same tier + SAME rank + BOTH open validity +
#     materially different values + observed_at within recency_epsilon →
#     unresolvable=True with a deterministic provisional winner (reads never
#     block); beyond epsilon, across tiers, across ranks, or with a closed
#     side → resolvable (a rank/validity difference IS a ranking),
#   * is_disputed counts OPEN-validity losers only — a closed loser (the old
#     half of a CHANGE) is resolved history, kept in losers[] for audit,
#   * materially-different normalization — strings compared stripped,
#   * empty input → no-value Resolution (not an error); single statement
#     passthrough; mixed (object_id, property) input → ValueError,
#   * per-(object_type, property) override beats the global ladder; classes
#     omitted from an override rank at a shared bottom tier,
#   * determinism — seeded manual shuffle loop (50 iterations): any input
#     order → an identical Resolution, including loser order.

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

import pytest

from pocketpaw.fabric.models import Statement
from pocketpaw.fabric.resolver import Resolution, resolve
from pocketpaw.fabric.trust import (
    DEFAULT_LADDER,
    TrustOverride,
    TrustRules,
    default_trust_rules,
)

T0 = datetime(2026, 7, 1, 12, 0, 0)

_counter = 0


def make_stmt(
    *,
    value: Any,
    writer_class: str = "connector",
    observed_at: datetime = T0,
    recorded_at: datetime = T0,
    rank: str = "normal",
    pinned: bool = False,
    valid_to: datetime | None = None,
    stmt_id: str | None = None,
    object_id: str = "obj-1",
    property: str = "email",
) -> Statement:
    """Statement factory with deterministic ids (stm-0001, stm-0002, ...)."""
    global _counter
    _counter += 1
    return Statement(
        id=stmt_id or f"stm-{_counter:04d}",
        object_id=object_id,
        property=property,
        value=value,
        source_ref_id="src-test",
        writer_class=writer_class,  # type: ignore[arg-type]
        observed_at=observed_at,
        recorded_at=recorded_at,
        valid_from=observed_at,
        valid_to=valid_to,
        rank=rank,  # type: ignore[arg-type]
        pinned=pinned,
    )


@pytest.fixture()
def rules() -> TrustRules:
    return default_trust_rules()


# ---------------------------------------------------------------- ladder rungs


@pytest.mark.parametrize(
    ("stronger", "weaker"),
    [
        ("human", "connector"),
        ("connector", "mirror"),
        ("mirror", "agent"),
        ("agent", "inferred"),
    ],
)
def test_every_ladder_rung_stronger_beats_weaker(rules, stronger, weaker):
    """Each adjacent rung: the stronger class wins even when the weaker
    statement is NEWER (tier beats recency across tiers)."""
    strong = make_stmt(value=f"{stronger}-val", writer_class=stronger, observed_at=T0)
    weak = make_stmt(
        value=f"{weaker}-val",
        writer_class=weaker,
        observed_at=T0 + timedelta(days=30),
    )
    res = resolve([weak, strong], rules)
    assert res.winner_statement is strong
    assert res.value == f"{stronger}-val"
    assert res.losers == [weak]
    # Different tiers → rankable, even though the values differ.
    assert res.unresolvable is False
    assert res.is_disputed is True


def test_full_ladder_all_five_classes(rules):
    """All five classes at once: human wins, losers ordered by the ladder."""
    stmts = [
        make_stmt(value=wc, writer_class=wc)
        for wc in ["inferred", "agent", "mirror", "connector", "human"]
    ]
    res = resolve(stmts, rules)
    assert res.value == "human"
    assert [s.writer_class for s in res.losers] == ["connector", "mirror", "agent", "inferred"]


# ------------------------------------------------------------------- pinning


def test_single_pin_beats_stronger_unpinned_tier(rules):
    """A pinned inferred statement beats an unpinned human one."""
    human = make_stmt(value="human-val", writer_class="human")
    pinned = make_stmt(value="pinned-val", writer_class="inferred", pinned=True)
    res = resolve([human, pinned], rules)
    assert res.winner_statement is pinned
    assert res.value == "pinned-val"
    assert res.losers == [human]
    assert res.unresolvable is False


def test_multiple_pins_newest_recorded_at_wins_and_disputes(rules):
    """Multiple pins: newest recorded_at wins deterministically; disputed."""
    old_pin = make_stmt(value="old", pinned=True, recorded_at=T0)
    new_pin = make_stmt(value="new", pinned=True, recorded_at=T0 + timedelta(hours=1))
    res = resolve([old_pin, new_pin], rules)
    assert res.winner_statement is new_pin
    assert res.is_disputed is True
    assert res.unresolvable is False  # a pin is authoritative
    assert res.losers == [old_pin]


def test_multiple_pins_same_value_still_disputed(rules):
    """Two pins agreeing on the value is still a curation conflict."""
    a = make_stmt(value="same", pinned=True, recorded_at=T0)
    b = make_stmt(value="same", pinned=True, recorded_at=T0 + timedelta(hours=1))
    res = resolve([a, b], rules)
    assert res.value == "same"
    assert res.is_disputed is True


def test_multiple_pins_equal_recorded_at_id_tiebreak(rules):
    """Equal recorded_at among pins → lexicographically smallest id wins."""
    b = make_stmt(value="b", pinned=True, stmt_id="stm-bbbb")
    a = make_stmt(value="a", pinned=True, stmt_id="stm-aaaa")
    res = resolve([b, a], rules)
    assert res.winner_statement is a


def test_deprecated_pin_does_not_short_circuit(rules):
    """A pinned but deprecated statement is filtered in step 1."""
    dead_pin = make_stmt(value="dead", pinned=True, rank="deprecated")
    normal = make_stmt(value="live", writer_class="inferred")
    res = resolve([dead_pin, normal], rules)
    assert res.winner_statement is normal
    assert res.value == "live"
    assert res.losers == []


# --------------------------------------------------------- deprecated exclusion


def test_deprecated_excluded_entirely(rules):
    """A deprecated human statement loses to a normal inferred one, and is
    not even listed among the losers."""
    dead = make_stmt(value="dead", writer_class="human", rank="deprecated")
    live = make_stmt(value="live", writer_class="inferred")
    res = resolve([dead, live], rules)
    assert res.winner_statement is live
    assert dead not in res.losers
    assert res.is_disputed is False  # the deprecated value doesn't count


def test_all_deprecated_returns_no_value(rules):
    dead1 = make_stmt(value="x", rank="deprecated")
    dead2 = make_stmt(value="y", rank="deprecated")
    res = resolve([dead1, dead2], rules)
    assert res.winner_statement is None
    assert res.value is None
    assert res.losers == []
    assert res.is_disputed is False
    assert res.unresolvable is False


# ------------------------------------------------- recency + tiebreak in tier


def test_recency_within_tier_uses_observed_at_not_recorded_at(rules):
    """The OLDER-recorded but NEWER-observed statement wins: recency is on
    observed_at, never ingest time."""
    stale = make_stmt(
        value="stale",
        observed_at=T0,
        recorded_at=T0 + timedelta(days=5),  # ingested later
    )
    fresh = make_stmt(
        value="fresh",
        observed_at=T0 + timedelta(days=2),
        recorded_at=T0,  # ingested earlier
    )
    res = resolve([stale, fresh], rules)
    assert res.winner_statement is fresh


def test_id_tiebreak_same_tier_same_observed_at(rules):
    """Same tier, same observed_at, same value → smallest id wins; no
    dispute, no unresolvable."""
    b = make_stmt(value="v", stmt_id="stm-bbbb")
    a = make_stmt(value="v", stmt_id="stm-aaaa")
    res = resolve([b, a], rules)
    assert res.winner_statement is a
    assert res.is_disputed is False
    assert res.unresolvable is False


# ------------------------------- rank + validity within tier (CHANGE verb)


def test_change_simulation_new_preferred_open_beats_old_normal_closed(rules):
    """FST-5's CHANGE verb: the old statement gets valid_to closed, the new
    one lands preferred + open. Same tier, close in time, different values —
    resolves CLEANLY: new wins, no dispute, no un-rankability."""
    old = make_stmt(
        value="old@a.com",
        observed_at=T0,
        valid_to=T0 + timedelta(minutes=5),  # closed by the CHANGE
    )
    new = make_stmt(
        value="new@b.com",
        observed_at=T0 + timedelta(minutes=5),  # inside the 24h epsilon
        rank="preferred",
    )
    res = resolve([old, new], rules)
    assert res.winner_statement is new
    assert res.value == "new@b.com"
    assert res.is_disputed is False  # closed loser = resolved history
    assert res.unresolvable is False  # rank + validity differences ARE a ranking
    assert res.losers == [old]  # ... but it stays in losers[] for audit


def test_preferred_beats_normal_within_tier_despite_older_observed_at(rules):
    """Within a tier, rank outranks recency: an OLDER preferred statement
    beats a newer normal one. Both open + materially different → still a
    live dispute, but rankable (rank difference IS a ranking)."""
    pref = make_stmt(value="pref", rank="preferred", observed_at=T0)
    norm = make_stmt(value="norm", observed_at=T0 + timedelta(hours=1))
    res = resolve([norm, pref], rules)
    assert res.winner_statement is pref
    assert res.is_disputed is True  # open normal loser materially differs
    assert res.unresolvable is False  # different ranks → resolvable


def test_low_tier_preferred_loses_to_high_tier_normal(rules):
    """Rank never crosses tiers: an inferred writer marking itself
    preferred must NOT beat a normal human statement."""
    sneaky = make_stmt(value="sneaky", writer_class="inferred", rank="preferred")
    human = make_stmt(value="human-val", writer_class="human")
    res = resolve([sneaky, human], rules)
    assert res.winner_statement is human
    assert res.value == "human-val"


def test_open_validity_beats_closed_despite_older_observed_at(rules):
    """Within tier + rank, validity outranks recency: an OLDER open
    statement beats a newer closed one; the closed loser doesn't dispute."""
    open_stmt = make_stmt(value="open-val", observed_at=T0)
    closed = make_stmt(
        value="closed-val",
        observed_at=T0 + timedelta(hours=1),
        valid_to=T0 + timedelta(hours=2),
    )
    res = resolve([closed, open_stmt], rules)
    assert res.winner_statement is open_stmt
    assert res.is_disputed is False  # closed loser excluded from disputes
    assert res.unresolvable is False  # validity difference → resolvable


def test_all_closed_statements_still_resolve(rules):
    """Reads never block: when EVERY survivor is closed, the newest-observed
    closed statement still wins (closed is demoted, never dropped)."""
    older = make_stmt(value="older", observed_at=T0, valid_to=T0 + timedelta(days=1))
    newer = make_stmt(
        value="newer",
        observed_at=T0 + timedelta(hours=1),
        valid_to=T0 + timedelta(days=1),
    )
    res = resolve([older, newer], rules)
    assert res.winner_statement is newer
    assert res.value == "newer"
    assert res.is_disputed is False  # every loser is closed history
    assert res.unresolvable is False  # winner not open → never un-rankable


# -------------------------------------------------------------- unresolvable


def test_unresolvable_same_tier_close_observed_different_values(rules):
    """Same tier, 1h apart (inside the 24h default epsilon), materially
    different values → unresolvable, but a provisional winner IS returned
    (the newer one) — reads never block."""
    older = make_stmt(value="alice@a.com", observed_at=T0)
    newer = make_stmt(value="alice@b.com", observed_at=T0 + timedelta(hours=1))
    res = resolve([older, newer], rules)
    assert res.unresolvable is True
    assert res.is_disputed is True
    assert res.winner_statement is newer  # deterministic provisional winner
    assert res.value == "alice@b.com"


def test_unresolvable_same_rank_preferred_pair(rules):
    """Same rank means SAME rank — two preferred statements, same tier,
    both open, close in time, different values → un-rankable too."""
    a = make_stmt(value="x", rank="preferred", observed_at=T0)
    b = make_stmt(value="y", rank="preferred", observed_at=T0 + timedelta(hours=1))
    res = resolve([a, b], rules)
    assert res.unresolvable is True
    assert res.winner_statement is b


def test_resolvable_when_gap_exceeds_epsilon(rules):
    """Same tier, different values, 48h apart (> 24h epsilon) → recency is
    trusted: disputed but NOT unresolvable."""
    older = make_stmt(value="old", observed_at=T0)
    newer = make_stmt(value="new", observed_at=T0 + timedelta(hours=48))
    res = resolve([older, newer], rules)
    assert res.winner_statement is newer
    assert res.is_disputed is True
    assert res.unresolvable is False


def test_recency_epsilon_is_configurable():
    """The closeness window comes from the rules, not a constant."""
    older = make_stmt(value="old", observed_at=T0)
    newer = make_stmt(value="new", observed_at=T0 + timedelta(minutes=2))
    tight = TrustRules(recency_epsilon_seconds=60.0)
    wide = TrustRules(recency_epsilon_seconds=600.0)
    assert resolve([older, newer], tight).unresolvable is False
    assert resolve([older, newer], wide).unresolvable is True


def test_same_values_close_in_time_not_unresolvable(rules):
    """Agreement inside the window is fine — only material difference
    triggers un-rankability."""
    a = make_stmt(value="same", observed_at=T0)
    b = make_stmt(value="same", observed_at=T0 + timedelta(hours=1))
    res = resolve([a, b], rules)
    assert res.unresolvable is False
    assert res.is_disputed is False


def test_lower_tier_conflict_does_not_make_unresolvable(rules):
    """A materially different value from a LOWER tier, however close in
    time, never makes the resolution un-rankable."""
    human = make_stmt(value="human-val", writer_class="human", observed_at=T0)
    conn = make_stmt(value="conn-val", writer_class="connector", observed_at=T0)
    res = resolve([human, conn], rules)
    assert res.winner_statement is human
    assert res.is_disputed is True
    assert res.unresolvable is False


# ---------------------------------------------------- materially different


def test_strings_compared_stripped_not_disputed(rules):
    """' Acme ' and 'Acme' are the SAME value after normalization."""
    a = make_stmt(value=" Acme ", observed_at=T0)
    b = make_stmt(value="Acme", observed_at=T0 + timedelta(hours=1))
    res = resolve([a, b], rules)
    assert res.is_disputed is False
    assert res.unresolvable is False


def test_case_difference_is_material(rules):
    """Normalization is strip-only: case differences ARE material."""
    a = make_stmt(value="acme", observed_at=T0)
    b = make_stmt(value="Acme", observed_at=T0 + timedelta(hours=1))
    res = resolve([a, b], rules)
    assert res.is_disputed is True


# --------------------------------------------------------------- edge inputs


def test_empty_statement_list_returns_no_value_resolution(rules):
    res = resolve([], rules)
    assert isinstance(res, Resolution)
    assert res.winner_statement is None
    assert res.value is None
    assert res.losers == []
    assert res.is_disputed is False
    assert res.unresolvable is False


def test_single_statement_passthrough(rules):
    only = make_stmt(value=42, writer_class="inferred")
    res = resolve([only], rules)
    assert res.winner_statement is only
    assert res.value == 42
    assert res.losers == []
    assert res.is_disputed is False
    assert res.unresolvable is False


def test_mixed_property_input_raises(rules):
    a = make_stmt(value="x", property="email")
    b = make_stmt(value="y", property="phone")
    with pytest.raises(ValueError, match="ONE"):
        resolve([a, b], rules)


def test_mixed_object_id_input_raises(rules):
    a = make_stmt(value="x", object_id="obj-1")
    b = make_stmt(value="y", object_id="obj-2")
    with pytest.raises(ValueError, match="ONE"):
        resolve([a, b], rules)


# ------------------------------------------------------------------ overrides


def test_per_property_override_beats_global_ladder():
    """(Customer, email) trusts the connector over the human; every other
    (object_type, property) keeps the global ladder."""
    rules = TrustRules(
        overrides=[
            TrustOverride(
                object_type="Customer",
                property="email",
                ladder=["connector", "human", "mirror", "agent", "inferred"],
            )
        ]
    )
    human = make_stmt(value="human-val", writer_class="human")
    conn = make_stmt(value="conn-val", writer_class="connector")

    # Override applies: connector outranks human for Customer.email.
    res = resolve([human, conn], rules, object_type="Customer")
    assert res.winner_statement is conn

    # Different object_type → global ladder → human wins.
    res = resolve([human, conn], rules, object_type="Deal")
    assert res.winner_statement is human

    # Different property → global ladder → human wins.
    ph = make_stmt(value="h", writer_class="human", property="phone")
    pc = make_stmt(value="c", writer_class="connector", property="phone")
    res = resolve([ph, pc], rules, object_type="Customer")
    assert res.winner_statement is ph

    # No object_type given → overrides never match → global ladder.
    res = resolve([human, conn], rules)
    assert res.winner_statement is human


def test_override_omitted_classes_rank_bottom():
    """Classes missing from an override ladder share the bottom tier —
    they are demoted, not dropped."""
    rules = TrustRules(
        overrides=[TrustOverride(object_type="Customer", property="email", ladder=["connector"])]
    )
    human = make_stmt(value="h", writer_class="human", observed_at=T0 + timedelta(days=1))
    conn = make_stmt(value="c", writer_class="connector", observed_at=T0)
    res = resolve([human, conn], rules, object_type="Customer")
    assert res.winner_statement is conn  # listed class beats unlisted
    assert res.losers == [human]  # unlisted class survives as a loser


def test_default_trust_rules_shape():
    rules = default_trust_rules()
    assert rules.ladder == DEFAULT_LADDER
    assert rules.ladder == ["human", "connector", "mirror", "agent", "inferred"]
    assert rules.overrides == []
    assert rules.recency_epsilon_seconds == 24 * 60 * 60.0
    assert rules.freshness_ttl_classes == {}  # FST-7 placeholder, empty


# --------------------------------------------------------------- determinism


def test_resolution_deterministic_under_input_shuffle(rules):
    """Property-based determinism: 50 seeded shuffles of a gnarly statement
    set (pins, deprecated, preferred/normal ranks, open/closed validity,
    ties, near-ties) all produce the IDENTICAL Resolution — winner, value,
    flags, and loser ORDER — across every leg of the sort key."""
    stmts = [
        make_stmt(value="a", writer_class="human", observed_at=T0),
        make_stmt(value="b", writer_class="human", observed_at=T0 + timedelta(hours=1)),
        # preferred vs the two normal humans → exercises the rank leg
        make_stmt(value="h", writer_class="human", rank="preferred", observed_at=T0),
        # closed human → exercises the validity leg
        make_stmt(
            value="i",
            writer_class="human",
            observed_at=T0 + timedelta(hours=2),
            valid_to=T0 + timedelta(days=1),
        ),
        make_stmt(value="c", writer_class="connector", observed_at=T0 + timedelta(days=2)),
        make_stmt(value="d", writer_class="mirror"),
        make_stmt(value="e", writer_class="agent", rank="deprecated"),
        make_stmt(value="f", writer_class="inferred"),
        # same tier + same observed_at as "f" → exercises the id tiebreak
        make_stmt(value="g", writer_class="inferred"),
    ]
    baseline = resolve(list(stmts), rules).model_dump()
    rng = random.Random(1337)
    for _ in range(50):
        shuffled = list(stmts)
        rng.shuffle(shuffled)
        assert resolve(shuffled, rules).model_dump() == baseline


def test_resolution_deterministic_with_pins_shuffled(rules):
    """Shuffle determinism on the pinned path too (multiple pins)."""
    stmts = [
        make_stmt(value="p1", pinned=True, recorded_at=T0),
        make_stmt(value="p2", pinned=True, recorded_at=T0 + timedelta(hours=2)),
        make_stmt(value="h", writer_class="human"),
        make_stmt(value="i", writer_class="inferred"),
    ]
    baseline = resolve(list(stmts), rules).model_dump()
    rng = random.Random(4242)
    for _ in range(50):
        shuffled = list(stmts)
        rng.shuffle(shuffled)
        assert resolve(shuffled, rules).model_dump() == baseline


def test_resolve_does_not_mutate_input(rules):
    """Purity: the caller's list is not reordered or altered."""
    a = make_stmt(value="a", writer_class="inferred")
    b = make_stmt(value="b", writer_class="human")
    original = [a, b]
    resolve(original, rules)
    assert original == [a, b]
