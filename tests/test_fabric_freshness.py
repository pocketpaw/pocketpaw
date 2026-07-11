# tests/test_fabric_freshness.py
# Created: 2026-07-11 (FST-7 — freshness: TTL classes, the pure tri-state,
#   within-family staleness demotion, provenance on reads).
#
# Part 1 — trust.py: the named TTL classes (volatile=1d, default=30d,
#   stable=365d), per-(object_type, property) class overrides mirroring the
#   ladder-override style, the layered max_age_for lookup (instance dict over
#   module defaults, unknown class → "default"), and the pure
#   freshness(observed_at, max_age_seconds, now) tri-state including the
#   UTC-normalization convention (naive = UTC at the comparison boundary).
#   Back-compat: default_trust_rules() still serializes with an EMPTY
#   freshness_ttl_classes dict (the FST-2 wire format).
# Part 2 — resolver.py: within-FAMILY staleness demotion at tier selection —
#   the FST-5 flagged case (stale connector seed loses to the fresh mirror
#   value), demotion never crossing families (stale connector still beats
#   fresh inferred), within-tier demotion across the rank leg, aging does
#   NOT demote, all-stale keeps ladder order, a lone stale value still wins,
#   pins ignore freshness, per-property TTL-class overrides steer demotion,
#   freshness-demoted losers are RESOLVED (no dispute / un-rankable noise),
#   and now=None is byte-identical legacy behavior.

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from pocketpaw.fabric.models import Statement
from pocketpaw.fabric.resolver import resolve
from pocketpaw.fabric.trust import (
    DEFAULT_FRESHNESS_CLASS,
    DEFAULT_FRESHNESS_TTL_CLASSES,
    FreshnessOverride,
    TrustRules,
    default_trust_rules,
    freshness,
)

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
DAY = 86400.0


# ------------------------------------------------------------- TTL classes


def test_default_ttl_classes_named_values():
    """The three named volatility classes carry the documented max-ages."""
    assert DEFAULT_FRESHNESS_TTL_CLASSES == {
        "volatile": 86400.0,  # 1 day
        "default": 2592000.0,  # 30 days
        "stable": 31536000.0,  # 365 days
    }


def test_default_trust_rules_wire_format_unchanged():
    """FST-2 back-compat: the INSTANCE dict stays empty (module defaults
    apply through max_age_for) and no freshness overrides ship by default."""
    rules = default_trust_rules()
    assert rules.freshness_ttl_classes == {}
    assert rules.freshness_overrides == []


def test_ttl_class_for_unassigned_property_is_default():
    rules = default_trust_rules()
    assert rules.ttl_class_for("person", "email") == DEFAULT_FRESHNESS_CLASS


def test_ttl_class_for_override_first_exact_match_wins():
    rules = TrustRules(
        freshness_overrides=[
            FreshnessOverride(object_type="person", property="status", ttl_class="volatile"),
            FreshnessOverride(object_type="person", property="status", ttl_class="stable"),
            FreshnessOverride(object_type="company", property="founded", ttl_class="stable"),
        ]
    )
    assert rules.ttl_class_for("person", "status") == "volatile"  # first match
    assert rules.ttl_class_for("company", "founded") == "stable"
    assert rules.ttl_class_for("person", "founded") == "default"  # both must match


def test_ttl_class_for_none_object_type_never_matches_override():
    rules = TrustRules(
        freshness_overrides=[
            FreshnessOverride(object_type="person", property="status", ttl_class="volatile"),
        ]
    )
    assert rules.ttl_class_for(None, "status") == DEFAULT_FRESHNESS_CLASS


def test_max_age_for_resolves_named_classes_from_module_defaults():
    rules = TrustRules(
        freshness_overrides=[
            FreshnessOverride(object_type="person", property="status", ttl_class="volatile"),
            FreshnessOverride(object_type="company", property="founded", ttl_class="stable"),
        ]
    )
    assert rules.max_age_for("person", "status") == 86400.0
    assert rules.max_age_for("company", "founded") == 31536000.0
    assert rules.max_age_for("person", "email") == 2592000.0  # unassigned → default


def test_max_age_for_instance_dict_overrides_module_default():
    """The instance dict is an OVERRIDE layer: redefining a class name wins
    over the module default for that class."""
    rules = TrustRules(
        freshness_ttl_classes={"volatile": 3600.0},
        freshness_overrides=[
            FreshnessOverride(object_type="person", property="status", ttl_class="volatile"),
        ],
    )
    assert rules.max_age_for("person", "status") == 3600.0
    # Untouched classes still come from the module defaults.
    assert rules.max_age_for("person", "email") == 2592000.0


