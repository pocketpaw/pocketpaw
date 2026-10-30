# tests/cloud/test_concierge_conversation_quota.py — the Staff tier's "200
# conversations a month" stops being a catalog claim and becomes a ceiling.
#
# Created 2026-08-26 (feat/concierge-conversation-quota). Until now
# ``conversation_allowance`` was read by exactly one thing: a DTO that renders a
# plan card. Nothing counted, nothing refused, and a site on the $19 rung could
# serve unlimited conversations forever.
#
# What this pins, and why each one is a way to get it wrong:
#
#   * The unit is a CONVERSATION, not a message and not a run. The ladder sells
#     "200 conversations a month" and the storefront shows that sentence to
#     buyers, so counting messages would silently sell a tenth of what was
#     advertised.
#   * Only a turn that STARTS a conversation is refused. Cutting a thread off
#     part-way strands a visitor mid-sentence over a number they cannot see.
#   * An allowance of 0 is NOT a cap. ``agency`` sells the concierge with a 0
#     allowance because it is metered from the first conversation at a pooled
#     rate; reading 0 as a ceiling refuses every conversation on the tier that
#     pays per conversation.
#   * The quota fails OPEN. This is the opposite of the entitlement gate next to
#     it, on purpose: "has this been paid for" fails to refuse, "has a paid
#     allowance been used up" fails to serve.
#   * The month boundary is NAIVE LOCAL, because ``created_at`` is. An aware
#     boundary renders with an offset that sorts after every stored row, so the
#     count comes back 0 and the ceiling silently never fires.

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.billing.enforcement import (
    _month_start,
    concierge_conversation_quota_exceeded,
)

from pocketpaw.paw_bar.store import PawBarStore

pytestmark = pytest.mark.anyio

WIDGET = "w-quota"
WORKSPACE = "ws-quota"


@pytest.fixture
async def store(tmp_path):
    return PawBarStore(str(tmp_path / "paw_bar.db"))


def _enforce(monkeypatch, *, on: bool) -> None:
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(billing_enforced=on, sites_billing_enforced=on),
    )


def _site(tier: str | None):
    return SimpleNamespace(plan_tier=tier)


async def _start_conversations(store: PawBarStore, n: int) -> None:
    """Mint n distinct conversations by giving each visitor its own ref."""
    for i in range(n):
        await store.upsert_conversation_on_visitor_turn(WIDGET, f"visitor-{i}", WORKSPACE)


# --------------------------------------------------------------------------- #
# The count
# --------------------------------------------------------------------------- #


async def test_a_conversation_counts_once_however_many_messages_it_carries(store):
    """The unit is the conversation. Ten turns from one visitor is one row and
    one count — counting turns would sell a tenth of what the ladder advertises."""
    for _ in range(10):
        await store.upsert_conversation_on_visitor_turn(WIDGET, "visitor-a", WORKSPACE)

    assert await store.count_conversations_started_since(WIDGET, _month_start(), WORKSPACE) == 1


async def test_distinct_visitors_are_distinct_conversations(store):
    await _start_conversations(store, 3)

    assert await store.count_conversations_started_since(WIDGET, _month_start(), WORKSPACE) == 3


async def test_the_count_is_scoped_to_one_widget_and_workspace(store):
    await _start_conversations(store, 2)
    await store.upsert_conversation_on_visitor_turn("w-other", "visitor-x", WORKSPACE)

    assert await store.count_conversations_started_since(WIDGET, _month_start(), WORKSPACE) == 2


async def test_a_retired_conversation_still_counts(store):
    """ "Start over" retires the row (``active = 0``) and mints a new one. Both
    count: excluding retired rows would let one visitor loop "start over"
    indefinitely and never spend the allowance, which is the obvious way to get
    unlimited conversations on a capped tier."""
    await store.upsert_conversation_on_visitor_turn(WIDGET, "visitor-a", WORKSPACE)
    await store.open_conversation(WIDGET, "visitor-a", WORKSPACE)

    assert await store.count_conversations_started_since(WIDGET, _month_start(), WORKSPACE) == 2


async def test_conversations_before_the_boundary_do_not_count(store):
    """Last month's traffic must not spend this month's allowance."""
    await _start_conversations(store, 2)
    future = datetime.now() + timedelta(days=1)

    assert await store.count_conversations_started_since(WIDGET, future, WORKSPACE) == 0


async def test_the_month_boundary_is_naive_local(store):
    """``created_at`` is written with ``datetime.now().isoformat()`` — no zone.

    An aware boundary renders as '...+00:00', which sorts AFTER every stored
    value, so the count returns 0 and the ceiling never fires. The bug would be
    invisible: no error, just a quota that silently does nothing."""
    boundary = _month_start()

    assert boundary.tzinfo is None
    assert "+" not in boundary.isoformat()


