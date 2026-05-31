# tests/ee/foresight/test_tier_pool.py
# Created: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
#
# Pin the tier-pool contract (RFC §7.3 + §10):
#   - TierMix rejects out-of-band fractions.
#   - TierMix rejects triples that don't sum to 1.0.
#   - locked_default() == (0.05, 0.15, 0.80).
#   - is_default() detects the locked mix.
#   - build_tier_pool produces a list of length n_personas.
#   - The mix composition matches the requested fractions (within
#     rounding).
#   - The shuffle is deterministic when seeded.
#   - tier_distribution counts a pool correctly.
#   - Factory failures surface with the tier name in the error.

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pocketpaw_ee.foresight.llm.tier_pool import (
    TierMix,
    build_tier_pool,
    tier_distribution,
)

# --- TierMix ---------------------------------------------------------


def test_tier_mix_default_is_5_15_80():
    mix = TierMix()
    assert mix.premium == 0.05
    assert mix.mid == 0.15
    assert mix.tail == 0.80


def test_tier_mix_locked_default_matches_constructor():
    assert TierMix.locked_default() == TierMix()


def test_tier_mix_is_default_recognizes_locked():
    assert TierMix.locked_default().is_default()


def test_tier_mix_is_default_recognizes_override():
    assert not TierMix(premium=0.10, mid=0.20, tail=0.70).is_default()


def test_tier_mix_rejects_negative_fraction():
    with pytest.raises(ValueError, match="must be in"):
        TierMix(premium=-0.1, mid=0.3, tail=0.8)


def test_tier_mix_rejects_fraction_over_one():
    with pytest.raises(ValueError, match="must be in"):
        TierMix(premium=1.5, mid=0.0, tail=-0.5)


def test_tier_mix_rejects_non_unit_sum():
    with pytest.raises(ValueError, match="must sum to 1.0"):
        TierMix(premium=0.1, mid=0.2, tail=0.5)


def test_tier_mix_accepts_small_float_drift():
    """0.1 + 0.2 = 0.30000000000000004 in floats — must not crash."""
    mix = TierMix(premium=0.1, mid=0.2, tail=0.7)
    assert abs((mix.premium + mix.mid + mix.tail) - 1.0) < 0.001


def test_tier_mix_as_dict_round_trips():
    mix = TierMix(premium=0.10, mid=0.20, tail=0.70)
    assert mix.as_dict() == {"premium": 0.10, "mid": 0.20, "tail": 0.70}


# --- build_tier_pool -------------------------------------------------


@dataclass
class _FakeBackend:
    """Stand-in for a CAMEL BaseModelBackend — carries a tier tag so
    tier_distribution's default markers can detect it.
    """

    _model: str


def _premium_factory():
    return _FakeBackend(_model="claude-sonnet-4-7")


def _mid_factory():
    return _FakeBackend(_model="claude-haiku-4-7")


def _tail_factory():
    return _FakeBackend(_model="llama-3.1-8b-vllm")


def test_build_tier_pool_returns_list_of_correct_length():
    pool = build_tier_pool(
        tier_mix=TierMix.locked_default(),
        n_personas=100,
        premium_factory=_premium_factory,
        mid_factory=_mid_factory,
        tail_factory=_tail_factory,
        seed=42,
    )
    assert isinstance(pool, list)
    assert len(pool) == 100


def test_build_tier_pool_honors_default_5_15_80():
    pool = build_tier_pool(
        tier_mix=TierMix.locked_default(),
        n_personas=100,
        premium_factory=_premium_factory,
        mid_factory=_mid_factory,
        tail_factory=_tail_factory,
        seed=42,
    )
    counts = tier_distribution(pool)
    # 100 personas at 5/15/80: 5 premium / 15 mid / 80 tail
    assert counts["premium"] == 5
    assert counts["mid"] == 15
    assert counts["tail"] == 80


def test_build_tier_pool_honors_override_mix():
    pool = build_tier_pool(
        tier_mix=TierMix(premium=0.10, mid=0.20, tail=0.70),
        n_personas=100,
        premium_factory=_premium_factory,
        mid_factory=_mid_factory,
        tail_factory=_tail_factory,
        seed=42,
    )
    counts = tier_distribution(pool)
    assert counts["premium"] == 10
    assert counts["mid"] == 20
    assert counts["tail"] == 70


