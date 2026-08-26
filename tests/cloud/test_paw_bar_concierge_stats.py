# tests/cloud/test_paw_bar_concierge_stats.py — the owner's concierge scoreboard.
#
# Created 2026-08-26. The Concierge panel could say how many conversations a site
# had and nothing about what running it cost, so the one question an owner asks
# about an always-on agent — "is this worth what it is spending?" — had no answer
# anywhere in the product. GET /paw-bar/admin/site/{id}/stats is that answer.
#
# What each layer here protects, in the order the endpoint is wrong if it breaks:
#
#   * ARITHMETIC. Tokens and cost come from ``metering.service``, the same
#     resolvers the workspace wallet bills with. If this file's totals ever stop
#     matching that, the panel and the invoice disagree in front of the customer.
#   * COUNTING THE RIGHT THING. Conversations and visitors are different numbers
#     — a visitor may hold several threads — and a pre-identity run still counts
#     once. Collapsing either is how a dashboard quietly under-reports.
#   * ISOLATION. The runs are read by the site's OWN pocket. A sibling site on
#     the same workspace contributing a single cent here is a billing leak
#     between two of the customer's own products.
#   * HONESTY UNDER LIMITS. The scan is bounded, so a busy site's number is a
#     floor. ``truncated`` has to say so; a partial scan rendered as a confident
#     total is the failure mode analytics surfaces are known for.
#   * REFUSING, NOT REINTERPRETING. A malformed window is a 422. Answering it
#     with "all" would silently change the question.

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarBlock, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

_KEY = "site_key_" + "a" * 24
_REF = "cust-0001"
_USER_ID = PydanticObjectId()


async def _site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        script_name="",
        signed_key=_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


def _widget(**ov: Any) -> PawBarWidget:
    d = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=PawBarSpec(
            widget_id="pp_seed",
            pocket_id="pocket-1",
            blocks=[PawBarBlock(type="text", content="Hi")],
        ),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
    )
    d.update(ov)
    return PawBarWidget(**d)


def _skey(conversation_id: str, pocket_id: str = "pocket-1") -> str:
    return f"cloud:concierge:{pocket_id}:{conversation_id}:agent-xyz"


def _usage(input_tokens: int, output_tokens: int, cost: float) -> dict[str, Any]:
    """A backend-reported usage blob with a REAL cost on it.

    ``total_cost_usd`` is set so the assertions pin the summing, not the pricing
    table — a model's per-token price changing must not turn this file red.
    """
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": 0,
        "total_cost_usd": cost,
        "model": "claude-opus-5",
    }


async def _mk_run(**ov: Any):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    d = dict(
        run_id=uuid.uuid4().hex,
        workspace="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key=_skey(_REF),
        user_id=_REF,
        agent_id="agent-xyz",
        client_message_id=uuid.uuid4().hex,
        user_message_id="",
        status="completed",
        user_text="When do you open?",
        partial_text="We open at 8.",
    )
    d.update(ov)
    doc = ChatRunDoc(**d)
    await doc.insert()
    return doc


