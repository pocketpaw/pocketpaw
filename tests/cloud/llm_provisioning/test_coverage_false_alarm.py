# tests/cloud/llm_provisioning/test_coverage_false_alarm.py — proves the
# attribution-coverage check can tell the proxy's own traffic from a billing hole.
#
# THE BUG. The check counted every spend row no tenant claimed and the runbook told
# the operator to treat any non-zero count as blocking. But a LiteLLM proxy logs
# traffic of its own beside ours — a human trying a model in its admin dashboard,
# its periodic model health check — and none of it can ever carry a workspace or be
# billed to one. So the count could never reach zero on a live deployment, and the
# one guard protecting the billing cutover was permanently red.
#
# The shape is taken from the production proxy on 2026-09-03. A sweep reported
# "8 of 19 proxy spend row(s) are being served and not billed", which read as a
# serious under-bill. Reading the rows back showed all 8 were the dashboard and the
# health check, worth $0.00014545 between them, and the 11 rows that WERE tagged
# cost exactly $0.00 because the models in use were free. Nothing was wrong. Several
# hours went into finding that out, which is the cost this test exists to stop
# paying again.
#
# So the check now prices the remainder and splits it three ways: rows naming an
# unswept workspace, rows from a real caller that named nobody, and the proxy's own.
# Only the first two are ours.
#
# Uses the shared ``mongo_db`` fixture from tests/cloud/conftest.py and a FAKE admin
# client, mirroring test_spend_by_customer.py.
#
# Created 2026-09-04 (fix/litellm-spend-leaks): new test module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.catalog.admin_client import LiteLLMAdminError
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning

UNTIL = datetime(2026, 9, 3, 21, 44, 22, tzinfo=UTC)
SINCE = UNTIL - timedelta(hours=24)

WS_A = "6a1210f462bf55588dee1d4f"
WS_B = "6a146169ad1f4c4decb828f4"


def _tagged(rid: str, workspace: str, usd: float = 0.0) -> dict:
    return {
        "request_id": rid,
        "end_user": workspace,
        "spend": usd,
        "startTime": "2026-09-03T20:11:29+00:00",
        "model": "deepseek/deepseek-v4-pro-0813-free",
    }


def _dashboard(rid: str, usd: float = 0.0) -> dict:
    """A human clicking "test model" in the LiteLLM admin UI."""
    return {
        "request_id": rid,
        "end_user": "",
        "team_id": "litellm-dashboard",
        "spend": usd,
        "startTime": "2026-09-03T20:14:42+00:00",
        "model": "Qwen3.8-27B",
    }


def _health_check(rid: str) -> dict:
    """The proxy probing its own models."""
    return {
        "request_id": rid,
        "end_user": "",
        "team_id": "litellm-internal-health-check",
        "spend": 0.0,
        "startTime": "2026-09-03T20:40:48+00:00",
        "model": "minimax/minimax-m3:free",
    }


def _real_untagged(rid: str, usd: float) -> dict:
    """Our own deployment reaching the proxy without naming who pays."""
    return {
        "request_id": rid,
        "end_user": "",
        "team_id": None,
        "spend": usd,
        "startTime": "2026-09-02T06:07:56+00:00",
        "model": "deepseek/deepseek-v4-flash",
    }


class FakeAdmin:
    """Serves the window's rows, and derives the counts from them.

    Deriving rather than hardcoding matters: the check reads counts on one path and
    rows on another, and a fake that let the two disagree would pass a version of
    the code where they do.
    """

    def __init__(self, rows: list[dict], *, customers=None, fail_window=False):
        self.rows = rows
        self.customers = customers or []
        self.fail_window = fail_window
        self.window_calls = 0

    async def spend_log_count(self, *, start_date, end_date, end_user=None):
        if end_user is None:
            return len(self.rows)
        return sum(1 for r in self.rows if (r.get("end_user") or "") == end_user)

    async def spend_logs_window(self, *, start_date, end_date, page_size=100, max_rows=1000):
        self.window_calls += 1
        if self.fail_window:
            raise LiteLLMAdminError("window read failed (simulated)")
        return list(self.rows), True

    async def list_customers(self):
        return list(self.customers)