def test_max_age_for_unknown_class_falls_back_to_default():
    """A typo'd class name degrades to the default class, never raises."""
    rules = TrustRules(
        freshness_overrides=[
            FreshnessOverride(object_type="person", property="status", ttl_class="nope"),
        ]
    )
    assert rules.max_age_for("person", "status") == 2592000.0


def test_max_age_for_unknown_class_uses_instance_default_when_redefined():
    rules = TrustRules(
        freshness_ttl_classes={"default": 100.0},
        freshness_overrides=[
            FreshnessOverride(object_type="person", property="status", ttl_class="nope"),
        ],
    )
    assert rules.max_age_for("person", "status") == 100.0
    assert rules.max_age_for("person", "email") == 100.0


# ------------------------------------------------------- freshness tri-state


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (0.0, "fresh"),
        (DAY, "fresh"),  # exactly max_age → still fresh
        (DAY + 1, "aging"),
        (2 * DAY, "aging"),  # exactly 2*max_age → still aging
        (2 * DAY + 1, "stale"),
        (100 * DAY, "stale"),
    ],
)
def test_freshness_boundaries(age_seconds, expected):
    observed = NOW - timedelta(seconds=age_seconds)
    assert freshness(observed, DAY, NOW) == expected


def test_freshness_future_observed_at_is_fresh():
    """Negative age (source clock ahead) classifies fresh, never raises."""
    assert freshness(NOW + timedelta(hours=3), DAY, NOW) == "fresh"


def test_freshness_naive_inputs_are_read_as_utc():
    """THE convention: a naive datetime is interpreted AS UTC at the
    comparison boundary — a naive-UTC stamp (SQLite datetime('now')) and the
    same instant written as aware-UTC classify identically."""
    naive_utc_observed = datetime(2026, 7, 11, 12, 0, 0) - timedelta(days=1, seconds=1)
    aware_observed = naive_utc_observed.replace(tzinfo=UTC)
    assert freshness(naive_utc_observed, DAY, NOW) == "aging"
    assert freshness(aware_observed, DAY, NOW) == freshness(naive_utc_observed, DAY, NOW)


def test_freshness_naive_now_matches_aware_now():
    """A naive-UTC ``now`` (e.g. parsed back from datetime('now')) and the
    aware datetime.now(timezone.utc) convention agree."""
    naive_now = datetime(2026, 7, 11, 12, 0, 0)
    observed = datetime(2026, 7, 8, 12, 0, 0)
    assert freshness(observed, DAY, naive_now) == freshness(observed, DAY, NOW) == "stale"


def test_freshness_mixed_naive_and_aware_never_raises_and_orders_correctly():
    """Naive-local vs naive-UTC: under the naive-as-UTC rule both are read on
    ONE axis, so ages stay monotonic — an earlier naive stamp is always at
    least as decayed as a later one, and mixing naive/aware never raises."""
    older_naive = datetime(2026, 7, 1, 12, 0, 0)  # e.g. a naive-local default
    newer_naive = datetime(2026, 7, 11, 11, 0, 0)  # e.g. a naive-UTC stamp
    order = {"fresh": 0, "aging": 1, "stale": 2}
    older_state = freshness(older_naive, DAY, NOW)  # aware now
    newer_state = freshness(newer_naive, DAY, NOW)
    assert order[older_state] >= order[newer_state]
    assert older_state == "stale"
    assert newer_state == "fresh"


def test_freshness_aware_non_utc_input_converts():
    """An aware non-UTC datetime is CONVERTED (not stripped): +05:30 wall
    time 36h ago is 36h old regardless of its zone."""
    ist = timezone(timedelta(hours=5, minutes=30))
    observed = (NOW - timedelta(hours=36)).astimezone(ist)
    assert freshness(observed, DAY, NOW) == "aging"


# ---------------------------------------- Part 2: within-family demotion

_counter = 0


def fstmt(
    *,
    value: Any,
    writer_class: str = "connector",
    observed_at: datetime = NOW,
    rank: str = "normal",
    pinned: bool = False,
    object_id: str = "obj-1",
    property: str = "email",
) -> Statement:
    """Statement factory with deterministic ids (fst7-0001, fst7-0002, ...).

    All timestamps in these tests are aware UTC — the freshness/naive
    conventions are covered by the Part 1 unit tests above."""
    global _counter
    _counter += 1
    return Statement(
        id=f"fst7-{_counter:04d}",
        object_id=object_id,
        property=property,
        value=value,
        source_ref_id="src-test",
        writer_class=writer_class,  # type: ignore[arg-type]
        observed_at=observed_at,
        recorded_at=observed_at,
        valid_from=observed_at,
        rank=rank,  # type: ignore[arg-type]
        pinned=pinned,
    )


