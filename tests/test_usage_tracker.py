"""Tests for usage_tracker.py — UsageTracker fixes.

[FI] Fix: two bugs in UsageTracker:

1. total_tokens excluded cached_input_tokens.
   In `record()`, total was computed as `input_tokens + output_tokens`,
   silently dropping cached tokens from the count even though they are real
   tokens processed by the model.

2. get_summary() called get_records(limit=10_000) instead of reading all
   records, so any installation with more than 10 000 lifetime records would
   silently produce wrong (understated) aggregation totals.
"""

from __future__ import annotations

import json

import pytest

from pocketpaw.usage_tracker import UsageTracker, _estimate_cost

# ---------------------------------------------------------------------------
# Bug 1 – total_tokens must include cached_input_tokens
# ---------------------------------------------------------------------------


class TestTotalTokensIncludesCachedInput:
    """total_tokens = input + output + cached_input (not just input + output)."""

    def test_total_tokens_with_cached(self, tmp_path):
        tracker = UsageTracker(path=tmp_path / "usage.jsonl")
        rec = tracker.record(
            backend="anthropic",
            model="claude-3-5-sonnet-20241022",
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=200,
        )
        assert rec.total_tokens == 350  # 100 + 50 + 200

    def test_total_tokens_without_cached(self, tmp_path):
        tracker = UsageTracker(path=tmp_path / "usage.jsonl")
        rec = tracker.record(
            backend="openai",
            model="gpt-4o",
            input_tokens=80,
            output_tokens=40,
            cached_input_tokens=0,
        )
        assert rec.total_tokens == 120  # 80 + 40 + 0

    def test_total_tokens_persisted_correctly(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        tracker = UsageTracker(path=path)
        tracker.record(
            backend="anthropic",
            model="claude-3-5-sonnet-20241022",
            input_tokens=10,
            output_tokens=20,
            cached_input_tokens=30,
        )
        line = path.read_text().strip()
        data = json.loads(line)
        assert data["total_tokens"] == 60  # 10 + 20 + 30

    def test_summary_total_tokens_includes_cached(self, tmp_path):
        tracker = UsageTracker(path=tmp_path / "usage.jsonl")
        tracker.record(
            backend="anthropic",
            model="claude-3-5-sonnet-20241022",
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=200,
        )
        tracker.record(
            backend="anthropic",
            model="claude-3-5-sonnet-20241022",
            input_tokens=50,
            output_tokens=25,
            cached_input_tokens=100,
        )
        summary = tracker.get_summary()
        # (100+50+200) + (50+25+100) = 350 + 175 = 525
        assert summary["total_tokens"] == 525
        assert summary["total_cached_input_tokens"] == 300


# ---------------------------------------------------------------------------
# Bug 2 – get_summary() must aggregate ALL records, not just the last 10 000
# ---------------------------------------------------------------------------


class TestSummaryCoversAllRecords:
    """get_summary() should cover every record ever written."""

    def _write_n_records(self, path, n: int) -> None:
        """Write n minimal records directly to the JSONL file."""
        lines = []
        for i in range(n):
            lines.append(
                json.dumps(
                    {
                        "timestamp": f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00",
                        "backend": "openai",
                        "model": "gpt-4o-mini",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cached_input_tokens": 0,
                        "total_tokens": 15,
                        "cost_usd": None,
                        "session_id": "",
                    }
                )
            )
        path.write_text("\n".join(lines) + "\n")

    def test_summary_counts_all_records_beyond_default_limit(self, tmp_path):
        """With 150 records, summary request_count must be 150, not 100."""
        path = tmp_path / "usage.jsonl"
        self._write_n_records(path, 150)
        tracker = UsageTracker(path=path)
        summary = tracker.get_summary()
        assert summary["request_count"] == 150
        assert summary["total_input_tokens"] == 150 * 10

    def test_summary_counts_all_records_beyond_old_hardcoded_limit(self, tmp_path):
        """With 10_001 records, summary must not cap at 10_000."""
        path = tmp_path / "usage.jsonl"
        self._write_n_records(path, 10_001)
        tracker = UsageTracker(path=path)
        summary = tracker.get_summary()
        assert summary["request_count"] == 10_001
        assert summary["total_output_tokens"] == 10_001 * 5

    def test_get_records_still_respects_limit(self, tmp_path):
        """get_records(limit=N) is unaffected — it should still cap at N."""
        path = tmp_path / "usage.jsonl"
        self._write_n_records(path, 200)
        tracker = UsageTracker(path=path)
        assert len(tracker.get_records(limit=50)) == 50
        assert len(tracker.get_records(limit=100)) == 100

    def test_summary_since_filter_works_with_all_records(self, tmp_path):
        """The `since` filter must still work when all records are scanned."""
        path = tmp_path / "usage.jsonl"
        # Write 5 old + 5 new records
        old = [
            json.dumps(
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "backend": "anthropic",
                    "model": "claude-3-5-sonnet-20241022",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cached_input_tokens": 0,
                    "total_tokens": 2,
                    "cost_usd": None,
                    "session_id": "",
                }
            )
            for _ in range(5)
        ]
        new = [
            json.dumps(
                {
                    "timestamp": "2026-03-01T00:00:00+00:00",
                    "backend": "anthropic",
                    "model": "claude-3-5-sonnet-20241022",
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "cached_input_tokens": 0,
                    "total_tokens": 20,
                    "cost_usd": None,
                    "session_id": "",
                }
            )
            for _ in range(5)
        ]
        path.write_text("\n".join(old + new) + "\n")
        tracker = UsageTracker(path=path)
        summary = tracker.get_summary(since="2026-01-01T00:00:00+00:00")
        assert summary["request_count"] == 5
        assert summary["total_input_tokens"] == 50


