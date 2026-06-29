# tests/cloud/billing/test_usage.py — proves the per-workspace USAGE transform
# (the billing usage-graph seam). The frontend renders a daily usage graph
# broken down by model from a workspace's LiteLLM proxy usage; this module locks
# the transform LiteLLM ``/user/daily/activity`` -> the WorkspaceUsage contract,
# including the USD->credits conversion (money-adjacent — pinned, not ambient).
#
# Asserts:
#   * the workspace -> LiteLLM virtual key mapping is resolved (via the provisioning
#     service's get_tenant_key) and passed as the ``api_key`` filter to the proxy
#     daily-activity read — usage is scoped to exactly that tenant's key.
#   * each LiteLLM daily record's ``breakdown.models[<model>].metrics`` is folded
#     into a daily bucket with a per-model {credits, tokens, requests} breakdown,
#     the spend USD converted to credits via the SAME rate card the meter uses
#     (round(cost_usd * markup / credit_usd) — a dollar shown == a dollar billed).
#   * ``models`` is the sorted distinct union across the range; ``total_credits``
#     is the sum over every bucket; each bucket's ``total_credits`` sums its models.
#   * default date range = last 30 days when start/end omitted, and the resolved
#     range is echoed onto the response.
#   * a workspace with NO provisioned key -> empty buckets + empty models +
#     total_credits 0 (HTTP 200, not an error) and NO proxy call.
#   * pagination: when the proxy reports ``metadata.has_more`` the service walks
#     every page and merges the daily records.
#
# A FAKE daily-activity client (duck-typed, no HTTP) stands in for the proxy — the
# admin client's own HTTP wiring (the new ``user_daily_activity`` method) is
# covered separately in tests/cloud/catalog/test_admin_client.py. Uses the shared
# ``mongo_db`` fixture (Beanie over ALL_DOCUMENTS) so get_tenant_key reads a real
# persisted LiteLLMTenantKey row, the same DB-fixture pattern the provisioning /
# credits tests use.
#
# Created 2026-06-29 (feat/billing-usage-endpoint): new test module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.billing import usage
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits

WS = "ws_usage_test"

# Pin the spend rate card so credits never depend on ambient POCKETPAW_* settings:
# credits = round(cost_usd * 2.5 / 0.01) = round(cost_usd * 250). Identical to the
# card the meter + spend-ingest use, so a dollar of usage shown matches a dollar
# billed.
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)

BUDGET = KeyBudget(max_budget_usd=25.0, budget_duration="30d", rpm_limit=60, tpm_limit=0, models=[])


def _metrics(spend: float, prompt: int, completion: int, requests: int) -> dict:
    """A LiteLLM ``SpendMetrics``-shaped dict (the per-model / per-day metric block)."""
    return {
        "spend": spend,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "successful_requests": requests,
        "failed_requests": 0,
        "api_requests": requests,
    }


def _day(date_str: str, models: dict[str, dict]) -> dict:
    """A LiteLLM ``DailySpendData``-shaped record: a date + a breakdown.models map
    (each model -> {"metrics": SpendMetrics}). The day-level ``metrics`` block is
    present for shape-fidelity but the transform reads the per-model breakdown."""
    day_metrics = _metrics(
        sum(m["__spend"] for m in models.values()),
        0,
        0,
        sum(m["__requests"] for m in models.values()),
    )
    breakdown_models = {
        name: {"metrics": m["metrics"], "metadata": {}, "api_key_breakdown": {}}
        for name, m in models.items()
    }
    return {"date": date_str, "metrics": day_metrics, "breakdown": {"models": breakdown_models}}


def _model_entry(spend: float, prompt: int, completion: int, requests: int) -> dict:
    """Helper bundling a model's metrics + its raw spend/requests (for the day total)."""
    return {
        "metrics": _metrics(spend, prompt, completion, requests),
        "__spend": spend,
        "__requests": requests,
    }