def test_stale_connector_seed_loses_to_fresh_mirror_value():
    """THE FST-5 FLAGGED CASE: a connector seed observed 90d ago (class
    "default" → max-age 30d → stale beyond 60d) loses to the mirror's fresh
    value when ``now`` is passed, even though the ladder ranks connector >
    mirror — same machine-sync family, so demotion applies. The demoted
    stale loser is RESOLVED: no dispute, no un-rankable noise."""
    stale_seed = fstmt(
        value="old@x.com", writer_class="connector", observed_at=NOW - timedelta(days=90)
    )
    fresh_mirror = fstmt(
        value="new@x.com", writer_class="mirror", observed_at=NOW - timedelta(days=1)
    )
    res = resolve([stale_seed, fresh_mirror], default_trust_rules(), now=NOW)
    assert res.winner_statement is fresh_mirror
    assert res.value == "new@x.com"
    assert res.losers == [stale_seed]
    assert res.is_disputed is False
    assert res.unresolvable is False


def test_stale_connector_does_not_lose_to_fresh_inferred():
    """Demotion NEVER crosses families: a stale connector value still beats
    a fresh inferred value — and the cross-family fresh loser keeps the
    normal dispute semantics (no freshness exclusion)."""
    stale_conn = fstmt(
        value="old@x.com", writer_class="connector", observed_at=NOW - timedelta(days=90)
    )
    fresh_inferred = fstmt(
        value="new@x.com", writer_class="inferred", observed_at=NOW - timedelta(days=1)
    )
    res = resolve([stale_conn, fresh_inferred], default_trust_rules(), now=NOW)
    assert res.winner_statement is stale_conn
    assert res.value == "old@x.com"
    assert res.is_disputed is True  # open, materially different, not resolved by freshness
    assert res.unresolvable is False


def test_now_none_skips_freshness_entirely():
    """Back-compat: without ``now`` the FST-5 flagged pair resolves exactly
    as pre-FST-7 — the ladder puts connector above mirror, stale or not."""
    stale_seed = fstmt(
        value="old@x.com", writer_class="connector", observed_at=NOW - timedelta(days=90)
    )
    fresh_mirror = fstmt(
        value="new@x.com", writer_class="mirror", observed_at=NOW - timedelta(days=1)
    )
    res = resolve([stale_seed, fresh_mirror], default_trust_rules())
    assert res.winner_statement is stale_seed
    assert res.value == "old@x.com"
    assert res.is_disputed is True  # normal FST-2 semantics, no freshness pass


def test_stale_preferred_loses_to_fresh_normal_within_tier():
    """Within one tier the rank leg puts preferred first — but a STALE
    preferred still loses to a fresh normal from the same family."""
    stale_preferred = fstmt(
        value="old",
        writer_class="connector",
        observed_at=NOW - timedelta(days=90),
        rank="preferred",
    )
    fresh_normal = fstmt(value="new", writer_class="connector", observed_at=NOW - timedelta(days=1))
    res = resolve([stale_preferred, fresh_normal], default_trust_rules(), now=NOW)
    assert res.winner_statement is fresh_normal
    assert res.is_disputed is False


def test_aging_winner_is_not_demoted():
    """Only STALE demotes: an aging top candidate (40d on the 30d default
    class) keeps the win over a fresh same-family value."""
    aging_conn = fstmt(value="old", writer_class="connector", observed_at=NOW - timedelta(days=40))
    fresh_mirror = fstmt(value="new", writer_class="mirror", observed_at=NOW - timedelta(days=1))
    res = resolve([aging_conn, fresh_mirror], default_trust_rules(), now=NOW)
    assert res.winner_statement is aging_conn
    assert res.is_disputed is True  # fresh loser is NOT freshness-resolved


def test_all_stale_same_family_keeps_ladder_order():
    """No non-stale candidate in the family → no demotion: ladder order
    stands even though everything is stale (reads never block)."""
    stale_conn = fstmt(value="a", writer_class="connector", observed_at=NOW - timedelta(days=90))
    stale_mirror = fstmt(value="b", writer_class="mirror", observed_at=NOW - timedelta(days=80))
    res = resolve([stale_conn, stale_mirror], default_trust_rules(), now=NOW)
    assert res.winner_statement is stale_conn
    assert res.is_disputed is True  # winner stale → no freshness exclusion


def test_lone_stale_value_still_wins():
    stale = fstmt(value="old", writer_class="connector", observed_at=NOW - timedelta(days=400))
    res = resolve([stale], default_trust_rules(), now=NOW)
    assert res.winner_statement is stale
    assert res.value == "old"


def test_pinned_stale_still_wins_with_now():
    """A pin is authoritative — the pinned short-circuit runs before the
    ladder path and ignores freshness entirely."""
    stale_pinned = fstmt(
        value="pinned", writer_class="mirror", observed_at=NOW - timedelta(days=400), pinned=True
    )
    fresh_conn = fstmt(value="new", writer_class="connector", observed_at=NOW - timedelta(days=1))
    res = resolve([stale_pinned, fresh_conn], default_trust_rules(), now=NOW)
    assert res.winner_statement is stale_pinned
    assert res.value == "pinned"