def _fake_user(role: str, workspace_id: str = "ws-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=_USER_ID,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    return PawBarStore(tmp_path / "stats.db")


@pytest_asyncio.fixture
async def client(mongo_db, store, monkeypatch):
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[current_active_user] = lambda: _fake_user("admin")
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


# --------------------------------------------------------------------------- #
# Volume and money
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_scoreboard_sums_tokens_and_cost_across_the_site(client):
    """The headline numbers. Two turns, one bill."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(usage=_usage(1_000, 200, 0.03))
    await _mk_run(usage=_usage(500, 100, 0.01))

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/stats")).json()

    assert body["runs"] == 2
    assert body["priced_runs"] == 2
    assert body["tokens"]["input"] == 1_500
    assert body["tokens"]["output"] == 300
    assert body["tokens"]["total"] == 1_800
    assert body["cost_usd"] == pytest.approx(0.04)
    # A run carries the visitor's question AND the agent's reply.
    assert body["messages"] == 4


@pytest.mark.asyncio
async def test_a_site_whose_backend_reports_no_usage_reads_as_unpriced(client):
    """ "We cannot price this" and "this was free" are different claims.

    ``priced_runs`` is what lets the panel say which one it is, so a site on a
    backend that reports no metering must not render a confident $0.00.
    """
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run()

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/stats")).json()

    assert body["runs"] == 1
    assert body["priced_runs"] == 0
    assert body["cost_usd"] == 0.0
    assert body["tokens"]["total"] == 0


# --------------------------------------------------------------------------- #
# Counting the right thing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_conversations_and_visitors_are_counted_separately(client):
    """A visitor may hold several threads, so these are two numbers.

    Reporting one for both is the same collapse the owner's inbox was fixed for
    on 2026-08-19 — it under-reports exactly the sites that are working.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    first = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    second = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await _mk_run(session_key=_skey(first.id))
    await _mk_run(session_key=_skey(second.id))
    await _mk_run(session_key=_skey("conv-other"), user_id="cust-0002")

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/stats")).json()

    assert body["visitors"] == 2
    assert body["conversations"] == 3


@pytest.mark.asyncio
async def test_a_pre_identity_run_still_counts_as_a_conversation(client):
    """Its session_key names the visitor, not a conversation. It is still a
    conversation, and dropping it would under-report every site that predates
    conversation identity."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(session_key=_skey(_REF))

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/stats")).json()

    assert body["conversations"] == 1
    assert body["visitors"] == 1


# --------------------------------------------------------------------------- #
# Isolation and windows
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_sibling_sites_runs_never_reach_this_scoreboard(client):
    """Two of the customer's own sites sharing a workspace is the normal case,
    and one paying for the other's compute is a billing leak."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(usage=_usage(100, 10, 0.05))
    await _mk_run(
        scope_id="pocket-2",
        session_key=_skey("conv-elsewhere", pocket_id="pocket-2"),
        usage=_usage(9_000, 9_000, 9.99),
    )

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/stats")).json()

    assert body["runs"] == 1
    assert body["cost_usd"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_the_window_excludes_older_runs(client):
    """The filter an owner actually uses: last week versus last month."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(usage=_usage(100, 10, 0.01))
    await _mk_run(
        createdAt=datetime.now(UTC) - timedelta(days=10),
        usage=_usage(200, 20, 0.02),
    )

    week = (await c.get(f"/paw-bar/admin/site/{site.id}/stats?window=7d")).json()
    month = (await c.get(f"/paw-bar/admin/site/{site.id}/stats?window=30d")).json()
    ever = (await c.get(f"/paw-bar/admin/site/{site.id}/stats?window=all")).json()

    assert week["runs"] == 1
    assert week["cost_usd"] == pytest.approx(0.01)
    assert month["runs"] == 2
    assert ever["runs"] == 2
    assert ever["since"] == ""


@pytest.mark.asyncio
async def test_a_malformed_window_is_refused_not_reinterpreted(client):
    """Answering an unparseable window with the whole history is the largest
    possible reinterpretation of a question nobody asked."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())

    res = await c.get(f"/paw-bar/admin/site/{site.id}/stats?window=lastweek")

    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Honesty under limits
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_capped_scan_says_it_was_capped(client, monkeypatch):
    """The scan is bounded, so a busy site's figure is a floor. Rendering a
    partial scan as a total is the one thing an analytics panel must not do."""
    c, store = client
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._STATS_SCAN_CAP", 1)
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(usage=_usage(100, 10, 0.01))
    await _mk_run(usage=_usage(100, 10, 0.01))

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/stats")).json()

    assert body["runs"] == 1
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_the_queue_numbers_come_from_the_widget_not_the_runs(client):
    """The per-state totals are the SAME source as the inbox chips.

    Deriving them from the run scan instead would drift the moment an owner
    closed a thread — the panel would keep calling it open because a run for it
    is still inside the window.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await store.ensure_conversation(widget.id, _REF, "ws-1")
    await store.update_conversation(widget.id, _REF, "ws-1", state="closed")
    await _mk_run()

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/stats")).json()

    assert body["states"].get("closed") == 1
    assert body["runs"] == 1
