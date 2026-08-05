# tests/cloud/growth/test_linkedin_queue.py — HTTP-layer tests for the G-8
# LinkedIn manual-queue slice (``ee/cloud/growth/{router,service}.py``). Same
# harness as test_drafts.py: FastAPI app + fixed RequestContext override per
# workspace (w1/w2 clients), mongomock-backed Beanie via the shared
# ``mongo_db`` fixture. Covers: queue membership (linkedin-only,
# proposed/approved-only, newest first, prospect join fields), tenancy (w2's
# queue never sees w1's drafts), mark-sent (approved→sent green;
# proposed→sent 422 ``draft.illegal_transition``; email-channel 422
# ``draft.wrong_channel``; cross-tenant 404; sent drafts leave the queue and
# stay on the normal lifecycle — sent→replied still legal), and the
# ``?format=md`` export (heading, profile link, tier + brief, connect note,
# after-accept message, draft ids, empty-queue shape).
#
# Created 2026-07-27 (feat/growth-g8): LinkedIn manual queue + md export.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.growth.router import router as growth_router
from pocketpaw_ee.cloud.license import require_license


# Integration (growth-v1) — G-4 landed per-route RBAC guards
# (``growth.read`` on the queue, ``growth.manage`` on mark-sent) after this
# slice was branched, so the test app has to supply an authenticated user +
# active workspace the guard can resolve. Mirrors test_drafts.py's harness.
class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str, workspace_id: str, role: str = "admin") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role=role)]


def _make_ctx(workspace_id: str | None, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(growth_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    user = _FakeUser(user_id, workspace_id or "w1")
    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


@pytest_asyncio.fixture
async def w1_client(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _prospect_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": "Sam Founder",
        "company": "Acme Dental",
        "domain": "acme-dental.com",
        "source": "manual",
        "tier": "a",
        "research_brief": "Books via a contact form; no online scheduling.\nSecond line.",
        "linkedin_url": "https://linkedin.com/in/sam-founder",
    }
    base.update(overrides)
    return base


async def _create_prospect(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    resp = await client.post("/api/v1/growth/prospects", json=_prospect_payload(**overrides))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_draft(client: AsyncClient, prospect_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "channel": "linkedin",
        "body": "Loved the smile-gallery on your site — building tools for clinics like yours.",
    }
    base.update(overrides)
    resp = await client.post(f"/api/v1/growth/prospects/{prospect_id}/drafts", json=base)
    assert resp.status_code == 200, resp.text
    return resp.json()


_GATE_OWNED = frozenset({"approved", "sent"})


async def _walk(
    client: AsyncClient, draft_id: str, *statuses: str, workspace_id: str = "w1"
) -> dict[str, Any]:
    """Walk a draft through a sequence of legal transitions.

    Public edges go through the HTTP status route; the gate-owned edges
    (``approved`` / ``sent``) go through ``service.gate_transition`` — G-4
    landed after this slice branched and made those targets reachable only via
    the Instinct gate seam. Mirrors test_drafts.py's helper."""
    from pocketpaw_ee.cloud.growth import service as growth_service

    body: dict[str, Any] = {}
    for status in statuses:
        if status in _GATE_OWNED:
            result = await growth_service.gate_transition(workspace_id, draft_id, status)
            body = result.model_dump()
        else:
            resp = await client.post(
                f"/api/v1/growth/drafts/{draft_id}/status", json={"status": status}
            )
            assert resp.status_code == 200, f"{status}: {resp.text}"
            body = resp.json()
        assert body["status"] == status
    return body


async def _queued_ids(client: AsyncClient) -> list[str]:
    resp = await client.get("/api/v1/growth/linkedin/queue")
    assert resp.status_code == 200, resp.text
    return [item["draft"]["id"] for item in resp.json()]


# ---------------------------------------------------------------------------
# Queue membership + join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_only_linkedin_proposed_or_approved(w1_client, w2_client):
    """The queue holds ONLY the workspace's linkedin drafts in proposed or
    approved — an email draft (even approved), a still-in-``draft`` linkedin
    draft, and another tenant's proposed linkedin draft all stay out."""
    prospect = await _create_prospect(w1_client)
    proposed = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, proposed["id"], "proposed")
    approved = await _create_draft(w1_client, prospect["id"], variant="follow_up")
    await _walk(w1_client, approved["id"], "proposed", "approved")

    # Excluded: linkedin still in `draft`.
    await _create_draft(w1_client, prospect["id"])
    # Excluded: email channel, even when approved.
    email = await _create_draft(
        w1_client, prospect["id"], channel="email", subject="Hi", body="Email body"
    )
    await _walk(w1_client, email["id"], "proposed", "approved")
    # Excluded: another tenant's proposed linkedin draft.
    foreign_prospect = await _create_prospect(w2_client, domain="other.io", company="Other")
    foreign = await _create_draft(w2_client, foreign_prospect["id"])
    await _walk(w2_client, foreign["id"], "proposed", workspace_id="w2")

    assert set(await _queued_ids(w1_client)) == {proposed["id"], approved["id"]}
    assert await _queued_ids(w2_client) == [foreign["id"]]


@pytest.mark.asyncio
async def test_queue_joins_prospect_context_and_orders_newest_first(w1_client):
    p1 = await _create_prospect(w1_client)
    p2 = await _create_prospect(
        w1_client,
        name="Ada Ops",
        company="Beta Clinic",
        domain="beta-clinic.io",
        tier="b",
        linkedin_url=None,
    )
    d1 = await _create_draft(w1_client, p1["id"])
    await _walk(w1_client, d1["id"], "proposed")
    d2 = await _create_draft(w1_client, p2["id"], body="Second note")
    await _walk(w1_client, d2["id"], "proposed", "approved")

    resp = await w1_client.get("/api/v1/growth/linkedin/queue")
    items = resp.json()
    # Newest first: d2 was created after d1.
    assert [i["draft"]["id"] for i in items] == [d2["id"], d1["id"]]

    by_id = {i["draft"]["id"]: i for i in items}
    first = by_id[d1["id"]]
    assert first["prospect_name"] == "Sam Founder"
    assert first["prospect_company"] == "Acme Dental"
    assert first["linkedin_url"] == "https://linkedin.com/in/sam-founder"
    assert first["tier"] == "a"
    assert first["research_brief"].startswith("Books via a contact form")
    assert first["draft"]["status"] == "proposed"
    second = by_id[d2["id"]]
    assert second["linkedin_url"] is None
    assert second["draft"]["status"] == "approved"


# ---------------------------------------------------------------------------
# mark-sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_sent_approved_goes_sent_and_leaves_queue(w1_client):
    """approved→sent via mark-sent, and the sent draft (a) drops off the
    queue and (b) stays on the normal lifecycle — sent→replied is legal."""
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, draft["id"], "proposed", "approved")

    resp = await w1_client.post(f"/api/v1/growth/linkedin/{draft['id']}/mark-sent")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "sent"
    assert await _queued_ids(w1_client) == []

    # Feeds the normal draft lifecycle: sent→replied still works.
    final = await _walk(w1_client, draft["id"], "replied")
    assert final["status"] == "replied"


@pytest.mark.asyncio
async def test_mark_sent_on_proposed_is_422_illegal_transition(w1_client):
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, draft["id"], "proposed")

    resp = await w1_client.post(f"/api/v1/growth/linkedin/{draft['id']}/mark-sent")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "draft.illegal_transition"
    # Unchanged — still queued as proposed.
    assert await _queued_ids(w1_client) == [draft["id"]]