def test_freshness_class_override_steers_demotion():
    """The per-(object_type, property) TTL class drives the demotion: a 3d-old
    connector value is stale on the "volatile" class (1d) but fresh on the
    default class (30d)."""
    conn_3d = fstmt(
        value="old",
        writer_class="connector",
        observed_at=NOW - timedelta(days=3),
        object_id="obj-2",
        property="status",
    )
    mirror_1h = fstmt(
        value="new",
        writer_class="mirror",
        observed_at=NOW - timedelta(hours=1),
        object_id="obj-2",
        property="status",
    )
    volatile_rules = TrustRules(
        freshness_overrides=[
            FreshnessOverride(object_type="person", property="status", ttl_class="volatile"),
        ]
    )
    res = resolve([conn_3d, mirror_1h], volatile_rules, object_type="person", now=NOW)
    assert res.winner_statement is mirror_1h  # stale on volatile → demoted
    res_default = resolve(
        [conn_3d, mirror_1h], default_trust_rules(), object_type="person", now=NOW
    )
    assert res_default.winner_statement is conn_3d  # fresh on default → ladder stands


def test_boundary_straddling_pair_resolves_with_now():
    """Two same-tier same-rank connector values within the 24h epsilon would
    be UN-RANKABLE under FST-2 — but when one side is stale and the winner is
    not, the stale side is freshness-resolved: no unresolvable, no dispute."""
    just_stale = fstmt(
        value="a", writer_class="connector", observed_at=NOW - timedelta(days=60, hours=2)
    )
    just_aging = fstmt(
        value="b", writer_class="connector", observed_at=NOW - timedelta(days=59, hours=12)
    )
    legacy = resolve([just_stale, just_aging], default_trust_rules())
    assert legacy.unresolvable is True  # the FST-2 semantics, unchanged without now
    res = resolve([just_stale, just_aging], default_trust_rules(), now=NOW)
    assert res.winner_statement is just_aging  # newer → ranked first, no demotion needed
    assert res.unresolvable is False
    assert res.is_disputed is False


# ---------------------------------------------------------------------------
# Part 3 — the provenance read surface (store.get_object_provenance, FST-7)
# ---------------------------------------------------------------------------


async def _tracked_crm_object(tmp_path, monkeypatch):
    """A store in shadow mode with one object whose ``arr`` property is
    statement-tracked (promoted by a second-source agent write)."""
    from pocketpaw.fabric.store import FabricStore

    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: "shadow")
    store = FabricStore(tmp_path / "fabric.db")
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(
        obj_type.id, {"name": "Acme", "arr": 120}, source_connector="crm", source_id="c-1"
    )
    await store.update_object(obj.id, {"arr": 150}, writer_class="agent", source_session_id="s1")
    return store, obj.id


async def test_get_object_provenance_reports_tracked_property(tmp_path, monkeypatch):
    """The opt-in read surface: a tracked property reports dispute state,
    freshness, statement count, and the WINNER's writer + source summary;
    untracked properties (``name``) don't appear at all."""
    store, obj_id = await _tracked_crm_object(tmp_path, monkeypatch)

    prov = await store.get_object_provenance(obj_id)

    assert set(prov.keys()) == {"arr"}  # "name" untracked -> absent
    entry = prov["arr"]
    assert entry["statements"] == 2
    assert entry["disputed"] is True  # open agent rival vs connector seed
    assert entry["freshness"] == "fresh"  # both written moments ago
    winner = entry["winner"]
    assert winner["writer_class"] == "connector"  # ladder: connector > agent
    assert winner["source"]["kind"] == "connector_run"
    assert winner["source"]["connector"] == "crm"


async def test_get_object_provenance_empty_when_nothing_tracked(tmp_path, monkeypatch):
    from pocketpaw.fabric.store import FabricStore

    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: "shadow")
    store = FabricStore(tmp_path / "fabric.db")
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(obj_type.id, {"name": "Acme"}, source_connector="crm")

    assert await store.get_object_provenance(obj.id) == {}


async def test_resolution_exposes_winner_freshness(tmp_path, monkeypatch):
    """The Resolution model carries the winner's tri-state when ``now`` is
    passed (the divergence line + read surface consume it) and None for
    legacy ``now=None`` callers."""
    stmts = [
        fstmt(value="a@b.co", writer_class="connector", observed_at=NOW - timedelta(days=2)),
    ]
    with_now = resolve(stmts, default_trust_rules(), now=NOW)
    assert with_now.winner_freshness == "fresh"
    legacy = resolve(stmts, default_trust_rules())
    assert legacy.winner_freshness is None
