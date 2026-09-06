"""Tests for usage_tracker.py — UsageTracker fixes.

[FI] Fix: two bugs in UsageTracker:

1. total_tokens excluded cached_input_tokens.
   In `record()`, total was computed as `input_tokens + output_tokens`,
   silently dropping cached tokens from the count even though they are real
   tokens processed by the model.

2. get_summary() called get_records(limit=10_000) instead of reading all
   records, so any installation with more than 10 000 lifetime records would
   silently produce wrong (understated) aggregation totals.

Updated 2026-09-02 (fix/metering-dated-pricing): pricing is a ladder now, so the
tests are in two halves. The older classes exercise `_estimate_cost` over the
hand table, which is the LAST rung and still owns the ids the price library does
not carry. The two new classes at the bottom exercise `price_run`, the front
door, and they pin exact dollar amounts on a dated boundary, a long-context
prompt and a cache-heavy turn. That split is deliberate: every existing pricing
test here asserts a price exists, and two rows sat at the wrong number for two
days while every one of them stayed green.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pocketpaw.usage_tracker import _PRICING, UsageTracker, _estimate_cost, price_run

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

    Two things about this class changed on 2026-09-02 and are worth saying out
    loud rather than leaving the paragraphs above to imply otherwise:

      * The silence is no longer total. ``resolve_cost`` now logs an unpriced run
        at WARNING under its own ``CostSource``, and the sweeper tallies them per
        tick. The bill is still 0 and the run still does not die over its own
        invoice; it just says so.
      * These tests assert a price EXISTS and never that it is RIGHT, which is
        precisely why ``gemini-2.5-flash`` and ``claude-sonnet-5`` sat at the
        wrong numbers for two days. The exact figures live in
        ``TestPricesAreDatedAndPinnedToTheDollar`` below. Existence checks are
        not a substitute for them and were never meant to be.
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
        """``"claude"`` is the agentapi fallback name and matches every claude key.

        It used to take the FIRST one in insertion order, which made the price of
        every run reporting it depend on where new rows happened to be written —
        put the current families above the older ones and it silently repriced
        from Sonnet to Opus, a 66% jump. Since 2026-09-02 the lookup takes the
        LONGEST match instead, so insertion order no longer prices anything.

        The number did not move: the longest claude key is
        ``claude-3-5-sonnet-20241022``, whose $3.00/$15.00/$0.30 is the same rate
        ``claude-sonnet-4-20250514`` carries. That is what this asserts — not
        which key wins, but that the bill is unchanged. Resolving a bare vendor
        name to any particular model is a guess either way, and this change was
        not the place to start making a different one.
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


# ---------------------------------------------------------------------------
# price_run — the dated ladder
# ---------------------------------------------------------------------------