@pytest.mark.asyncio
async def test_mark_sent_on_email_draft_is_422_wrong_channel(w1_client):
    """A non-linkedin draft is rejected on channel BEFORE any status move —
    even an approved email draft cannot ride the linkedin mark-sent route."""
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(
        w1_client, prospect["id"], channel="email", subject="Hi", body="Email body"
    )
    await _walk(w1_client, draft["id"], "proposed", "approved")

    resp = await w1_client.post(f"/api/v1/growth/linkedin/{draft['id']}/mark-sent")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "draft.wrong_channel"
    listed = (await w1_client.get("/api/v1/growth/drafts", params={"channel": "email"})).json()
    assert listed[0]["status"] == "approved"


@pytest.mark.asyncio
async def test_mark_sent_cross_tenant_is_404(w1_client, w2_client):
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, draft["id"], "proposed", "approved")

    resp = await w2_client.post(f"/api/v1/growth/linkedin/{draft['id']}/mark-sent")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "draft.not_found"
    # Untouched for w1.
    assert await _queued_ids(w1_client) == [draft["id"]]


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_md_export_renders_paste_ready_sections(w1_client):
    """?format=md renders one section per prospect: name+company heading,
    profile link, tier + one-line brief, connect note (first_touch) and
    after-accept message (follow_up), each with its draft id."""
    prospect = await _create_prospect(w1_client)
    connect = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, connect["id"], "proposed", "approved")
    follow = await _create_draft(
        w1_client,
        prospect["id"],
        variant="follow_up",
        body="Thanks for connecting, Sam! Here's the 2-min demo I mentioned.",
    )
    await _walk(w1_client, follow["id"], "proposed")

    resp = await w1_client.get("/api/v1/growth/linkedin/queue", params={"format": "md"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    md = resp.text

    assert "## Sam Founder — Acme Dental" in md
    assert "[LinkedIn profile](https://linkedin.com/in/sam-founder)" in md
    # Tier + one-line brief (first line only — the second line stays out).
    assert "Tier A — Books via a contact form; no online scheduling." in md
    assert "Second line." not in md
    assert "Connect note (" in md
    assert "Loved the smile-gallery on your site" in md
    assert "After accept (" in md
    assert "Thanks for connecting, Sam!" in md
    assert f"Draft id: `{connect['id']}`" in md
    assert f"Draft id: `{follow['id']}`" in md
    # Paste-ready: no tables, no HTML.
    assert "|" not in md
    assert "<" not in md.replace("≤", "")


@pytest.mark.asyncio
async def test_md_export_empty_queue(w1_client):
    resp = await w1_client.get("/api/v1/growth/linkedin/queue", params={"format": "md"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# LinkedIn outreach queue" in resp.text
    assert "Queue is empty" in resp.text


@pytest.mark.asyncio
async def test_queue_rejects_unknown_format(w1_client):
    resp = await w1_client.get("/api/v1/growth/linkedin/queue", params={"format": "html"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_md_export_titles_a_nameless_prospect_with_its_domain(w1_client):
    """A prospect can be just a domain. The export must degrade to something
    TRUE — the domain — never to a placeholder like "Unknown — Unknown"."""
    prospect = await _create_prospect(w1_client, name="", company="")
    draft = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, draft["id"], "proposed")

    md = (await w1_client.get("/api/v1/growth/linkedin/queue", params={"format": "md"})).text
    assert "## acme-dental.com" in md
    assert "unknown" not in md.lower()
    assert "—" not in md.splitlines()[2]  # no orphaned dash on the heading line


@pytest.mark.asyncio
async def test_md_export_titles_a_half_known_prospect_with_what_it_has(w1_client):
    """Company known, contact not: the heading is the company alone."""
    prospect = await _create_prospect(w1_client, name="")
    draft = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, draft["id"], "proposed")

    md = (await w1_client.get("/api/v1/growth/linkedin/queue", params={"format": "md"})).text
    assert "## Acme Dental" in md
    assert "## Acme Dental —" not in md