# --------------------------------------------------------------------------- #
# The ceiling
# --------------------------------------------------------------------------- #


async def test_under_the_allowance_is_allowed(store, monkeypatch):
    _enforce(monkeypatch, on=True)
    await _start_conversations(store, 199)

    assert (
        await concierge_conversation_quota_exceeded(
            _site("staff"), widget_id=WIDGET, workspace_id=WORKSPACE, store=store
        )
        is False
    )


async def test_at_the_allowance_the_next_conversation_is_refused(store, monkeypatch):
    """200 included means the 201st is refused, so the ceiling trips AT 200."""
    _enforce(monkeypatch, on=True)
    await _start_conversations(store, 200)

    assert (
        await concierge_conversation_quota_exceeded(
            _site("staff"), widget_id=WIDGET, workspace_id=WORKSPACE, store=store
        )
        is True
    )


async def test_a_zero_allowance_is_not_a_ceiling(store, monkeypatch):
    """0 means "this tier does not sell an allowance", never "zero conversations".

    Uses ``site`` — a SITE-SCOPED tier whose allowance is 0 — because that is the
    only shape that actually reaches this branch. The first version of this test
    used ``agency``, the tier the design comment cites as the metered case, and it
    passed without ever getting here: an org flat is refused by
    ``site_scoped_tier`` two lines earlier, so the assertion held for a reason
    that had nothing to do with the allowance. The mutation plan caught it.
    """
    _enforce(monkeypatch, on=True)
    await _start_conversations(store, 50)

    assert (
        await concierge_conversation_quota_exceeded(
            _site("site"), widget_id=WIDGET, workspace_id=WORKSPACE, store=store
        )
        is False
    )


async def test_an_org_flat_never_reaches_the_allowance_branch(store, monkeypatch):
    """``agency`` is the tier that is genuinely metered-from-zero, and it can never
    be a single site's plan: it is an org-wide flat, so ``site_scoped_tier``
    refuses it before any allowance is read. Pinned so the design comment about
    the metered case is read as forward-looking rather than as describing
    something reachable today."""
    from pocketpaw_ee.cloud.billing import site_plans

    _enforce(monkeypatch, on=True)
    await _start_conversations(store, 50)

    assert site_plans.get_site_plan("agency").is_org_scoped is True
    assert site_plans.site_scoped_tier("agency") is None
    assert (
        await concierge_conversation_quota_exceeded(
            _site("agency"), widget_id=WIDGET, workspace_id=WORKSPACE, store=store
        )
        is False
    )


async def test_an_unenforced_deployment_has_no_ceiling(store, monkeypatch):
    """OSS and self-host see no paywall, and this returns before reading anything."""
    _enforce(monkeypatch, on=False)
    await _start_conversations(store, 500)

    assert (
        await concierge_conversation_quota_exceeded(
            _site("staff"), widget_id=WIDGET, workspace_id=WORKSPACE, store=store
        )
        is False
    )


async def test_an_unresolvable_tier_is_left_to_the_entitlement_gate(store, monkeypatch):
    _enforce(monkeypatch, on=True)
    await _start_conversations(store, 500)

    for tier in (None, "not_a_tier"):
        assert (
            await concierge_conversation_quota_exceeded(
                _site(tier), widget_id=WIDGET, workspace_id=WORKSPACE, store=store
            )
            is False
        )


async def test_a_broken_store_serves_rather_than_refuses(monkeypatch):
    """FAIL OPEN, and the opposite of the entitlement gate beside it.

    That gate answers "has this been paid for", where the safe error is refuse.
    This one answers "has a paid allowance been used up", where the safe error is
    serve — charging a customer for the tier and then withholding it because a
    count did not load is the worse outcome."""
    _enforce(monkeypatch, on=True)

    class _Broken:
        async def count_conversations_started_since(self, *a, **kw):
            raise RuntimeError("store is down")

    assert (
        await concierge_conversation_quota_exceeded(
            _site("staff"), widget_id=WIDGET, workspace_id=WORKSPACE, store=_Broken()
        )
        is False
    )


async def test_the_allowance_comes_from_the_catalog_not_a_literal():
    """If the ladder re-prices, the ceiling moves with it. A hardcoded 200 here
    would keep enforcing the old number after the catalog changed."""
    from pocketpaw_ee.cloud.billing import site_plans

    assert site_plans.get_site_plan("staff").conversation_allowance == 200
    assert site_plans.get_site_plan("site").conversation_allowance == 0