class TestPricesAreDatedAndPinnedToTheDollar:
    """``test_every_model_in_use_has_a_price`` asserts a price EXISTS. It never
    asserted the price was RIGHT, which is exactly why two rows sat wrong for
    two days: ``gemini-2.5-flash`` at $0.15/$0.60 against a real $0.30/$2.50, and
    ``claude-sonnet-5`` at $2.00/$10.00 against $3.00/$15.00. Both would have
    passed every existence check ever written.

    So these pin dollar amounts, not ``> 0``. Every figure was measured against
    genai-prices 0.0.73 on 2026-09-02; if the library moves a rate, one of these
    goes red and someone reads a changelog, which is the entire point.
    """

    # Any moment after the 2026-09-01 Anthropic change. Fixed rather than "now"
    # so the suite does not start pricing at whatever today happens to be.
    AT = datetime(2026, 9, 1, tzinfo=UTC)

    def test_a_cache_heavy_turn_prices_reads_and_writes_apart(self):
        """10k prompt of which 8k is a cache read and 1k a cache write, 1k out.

        $0.003 fresh + $0.0024 read + $0.00375 write + $0.015 out. The write is
        the number the old flat table could not express at all — it had one
        input rate and folded a write into it, undercounting every cached turn.
        """
        assert price_run(
            "claude-sonnet-5",
            input_tokens=10_000,
            output_tokens=1_000,
            cache_read_tokens=8_000,
            cache_write_tokens=1_000,
            at=self.AT,
        ) == Decimal("0.02415")

    def test_input_tokens_is_the_inclusive_total_not_the_remainder(self):
        """The same turn, priced the wrong way round, is a different number.

        If ``input_tokens`` were the uncached remainder, the caller would pass
        1000 and the cache buckets would be subtracted from it a second time.
        Pinning both proves the contract rather than describing it.
        """
        inclusive = price_run(
            "claude-sonnet-5",
            input_tokens=10_000,
            output_tokens=1_000,
            cache_read_tokens=8_000,
            cache_write_tokens=1_000,
            at=self.AT,
        )
        remainder = price_run(
            "claude-sonnet-5",
            input_tokens=1_000,
            output_tokens=1_000,
            cache_read_tokens=8_000,
            cache_write_tokens=1_000,
            at=self.AT,
        )
        assert inclusive == Decimal("0.02415")
        # The malformed call is widened to cover its own cache lines, so it
        # loses the 1k of fresh input rather than crashing or going negative.
        assert remainder == Decimal("0.02115")
        assert remainder < inclusive

    def test_a_cache_write_costs_more_than_plain_input(self):
        """Anthropic bills a 5-minute cache write at 1.25x input. $3.75/MTok
        against $3.00. The old estimator had no concept of a write at all."""
        write = price_run(
            "claude-sonnet-5",
            input_tokens=1_000,
            output_tokens=0,
            cache_write_tokens=1_000,
            at=self.AT,
        )
        plain = price_run("claude-sonnet-5", input_tokens=1_000, output_tokens=0, at=self.AT)
        assert plain == Decimal("0.003")
        assert write == Decimal("0.00375")

    def test_a_long_context_prompt_crosses_the_200k_tier(self):
        """``claude-sonnet-4-5`` is $3.00/MTok up to 200k prompt tokens and
        $6.00 above it. One flat rate per model cannot say that, so a long
        prompt used to bill at half price."""
        assert price_run(
            "claude-sonnet-4-5", input_tokens=199_999, output_tokens=0, at=self.AT
        ) == Decimal("0.599997")
        assert price_run(
            "claude-sonnet-4-5", input_tokens=250_000, output_tokens=0, at=self.AT
        ) == Decimal("1.5")

    def test_the_price_is_the_one_in_force_when_the_run_happened(self):
        """THE crux. ``claude-sonnet-5`` was $2.00/MTok through 2026-08-31 and
        $3.00 from 2026-09-01.

        Billing runs on a sweeper, after the run, 200 at a tick, so a backlog
        spans days. Pricing at ``now()`` bills a run from the 31st at the 1st's
        rate — a 50% overcharge that no test asserting ``> 0`` would ever see.
        """
        before = price_run(
            "claude-sonnet-5",
            input_tokens=1_000_000,
            output_tokens=0,
            at=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
        )
        after = price_run(
            "claude-sonnet-5",
            input_tokens=1_000_000,
            output_tokens=0,
            at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        )
        assert before == Decimal("2.0")
        assert after == Decimal("3.00")

    def test_a_naive_timestamp_is_read_as_utc(self):
        """Mongo hands back naive datetimes and the rest of this codebase reads
        those as UTC. Pricing has to agree or a run near a boundary drifts."""
        assert price_run(
            "claude-sonnet-5", input_tokens=1_000_000, output_tokens=0, at=datetime(2026, 8, 31)
        ) == Decimal("2.0")

    def test_an_unpriceable_model_is_none_and_never_zero(self):
        """``None`` and ``Decimal("0")`` are different facts. A $0 bill is a
        statement; an unknown bill is a missing statement, and collapsing the
        second into the first is how a stale table stays invisible."""
        got = price_run("some-other-vendor-model", input_tokens=1000, output_tokens=100, at=self.AT)
        assert got is None
        assert got != Decimal("0")

    def test_it_never_raises_on_junk(self):
        """A run must not die over its own invoice. Every degenerate input is a
        ``None`` or a number, never an exception."""
        for kwargs in (
            {"model": None, "input_tokens": 1, "output_tokens": 1},
            {"model": "", "input_tokens": 1, "output_tokens": 1},
            {"model": "claude-sonnet-5", "input_tokens": -5, "output_tokens": None},
            {"model": "claude-sonnet-5", "input_tokens": "oops", "output_tokens": 3},
            # cache buckets larger than the total they are supposed to be part of
            {
                "model": "claude-sonnet-5",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 8000,
            },
        ):
            model = kwargs.pop("model")
            price_run(model, at=self.AT, **kwargs)  # must not raise

    @pytest.mark.parametrize(
        "model",
        ["deepseek-chat", "deepseek-v3.2", "deepseek/deepseek-chat"],
    )
    def test_every_spelling_of_a_model_reaches_a_price(self, model):
        """Model strings are not normalised upstream. The model-group alias
        (``deepseek-v3.2``), the upstream bare name (``deepseek-chat``) and the
        provider-prefixed form are all reachable, and the price library rejects
        the last one outright."""
        assert price_run(model, input_tokens=1_000_000, output_tokens=0, at=self.AT) is not None

    def test_a_prefixed_id_prices_the_same_as_its_bare_form(self):
        bare = price_run("deepseek-chat", input_tokens=1_000_000, output_tokens=0, at=self.AT)
        prefixed = price_run(
            "deepseek/deepseek-chat", input_tokens=1_000_000, output_tokens=0, at=self.AT
        )
        assert bare == prefixed == Decimal("0.135")

    def test_pricing_opens_no_socket(self):
        """The price data is compiled into the package as Python source and the
        updater is opt-in. Nothing on this path may reach the network — a
        pricing call that can block on I/O inside a 200-run billing sweep is the
        reason litellm was rejected for this job."""
        import socket

        def _boom(*args, **kwargs):
            raise AssertionError("pricing opened a socket")

        original_socket, original_conn = socket.socket, socket.create_connection
        socket.socket, socket.create_connection = _boom, _boom
        try:
            assert price_run(
                "claude-sonnet-5", input_tokens=1_000_000, output_tokens=0, at=self.AT
            ) == Decimal("3.00")
        finally:
            socket.socket, socket.create_connection = original_socket, original_conn