class FakeDailyActivity:
    """In-memory stand-in for LiteLLMAdminClient.user_daily_activity (no HTTP).

    ``pages`` is a list of response bodies (each a SpendAnalyticsPaginatedResponse-
    shaped dict). The method returns the merged ``results`` across all pages and
    records the kwargs it was called with so the test can assert the api_key filter
    + date range round-tripped. ``calls`` counts invocations (0 proves the no-key
    path makes no proxy call).
    """

    def __init__(self, *, pages: list[dict] | None = None) -> None:
        self.calls = 0
        self.last_kwargs: dict | None = None
        self._pages = pages if pages is not None else [{"results": [], "metadata": {}}]

    async def user_daily_activity(
        self, *, start_date: str, end_date: str, api_key: str, page_size: int = 1000
    ) -> list[dict]:
        self.calls += 1
        self.last_kwargs = {
            "start_date": start_date,
            "end_date": end_date,
            "api_key": api_key,
            "page_size": page_size,
        }
        merged: list[dict] = []
        for body in self._pages:
            merged.extend(body.get("results", []))
        return merged


# ---------------------------------------------------------------------------
# The transform — LiteLLM daily activity -> the WorkspaceUsage contract.
# ---------------------------------------------------------------------------


async def test_usage_transforms_daily_activity_into_buckets_by_model(mongo_db):
    # Provision the workspace so get_tenant_key resolves a real virtual key.
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=_FakeAdmin())

    sonnet = "anthropic/claude-3-5-sonnet"
    gpt = "openai/gpt-4o"
    pages = [
        {
            "results": [
                # Day 1: two models. sonnet $0.04 -> 10 credits; gpt $0.02 -> 5 credits.
                _day(
                    "2026-06-01",
                    {
                        sonnet: _model_entry(0.04, 1000, 200, 3),
                        gpt: _model_entry(0.02, 500, 100, 2),
                    },
                ),
                # Day 2: sonnet only. $0.10 -> 25 credits.
                _day("2026-06-02", {sonnet: _model_entry(0.10, 4000, 800, 7)}),
            ],
            "metadata": {"has_more": False, "page": 1, "total_pages": 1},
        }
    ]
    client = FakeDailyActivity(pages=pages)

    result = await usage.get_workspace_usage(
        WS,
        start_date="2026-06-01",
        end_date="2026-06-02",
        spend_card=SPEND,
        daily_activity_client=client,
    )

    # The proxy read was scoped to the workspace's virtual key over the given range.
    assert client.calls == 1
    assert client.last_kwargs["api_key"] == "sk-ws-ws_usage_test"
    assert client.last_kwargs["start_date"] == "2026-06-01"
    assert client.last_kwargs["end_date"] == "2026-06-02"

    # Echoed range.
    assert result.start_date == "2026-06-01"
    assert result.end_date == "2026-06-02"

    # Distinct model list, sorted (alphabetical: "anthropic/..." before "openai/...").
    assert result.models == sorted([gpt, sonnet])
    assert result.models == [sonnet, gpt]

    # Two daily buckets.
    assert [b.date for b in result.buckets] == ["2026-06-01", "2026-06-02"]

    b1 = result.buckets[0]
    assert b1.by_model[sonnet].credits == 10  # round(0.04 * 250)
    assert b1.by_model[sonnet].tokens == 1200  # prompt + completion
    assert b1.by_model[sonnet].requests == 3
    assert b1.by_model[gpt].credits == 5  # round(0.02 * 250)
    assert b1.by_model[gpt].tokens == 600
    assert b1.by_model[gpt].requests == 2
    assert b1.total_credits == 15  # 10 + 5

    b2 = result.buckets[1]
    assert set(b2.by_model.keys()) == {sonnet}
    assert b2.by_model[sonnet].credits == 25  # round(0.10 * 250)
    assert b2.by_model[sonnet].tokens == 4800
    assert b2.total_credits == 25

    # Grand total over every bucket.
    assert result.total_credits == 40  # 15 + 25


