# tests/cloud/growth/test_drafts.py — HTTP-layer tests for the G-3 drafts
# slice (``ee/cloud/growth/{router,service}.py``). Same harness as
# test_router.py: FastAPI app + fixed RequestContext override per workspace
# (w1/w2 clients), mongomock-backed Beanie via the shared ``mongo_db``
# fixture. Covers: one prospect fully drafted across the three channels, the
# prospect.status flip to ``drafted`` (and no regression from later
# statuses), the legal lifecycle chain, parametrized illegal transitions
# (422 ``draft.illegal_transition``), DTO-boundary validation (blank body,
# subject on a non-email channel), list filters, and tenancy (foreign
# prospect → 404 on create, foreign draft ids 404, lists never leak).
#
# Created 2026-07-27 (feat/growth-g3): third slice of /growth — drafts.
# Updated 2026-07-27 (feat/growth-g4): the Instinct send gate owns the
# ``approved`` / ``sent`` edges now — the public status route refuses them
# with 403 ``draft.gate_required`` (new tests below). Lifecycle walks that
# previously drove those edges over HTTP route the gate-owned hops through
# ``service.gate_transition`` — the exact seam the gate machinery uses (the
# full propose→approve HTTP path is covered in test_gate.py). The 422
# illegal-transition coverage is unchanged: every parametrized attempt stays
# illegal per the table and still asserts the 422 via the public route.

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


# G-4 — the growth routes now carry real RBAC guards
# (``growth.read`` / ``growth.write`` / ``growth.manage``), so the test app has
# to supply an authenticated user + active workspace the guard can resolve.
# ``role`` drives the guard's verdict, so a test can drive an under-privileged
# caller by building the app at a lower tier.
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


def _build_app(
    workspace_id: str | None = "w1", user_id: str = "u1", role: str = "admin"
) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(growth_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    user = _FakeUser(user_id, workspace_id or "w1", role=role)
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
    }
    base.update(overrides)
    return base


def _draft_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "channel": "email",
        "subject": "Quick idea for Acme Dental's booking flow",
        "body": "Saw your online booking stops at a contact form — here's a live demo.",
    }
    base.update(overrides)
    return base