class TestTheTableIsTheLastRungAndStillCarriesItsOwn:
    """``_PRICING`` stayed rather than being deleted, and it earns its keep:
    measured against genai-prices 0.0.73 on 2026-09-02, the library does not
    carry ``claude-haiku-4-20250506``, ``codex-mini-latest``, or the bare
    ``"claude"`` the agentapi path reports. Dropping the table would have
    silently unpriced all three."""

    AT = datetime(2026, 9, 1, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("claude-haiku-4-20250506", Decimal("0.8")),
            ("codex-mini-latest", Decimal("1.5")),
            ("claude", Decimal("3.0")),
        ],
    )
    def test_the_ids_the_library_does_not_carry_fall_through_to_the_table(self, model, expected):
        assert price_run(model, input_tokens=1_000_000, output_tokens=0, at=self.AT) == expected

    def test_a_dated_mini_id_does_not_match_its_bigger_sibling(self):
        """C5. The old lookup scanned in insertion order and took the first
        two-way prefix hit, so ``gpt-4.1-mini-2025-04-14`` matched ``gpt-4.1``
        and billed at $2.00/MTok instead of $0.40 — a 5x overcharge that
        depended on nothing but the order rows were written in. Longest match
        wins now, and this pins the case that was wrong."""
        assert _estimate_cost("gpt-4.1-mini-2025-04-14", 1_000_000, 0) == 0.4
        assert _estimate_cost("gpt-4.1-nano-2025-04-14", 1_000_000, 0) == 0.1
        assert _estimate_cost("gpt-4o-mini-2024-07-18", 1_000_000, 0) == 0.15

    def test_the_two_rows_that_were_measured_wrong(self):
        """C7, pinned as dollars. Both rows were correct when written and went
        wrong on a date nobody was watching."""
        assert _PRICING["gemini-2.5-flash"] == {"input": 0.30, "output": 2.50}
        assert _PRICING["claude-sonnet-5"]["input"] == 3.0
        assert _PRICING["claude-sonnet-5"]["output"] == 15.0