def test_build_tier_pool_seeded_shuffle_is_deterministic():
    """Same seed → same persona-id-to-tier assignment."""
    pool_a = build_tier_pool(
        tier_mix=TierMix.locked_default(),
        n_personas=50,
        premium_factory=_premium_factory,
        mid_factory=_mid_factory,
        tail_factory=_tail_factory,
        seed=1234,
    )
    pool_b = build_tier_pool(
        tier_mix=TierMix.locked_default(),
        n_personas=50,
        premium_factory=_premium_factory,
        mid_factory=_mid_factory,
        tail_factory=_tail_factory,
        seed=1234,
    )
    assert [b._model for b in pool_a] == [b._model for b in pool_b]


def test_build_tier_pool_shuffles_so_consecutive_ids_span_tiers():
    """With the locked 5/15/80, the first 10 entries should NOT all be
    tail (which they would be if no shuffle happened).
    """
    pool = build_tier_pool(
        tier_mix=TierMix.locked_default(),
        n_personas=100,
        premium_factory=_premium_factory,
        mid_factory=_mid_factory,
        tail_factory=_tail_factory,
        seed=42,
    )
    first_10 = [b._model for b in pool[:10]]
    # At least one non-tail backend in the first 10.
    assert any("sonnet" in m or "haiku" in m for m in first_10)


def test_build_tier_pool_rejects_zero_personas():
    with pytest.raises(ValueError, match="n_personas must be >= 1"):
        build_tier_pool(
            tier_mix=TierMix.locked_default(),
            n_personas=0,
            premium_factory=_premium_factory,
            mid_factory=_mid_factory,
            tail_factory=_tail_factory,
        )


def test_build_tier_pool_propagates_factory_errors_with_tier_name():
    def _broken():
        raise RuntimeError("oops")

    with pytest.raises(RuntimeError, match="premium"):
        build_tier_pool(
            tier_mix=TierMix.locked_default(),
            n_personas=20,
            premium_factory=_broken,
            mid_factory=_mid_factory,
            tail_factory=_tail_factory,
        )


def test_build_tier_pool_rejects_none_returning_factory():
    def _none_factory():
        return None

    with pytest.raises(ValueError, match="returned None"):
        build_tier_pool(
            tier_mix=TierMix.locked_default(),
            n_personas=20,
            premium_factory=_none_factory,
            mid_factory=_mid_factory,
            tail_factory=_tail_factory,
        )


def test_build_tier_pool_handles_small_population():
    """5 personas at 5/15/80 should land 0 premium / 1 mid / 4 tail."""
    pool = build_tier_pool(
        tier_mix=TierMix.locked_default(),
        n_personas=5,
        premium_factory=_premium_factory,
        mid_factory=_mid_factory,
        tail_factory=_tail_factory,
        seed=0,
    )
    assert len(pool) == 5
    counts = tier_distribution(pool)
    # Rounding: 0.25 → 0 premium; 0.75 → 1 mid; remainder → 4 tail.
    assert counts["premium"] == 0
    assert counts["mid"] == 1
    assert counts["tail"] == 4


# --- tier_distribution ----------------------------------------------


def test_tier_distribution_default_markers_detect_model_name():
    pool = [
        _FakeBackend(_model="claude-sonnet-4-7"),
        _FakeBackend(_model="claude-haiku-4-7"),
        _FakeBackend(_model="llama-3.1-8b"),
    ]
    counts = tier_distribution(pool)
    assert counts == {"premium": 1, "mid": 1, "tail": 1}


def test_tier_distribution_unknown_backends_default_to_tail():
    pool = [_FakeBackend(_model="gpt-4o")]
    counts = tier_distribution(pool)
    assert counts == {"premium": 0, "mid": 0, "tail": 1}


def test_tier_distribution_handles_backends_without_model_attr():
    """A backend without a ``_model`` attr lands in tail."""

    class _NoModel:
        pass

    pool = [_NoModel()]
    counts = tier_distribution(pool)
    assert counts == {"premium": 0, "mid": 0, "tail": 1}


def test_tier_distribution_honors_custom_markers():
    pool = [{"flavor": "premium"}, {"flavor": "tail"}]

    def _is_premium(b):
        return isinstance(b, dict) and b.get("flavor") == "premium"

    counts = tier_distribution(pool, premium_marker=_is_premium)
    assert counts == {"premium": 1, "mid": 0, "tail": 1}