async def _create_prospect(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    resp = await client.post("/api/v1/growth/prospects", json=_prospect_payload(**overrides))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_draft(client: AsyncClient, prospect_id: str, **overrides: Any) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/growth/prospects/{prospect_id}/drafts", json=_draft_payload(**overrides)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# G-4 — the gate-owned edges. The public status route 403s these targets;
# lifecycle walks route them through the service's gate seam instead.
_GATE_OWNED = {"approved", "sent"}


async def _walk(
    client: AsyncClient, draft_id: str, *statuses: str, workspace_id: str = "w1"
) -> dict[str, Any]:
    """Walk a draft through a sequence of legal transitions.

    Public edges go through the HTTP status route; gate-owned edges
    (``approved`` / ``sent``) go through ``service.gate_transition`` — the
    same seam the Instinct gate machinery uses (G-4; the full
    propose→approve HTTP path is exercised in test_gate.py)."""
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


# ---------------------------------------------------------------------------
# Create — three channels on one prospect, defaults, validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_prospect_fully_drafted_across_three_channels(w1_client):
    """The G-3 acceptance slice: a qualified prospect carries an email, a
    linkedin, and a whatsapp draft, all born in ``status=draft``."""
    prospect = await _create_prospect(w1_client)
    email = await _create_draft(w1_client, prospect["id"])
    linkedin = await _create_draft(
        w1_client,
        prospect["id"],
        channel="linkedin",
        subject=None,
        body="Loved the smile-gallery on your site — building tools for clinics like yours.",
    )
    whatsapp = await _create_draft(
        w1_client,
        prospect["id"],
        channel="whatsapp",
        subject=None,
        body="Hi Sam! Following up on your reply — happy to walk you through the demo.",
        variant="follow_up",
    )

    assert email["channel"] == "email"
    assert email["subject"] is not None
    assert linkedin["channel"] == "linkedin"
    assert linkedin["subject"] is None
    assert whatsapp["channel"] == "whatsapp"
    assert whatsapp["variant"] == "follow_up"
    for draft in (email, linkedin, whatsapp):
        assert draft["status"] == "draft"
        assert draft["workspace_id"] == "w1"
        assert draft["prospect_id"] == prospect["id"]
        assert draft["id"]
        assert draft["created_at"]

    listed = await w1_client.get("/api/v1/growth/drafts", params={"prospect_id": prospect["id"]})
    assert listed.status_code == 200
    assert {d["channel"] for d in listed.json()} == {"email", "linkedin", "whatsapp"}


@pytest.mark.asyncio
async def test_draft_defaults(w1_client):
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    assert draft["variant"] == "first_touch"
    assert draft["status"] == "draft"
    assert draft["demo_url"] is None


@pytest.mark.asyncio
async def test_first_draft_flips_prospect_status_to_drafted(w1_client):
    """A ``new`` prospect flips to ``drafted`` on its first draft; the same
    holds for ``qualified``."""
    for domain, patch in (("alpha.io", None), ("beta.io", {"status": "qualified"})):
        prospect = await _create_prospect(w1_client, domain=domain, company=domain)
        if patch:
            resp = await w1_client.patch(f"/api/v1/growth/prospects/{prospect['id']}", json=patch)
            assert resp.status_code == 200
        await _create_draft(w1_client, prospect["id"])
        fetched = (await w1_client.get(f"/api/v1/growth/prospects/{prospect['id']}")).json()
        assert fetched["status"] == "drafted", domain


@pytest.mark.asyncio
async def test_draft_never_regresses_a_later_prospect_status(w1_client):
    """A prospect already past ``drafted`` (e.g. ``replied``) keeps its
    status when another draft (a follow_up) is added."""
    prospect = await _create_prospect(w1_client)
    resp = await w1_client.patch(
        f"/api/v1/growth/prospects/{prospect['id']}", json={"status": "replied"}
    )
    assert resp.status_code == 200
    await _create_draft(w1_client, prospect["id"], variant="follow_up")
    fetched = (await w1_client.get(f"/api/v1/growth/prospects/{prospect['id']}")).json()
    assert fetched["status"] == "replied"


@pytest.mark.asyncio
async def test_create_draft_rejects_blank_body(w1_client):
    prospect = await _create_prospect(w1_client)
    resp = await w1_client.post(
        f"/api/v1/growth/prospects/{prospect['id']}/drafts",
        json=_draft_payload(body="   "),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["linkedin", "whatsapp"])
async def test_subject_is_email_only(w1_client, channel):
    prospect = await _create_prospect(w1_client)
    resp = await w1_client.post(
        f"/api/v1/growth/prospects/{prospect['id']}/drafts",
        json=_draft_payload(channel=channel, subject="Should not be here"),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_draft_rejects_unknown_channel(w1_client):
    prospect = await _create_prospect(w1_client)
    resp = await w1_client.post(
        f"/api/v1/growth/prospects/{prospect['id']}/drafts",
        json=_draft_payload(channel="carrier_pigeon", subject=None),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Lifecycle — legal chain green, illegal moves 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legal_chain_walks_green(w1_client):
    """draft→proposed→approved→sent→replied — the full legal chain. The
    public hops are 200s; the gate-owned hops (approved/sent) walk through
    the gate seam (G-4)."""
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    final = await _walk(w1_client, draft["id"], "proposed", "approved", "sent", "replied")
    assert final["status"] == "replied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain", "attempt"),
    [
        (("proposed",), "approved"),  # the gate owns proposed→approved
        (("proposed", "approved"), "sent"),  # the dispatch worker owns approved→sent
    ],
)
async def test_gate_owned_edges_forbidden_on_public_route(w1_client, chain, attempt):
    """G-4 gate integrity: ``approved`` / ``sent`` are LEGAL per the table but
    REFUSED on the public status route (403 ``draft.gate_required``) — only an
    approved ``_growth_send`` Instinct proposal (and, downstream, the dispatch
    worker) may set them. The draft does not move."""
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    await _walk(w1_client, draft["id"], *chain)

    resp = await w1_client.post(
        f"/api/v1/growth/drafts/{draft['id']}/status", json={"status": attempt}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "draft.gate_required"

    listed = (
        await w1_client.get("/api/v1/growth/drafts", params={"prospect_id": prospect["id"]})
    ).json()
    assert listed[0]["status"] == chain[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["draft", "proposed", "approved", "sent"])
async def test_any_nonterminal_can_be_rejected(w1_client, start):
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    chain = {
        "draft": (),
        "proposed": ("proposed",),
        "approved": ("proposed", "approved"),
        "sent": ("proposed", "approved", "sent"),
    }[start]
    if chain:
        await _walk(w1_client, draft["id"], *chain)
    final = await _walk(w1_client, draft["id"], "rejected")
    assert final["status"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain", "attempt"),
    [
        # Skipping ahead.
        ((), "approved"),
        ((), "sent"),
        ((), "replied"),
        (("proposed",), "sent"),
        # Going backwards.
        (("proposed",), "draft"),
        (("proposed", "approved"), "proposed"),
        (("proposed", "approved", "sent"), "approved"),
        # Self-transition.
        ((), "draft"),
        # Terminal states are terminal.
        (("proposed", "approved", "sent", "replied"), "rejected"),
        (("proposed", "approved", "sent", "replied"), "draft"),
        (("proposed", "approved", "sent", "replied"), "sent"),
        (("rejected",), "proposed"),
        (("rejected",), "rejected"),
    ],
)
async def test_illegal_transition_is_422(w1_client, chain, attempt):
    """Any move outside the machine — skips, reversals, self-loops, exits from
    a terminal state — is a 422 ``draft.illegal_transition`` and does not
    mutate the draft."""
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    if chain:
        await _walk(w1_client, draft["id"], *chain)
    before = chain[-1] if chain else "draft"

    resp = await w1_client.post(
        f"/api/v1/growth/drafts/{draft['id']}/status", json={"status": attempt}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "draft.illegal_transition"

    listed = (
        await w1_client.get("/api/v1/growth/drafts", params={"prospect_id": prospect["id"]})
    ).json()
    assert listed[0]["status"] == before


@pytest.mark.asyncio
async def test_transition_rejects_unknown_status_name(w1_client):
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])
    resp = await w1_client.post(
        f"/api/v1/growth/drafts/{draft['id']}/status", json={"status": "shipped"}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_drafts_filters_by_channel_and_status(w1_client):
    p1 = await _create_prospect(w1_client, domain="alpha.io", company="Alpha")
    p2 = await _create_prospect(w1_client, domain="beta.io", company="Beta")
    email = await _create_draft(w1_client, p1["id"])
    await _create_draft(w1_client, p1["id"], channel="linkedin", subject=None, body="Connect note")
    await _create_draft(w1_client, p2["id"], channel="linkedin", subject=None, body="Other note")
    await _walk(w1_client, email["id"], "proposed")

    resp = await w1_client.get("/api/v1/growth/drafts")
    assert len(resp.json()) == 3

    resp = await w1_client.get("/api/v1/growth/drafts", params={"channel": "linkedin"})
    assert {d["prospect_id"] for d in resp.json()} == {p1["id"], p2["id"]}

    resp = await w1_client.get("/api/v1/growth/drafts", params={"status": "proposed"})
    assert [d["id"] for d in resp.json()] == [email["id"]]

    resp = await w1_client.get(
        "/api/v1/growth/drafts", params={"prospect_id": p1["id"], "channel": "linkedin"}
    )
    assert len(resp.json()) == 1

    resp = await w1_client.get("/api/v1/growth/drafts", params={"channel": "fax"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tenancy — foreign prospects 404 on create, drafts never leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_draft_on_foreign_workspace_prospect_is_404(w1_client, w2_client):
    """w2 cannot attach a draft to w1's prospect — identical 404, existence
    never leaks, and nothing is written."""
    prospect = await _create_prospect(w1_client)
    resp = await w2_client.post(
        f"/api/v1/growth/prospects/{prospect['id']}/drafts", json=_draft_payload()
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "prospect.not_found"
    assert (await w1_client.get("/api/v1/growth/drafts")).json() == []
    assert (await w2_client.get("/api/v1/growth/drafts")).json() == []


@pytest.mark.asyncio
async def test_drafts_are_tenant_scoped(w1_client, w2_client):
    """w1's drafts never appear in w2's list, and w2 cannot read or move
    w1's draft by id."""
    prospect = await _create_prospect(w1_client)
    draft = await _create_draft(w1_client, prospect["id"])

    assert (await w2_client.get("/api/v1/growth/drafts")).json() == []

    resp = await w2_client.post(
        f"/api/v1/growth/drafts/{draft['id']}/status", json={"status": "proposed"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "draft.not_found"

    # The cross-tenant attempt must not have moved the draft.
    listed = (await w1_client.get("/api/v1/growth/drafts")).json()
    assert listed[0]["status"] == "draft"