# ---------------------------------------------------------------------------
# _estimate_cost sanity checks
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_known_model(self):
        cost = _estimate_cost("gpt-4o-mini", 1_000_000, 0)
        assert cost == pytest.approx(0.15, rel=1e-3)

    def test_prefix_match(self):
        # "gpt-4o-2024-11-20" should match "gpt-4o" pricing
        cost = _estimate_cost("gpt-4o-2024-11-20", 1_000_000, 0)
        assert cost == pytest.approx(2.50, rel=1e-3)

    def test_unknown_model_returns_none(self):
        assert _estimate_cost("unknown-model-xyz", 100, 50) is None

    def test_cached_input_billed_at_lower_rate(self):
        # For claude-3-5-sonnet: input=3.0, cached_input=0.30, output=15.0
        # 0 fresh input, 1M cached, 0 output → 0.30 USD
        cost = _estimate_cost("claude-3-5-sonnet-20241022", 0, 0, cached_input_tokens=1_000_000)
        assert cost == pytest.approx(0.30, rel=1e-3)


# ---------------------------------------------------------------------------
# The pricing table has to keep up with the models
# ---------------------------------------------------------------------------


class TestTheModelsWeActuallyRunArePriceable:
    """A model missing from ``_PRICING`` estimates to None, which
    ``metering.resolve_cost`` turns into a $0 bill and no error.

    That silence is deliberate — a run must not die over its own invoice — and it
    is why nobody noticed the table had stopped at the mid-2025 ids while every
    model in production moved on. Measured on the dev database 2026-08-21:
    claude-haiku-4-5-20251001, claude-sonnet-4-6 and claude-opus-4-7 all priced
    to None, so every run on the pydantic_ai backend billed nothing. The
    claude_agent_sdk path never surfaced it because the SDK reports its own cost
    and never consults this table.

    These are model ids observed in real run documents, not invented ones. When a
    new family ships, it goes in the list and in the table.
    """

    OBSERVED = [
        "claude-haiku-4-5-20251001",  # seen in chat_runs, 32 runs
        "claude-opus-4-7[1m]",  # seen in chat_runs — note the context suffix
        "claude-sonnet-4-6",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
    ]

    @pytest.mark.parametrize("model", OBSERVED)
    def test_every_model_in_use_has_a_price(self, model):
        from pocketpaw.usage_tracker import _estimate_cost

        assert _estimate_cost(model, 10_000, 500, 0) is not None, (
            f"{model} is not in _PRICING — every run on it bills $0 silently"
        )

    @pytest.mark.parametrize("model", OBSERVED)
    def test_a_cache_hit_is_cheaper_than_a_cold_read(self, model):
        """The cached rate has to actually be applied, not silently fall back to
        the full input price. At a 69% hit rate — what the dev database shows —
        getting this wrong overstates the bill by roughly half."""
        from pocketpaw.usage_tracker import _estimate_cost

        cold = _estimate_cost(model, 10_000, 500, 0)
        warm = _estimate_cost(model, 10_000, 500, 9_000)

        assert warm < cold

    def test_a_bare_claude_still_resolves_where_it_always_did(self):
        """``"claude"`` is the agentapi fallback name and prefix-matches the FIRST
        claude key in insertion order.

        Adding the current families ahead of the older ones would have silently
        repriced every run that reported it — Opus rates instead of Sonnet, a 66%
        jump. The new entries go after the old ones for exactly this reason, and
        this pins it.
        """
        from pocketpaw.usage_tracker import _PRICING, _estimate_cost

        assert _estimate_cost("claude", 10_000, 500, 0) == _estimate_cost(
            "claude-sonnet-4-20250514", 10_000, 500, 0
        )
        assert "claude-sonnet-4-20250514" in _PRICING

    def test_a_retired_id_keeps_its_own_retired_price(self):
        """Opus 4 and Opus 4.5+ are $15 and $5 per MTok respectively. A prefix
        match that let one shadow the other would misprice by 3x in whichever
        direction insertion order happened to fall."""
        from pocketpaw.usage_tracker import _estimate_cost

        retired = _estimate_cost("claude-opus-4-20250514", 1_000_000, 0, 0)
        current = _estimate_cost("claude-opus-4-7", 1_000_000, 0, 0)

        assert retired == 15.0
        assert current == 5.0

    def test_an_unknown_model_is_still_none(self):
        """The fallback stays a fallback. Prefix matching must not start pricing
        arbitrary strings just because the table grew."""
        from pocketpaw.usage_tracker import _estimate_cost

        assert _estimate_cost("some-other-vendor-model", 1000, 100, 0) is None