async def test_usage_defaults_to_last_30_days_when_range_omitted(mongo_db):
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=_FakeAdmin())
    client = FakeDailyActivity(pages=[{"results": [], "metadata": {}}])

    result = await usage.get_workspace_usage(WS, spend_card=SPEND, daily_activity_client=client)

    today = datetime.now(UTC).date()
    expected_start = (today - timedelta(days=29)).isoformat()  # inclusive 30-day window
    expected_end = today.isoformat()

    assert result.start_date == expected_start
    assert result.end_date == expected_end
    assert client.last_kwargs["start_date"] == expected_start
    assert client.last_kwargs["end_date"] == expected_end
    # No usage -> empty, but a key exists so the proxy WAS queried.
    assert result.buckets == []
    assert result.models == []
    assert result.total_credits == 0


async def test_usage_unprovisioned_workspace_returns_empty_no_proxy_call(mongo_db):
    # No LiteLLMTenantKey row for WS -> brand-new workspace, no usage yet.
    assert await provisioning.get_tenant_key(WS) is None
    client = FakeDailyActivity(pages=[{"results": [_day("2026-06-01", {})]}])

    result = await usage.get_workspace_usage(
        WS,
        start_date="2026-06-01",
        end_date="2026-06-30",
        spend_card=SPEND,
        daily_activity_client=client,
    )

    # Empty contract, NOT an error — and the proxy is never called (no key to scope).
    assert result.buckets == []
    assert result.models == []
    assert result.total_credits == 0
    assert result.start_date == "2026-06-01"
    assert result.end_date == "2026-06-30"
    assert client.calls == 0


async def test_usage_walks_every_page_when_has_more(mongo_db):
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=_FakeAdmin())
    sonnet = "anthropic/claude-3-5-sonnet"
    # Two pages: the client merges both pages' results (pagination is the client's
    # job; the service sees the merged list). Distinct dates across pages.
    pages = [
        {
            "results": [_day("2026-06-01", {sonnet: _model_entry(0.04, 1000, 200, 1)})],
            "metadata": {"has_more": True, "page": 1, "total_pages": 2},
        },
        {
            "results": [_day("2026-06-02", {sonnet: _model_entry(0.02, 500, 100, 1)})],
            "metadata": {"has_more": False, "page": 2, "total_pages": 2},
        },
    ]
    client = FakeDailyActivity(pages=pages)

    result = await usage.get_workspace_usage(
        WS,
        start_date="2026-06-01",
        end_date="2026-06-02",
        spend_card=SPEND,
        daily_activity_client=client,
    )

    assert [b.date for b in result.buckets] == ["2026-06-01", "2026-06-02"]
    assert result.total_credits == 15  # 10 + 5


async def test_usage_sub_credit_spend_rounds_to_zero(mongo_db):
    # A tiny spend below half a credit rounds to 0 credits (no phantom charge) but
    # the model still appears with its tokens/requests — usage is real even if the
    # rounded credit cost is 0.
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=_FakeAdmin())
    sonnet = "anthropic/claude-3-5-sonnet"
    pages = [
        {
            "results": [_day("2026-06-01", {sonnet: _model_entry(0.0001, 5, 1, 1)})],
            "metadata": {"has_more": False},
        }
    ]
    client = FakeDailyActivity(pages=pages)

    result = await usage.get_workspace_usage(
        WS,
        start_date="2026-06-01",
        end_date="2026-06-01",
        spend_card=SPEND,
        daily_activity_client=client,
    )

    assert result.models == [sonnet]
    b1 = result.buckets[0]
    assert b1.by_model[sonnet].credits == 0  # round(0.0001 * 250) == 0
    assert b1.by_model[sonnet].tokens == 6
    assert b1.by_model[sonnet].requests == 1
    assert b1.total_credits == 0
    assert result.total_credits == 0


# ---------------------------------------------------------------------------
# Minimal fake admin client to provision a key (no HTTP) — mirrors the
# provisioning test's FakeAdmin (only the generate_key surface is needed here).
# ---------------------------------------------------------------------------


class _FakeAdmin:
    async def generate_key(self, **kwargs):
        return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **kwargs}