async def test_the_proxys_own_traffic_is_not_a_billing_hole(mongo_db):
    """The production shape. Twelve tagged rows, eight unattributed, and every one
    of the eight is the proxy talking to itself. Nothing of ours is unbilled."""
    rows = (
        [_tagged(f"a{i}", WS_A) for i in range(6)]
        + [_tagged(f"b{i}", WS_B) for i in range(6)]
        + [_dashboard(f"d{i}") for i in range(7)]
        + [_health_check("h0")]
    )
    admin = FakeAdmin(rows)

    coverage = await provisioning.spend_attribution_coverage(
        [WS_A, WS_B], since=SINCE, until=UNTIL, admin_client=admin
    )

    assert coverage.unattributed_rows == 8
    assert coverage.internal_rows == 8
    assert coverage.untagged_rows == 0, "the proxy's own traffic was read as our hole"
    assert coverage.untagged_usd == 0.0
    assert coverage.classified is True
    assert not coverage.degraded


async def test_a_real_untagged_caller_is_still_reported(mongo_db):
    """The check must not go quiet. Our own deployment reaching the proxy without
    a workspace is the failure the whole seam exists to catch, and it has to
    survive the noise filter that let the dashboard through."""
    rows = [
        _tagged("a0", WS_A),
        _dashboard("d0"),
        _health_check("h0"),
        _real_untagged("r0", 0.02039758),
        _real_untagged("r1", 0.00692236),
    ]
    admin = FakeAdmin(rows)

    coverage = await provisioning.spend_attribution_coverage(
        [WS_A], since=SINCE, until=UNTIL, admin_client=admin
    )

    assert coverage.unattributed_rows == 4
    assert coverage.internal_rows == 2
    assert coverage.untagged_rows == 2
    assert coverage.untagged_usd == pytest.approx(0.02731994)


async def test_the_remainder_is_priced_not_just_counted(mongo_db):
    """A count cannot tell a hundredth of a cent of dashboard poking from a dollar
    of unbilled chat, and deciding whether to block a billing cutover is exactly
    the question that needs the difference."""
    rows = [_tagged("a0", WS_A), _dashboard("d0", usd=0.00014545)]
    admin = FakeAdmin(rows)

    coverage = await provisioning.spend_attribution_coverage(
        [WS_A], since=SINCE, until=UNTIL, admin_client=admin
    )

    assert coverage.unattributed_usd == pytest.approx(0.00014545)
    assert coverage.untagged_usd == 0.0


async def test_a_clean_window_never_reads_the_rows(mongo_db):
    """The classification is a diagnostic, and a diagnostic that runs when there is
    nothing to diagnose is just cost. Every tick pays for the counts; only a tick
    with a remainder pays to explain it."""
    admin = FakeAdmin([_tagged("a0", WS_A)])

    coverage = await provisioning.spend_attribution_coverage(
        [WS_A], since=SINCE, until=UNTIL, admin_client=admin
    )

    assert coverage.unattributed_rows == 0
    assert admin.window_calls == 0


async def test_a_failed_row_read_leaves_the_count_alone(mongo_db):
    """The classification is an observation of the check, not part of it. If the
    proxy will not serve the rows, the operator still gets the remainder — flagged
    as unclassified, so a zero in the split reads as "could not tell" rather than
    as "nothing of ours"."""
    rows = [_tagged("a0", WS_A), _real_untagged("r0", 0.5)]
    admin = FakeAdmin(rows, fail_window=True)

    coverage = await provisioning.spend_attribution_coverage(
        [WS_A], since=SINCE, until=UNTIL, admin_client=admin
    )

    assert coverage.unattributed_rows == 1
    assert coverage.classified is False
    assert coverage.untagged_rows == 0
