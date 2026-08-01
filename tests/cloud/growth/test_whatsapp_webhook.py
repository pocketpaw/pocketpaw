# tests/cloud/growth/test_whatsapp_webhook.py — the G-6 inbound MSG91 webhook
# (``ee/cloud/growth/webhooks.py``).
#
# The endpoint is unauthenticated by nature (MSG91 is the caller) and it is the
# ONLY thing that can set ``prospect.opted_in = True`` — i.e. the only thing
# that can unlock business-initiated template sends to a number. So the suite
# leads with the fail-closed cases: a forged, unsigned, or unverifiable request
# must never reach the service.
#
# What it proves:
#   1. FAIL CLOSED — bad signature, missing header, and an unset webhook secret
#      are all 403, and nothing is mutated in any of those cases.
#   2. A verified reply from a known number opts the prospect in, moves them to
#      ``replied``, and walks their ``sent`` WhatsApp draft to ``replied``.
#   3. An unknown number is a 200 no-op whose response body is byte-identical to
#      the known-number response — the endpoint is not a membership oracle over
#      phone numbers.
#   4. Delivery-status callbacks are ignored (a receipt is not consent).
#   5. Number spellings (+prefix, spaces, dashes) still match the stored row.
#
# Harness: the webhook router alone in a bare FastAPI app (no auth, no license,
# no RequestContext — matching how it is mounted in ``ee/cloud/__init__.py``),
# with mongomock-backed Beanie via the shared ``mongo_db`` fixture. Requests are
# signed by the test itself over the exact bytes posted.
#
# Created 2026-07-27 (feat/growth-g6): new module.

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.dto import CreateDraftRequest, CreateProspectRequest
from pocketpaw_ee.cloud.growth.webhooks import router as growth_webhooks_router

WEBHOOK_SECRET = "test-msg91-webhook-secret"
NUMBER = "919876543210"


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROWTH_MSG91_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest_asyncio.fixture
async def client(mongo_db: Any) -> AsyncClient:  # noqa: ARG001 — forces Beanie init
    app = FastAPI()
    add_error_handler(app)
    app.include_router(growth_webhooks_router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _post(
    client: AsyncClient,
    payload: dict[str, Any],
    *,
    signature: str | None = "__valid__",
) -> Any:
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if signature == "__valid__":
        headers["X-Msg91-Signature"] = _sign(body)
    elif signature is not None:
        headers["X-Msg91-Signature"] = signature
    return await client.post("/api/v1/growth/webhooks/msg91", content=body, headers=headers)


def _ctx(workspace_id: str = "w1") -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _seed_sent_outreach(
    *,
    workspace_id: str = "w1",
    number: str | None = NUMBER,
    domain: str = "acme-dental.com",
    send_it: bool = True,
) -> tuple[Any, Any]:
    """A prospect we WhatsApp'd, with the draft parked in ``sent``."""
    prospect = await growth_service.create(
        _ctx(workspace_id),
        CreateProspectRequest(
            name="Sam Founder",
            company="Acme Dental",
            domain=domain,
            source="manual",
            whatsapp_number=number,
            opted_in=False,
        ),
    )
    draft = await growth_service.create_draft(
        _ctx(workspace_id),
        prospect.id,
        CreateDraftRequest(channel="whatsapp", body="2 min demo?"),
    )
    await growth_service.gate_transition(workspace_id, draft.id, "proposed")
    await growth_service.gate_transition(workspace_id, draft.id, "approved")
    if send_it:
        await growth_service.gate_transition(workspace_id, draft.id, "sent")
    return prospect, draft


async def _reload_prospect(prospect_id: str, workspace_id: str = "w1") -> Any:
    return await growth_service.get(_ctx(workspace_id), prospect_id)


async def _draft_status(draft_id: str, workspace_id: str = "w1") -> str:
    drafts = await growth_service.list_drafts(_ctx(workspace_id))
    return next(d.status for d in drafts if d.id == draft_id)


def _inbound(number: str = NUMBER) -> dict[str, Any]:
    return {"type": "message", "customer_number": number, "content": {"text": "yes, interested"}}


# ---------------------------------------------------------------------------
# 1 — fail closed
# ---------------------------------------------------------------------------


class TestFailsClosed:
    async def test_bad_signature_is_rejected(self, client: AsyncClient) -> None:
        prospect, draft = await _seed_sent_outreach()

        resp = await _post(client, _inbound(), signature="deadbeef")

        assert resp.status_code in (401, 403), resp.text
        # Nothing was read from the unverified body.
        assert (await _reload_prospect(prospect.id)).opted_in is False
        assert await _draft_status(draft.id) == "sent"

    async def test_missing_signature_header_is_rejected(self, client: AsyncClient) -> None:
        prospect, _draft = await _seed_sent_outreach()

        resp = await _post(client, _inbound(), signature=None)

        assert resp.status_code in (401, 403), resp.text
        assert (await _reload_prospect(prospect.id)).opted_in is False

    async def test_unconfigured_secret_is_rejected_not_waved_through(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Recall webhook warns-and-accepts when its secret is unset. This
        one must not: an unsigned body here forges consent."""
        monkeypatch.delenv("GROWTH_MSG91_WEBHOOK_SECRET", raising=False)
        prospect, _draft = await _seed_sent_outreach()

        resp = await _post(client, _inbound())

        assert resp.status_code in (401, 403), resp.text
        assert (await _reload_prospect(prospect.id)).opted_in is False

    async def test_a_signature_for_a_different_body_is_rejected(self, client: AsyncClient) -> None:
        """Replaying a valid digest against tampered content must not pass."""
        prospect, _draft = await _seed_sent_outreach()
        other_signature = _sign(json.dumps({"type": "message"}).encode())

        resp = await _post(client, _inbound(), signature=other_signature)

        assert resp.status_code in (401, 403), resp.text
        assert (await _reload_prospect(prospect.id)).opted_in is False

    async def test_a_signature_from_the_wrong_secret_is_rejected(self, client: AsyncClient) -> None:
        prospect, _draft = await _seed_sent_outreach()
        body = json.dumps(_inbound()).encode()

        resp = await _post(client, _inbound(), signature=_sign(body, secret="not-our-secret"))

        assert resp.status_code in (401, 403), resp.text
        assert (await _reload_prospect(prospect.id)).opted_in is False


# ---------------------------------------------------------------------------
# 2 — a verified reply
# ---------------------------------------------------------------------------


class TestVerifiedReply:
    async def test_opts_in_and_flips_statuses(self, client: AsyncClient) -> None:
        prospect, draft = await _seed_sent_outreach()

        resp = await _post(client, _inbound())

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}

        refreshed = await _reload_prospect(prospect.id)
        assert refreshed.opted_in is True
        assert refreshed.status == "replied"
        assert await _draft_status(draft.id) == "replied"

    async def test_sha256_prefixed_signature_is_accepted(self, client: AsyncClient) -> None:
        prospect, _draft = await _seed_sent_outreach()
        body = json.dumps(_inbound()).encode()

        resp = await _post(client, _inbound(), signature=f"sha256={_sign(body)}")

        assert resp.status_code == 200, resp.text
        assert (await _reload_prospect(prospect.id)).opted_in is True

    @pytest.mark.parametrize(
        "stored",
        ["+919876543210", "+91 98765 43210", "919876543210", "+91-98765-43210"],
    )
    async def test_number_spellings_still_match(self, client: AsyncClient, stored: str) -> None:
        prospect, _draft = await _seed_sent_outreach(number=stored)

        resp = await _post(client, _inbound(NUMBER))

        assert resp.status_code == 200, resp.text
        assert (await _reload_prospect(prospect.id)).opted_in is True

    async def test_meta_style_messages_envelope_is_understood(self, client: AsyncClient) -> None:
        prospect, _draft = await _seed_sent_outreach()
        payload = {"data": {"messages": [{"from": NUMBER, "text": {"body": "yes"}}]}}

        resp = await _post(client, payload)

        assert resp.status_code == 200, resp.text
        assert (await _reload_prospect(prospect.id)).opted_in is True

    async def test_reply_from_a_prospect_we_never_messaged_still_opts_them_in(
        self, client: AsyncClient
    ) -> None:
        """An inbound message IS the opt-in signal under Meta's rules, whether
        or not we happened to have a sent draft outstanding."""
        prospect, draft = await _seed_sent_outreach(send_it=False)

        resp = await _post(client, _inbound())

        assert resp.status_code == 200, resp.text
        refreshed = await _reload_prospect(prospect.id)
        assert refreshed.opted_in is True
        assert refreshed.status == "replied"
        # The draft never sent, so it stays where the gate left it.
        assert await _draft_status(draft.id) == "approved"


# ---------------------------------------------------------------------------
# 3 — no membership oracle, no consent from receipts
# ---------------------------------------------------------------------------


class TestNoLeakAndNoFalseConsent:
    async def test_unknown_number_is_an_indistinguishable_200(self, client: AsyncClient) -> None:
        prospect, draft = await _seed_sent_outreach()

        known = await _post(client, _inbound(NUMBER))
        unknown = await _post(client, _inbound("910000000000"))

        assert unknown.status_code == known.status_code == 200
        # Byte-identical: a caller cannot tell whether the number exists.
        assert unknown.content == known.content

        # ...and the unknown number changed nothing about the real prospect
        # beyond what the known-number call already did.
        assert (await _reload_prospect(prospect.id)).opted_in is True
        assert await _draft_status(draft.id) == "replied"

    async def test_unknown_number_mutates_nothing(self, client: AsyncClient) -> None:
        prospect, draft = await _seed_sent_outreach()

        resp = await _post(client, _inbound("910000000000"))

        assert resp.status_code == 200
        assert (await _reload_prospect(prospect.id)).opted_in is False
        assert await _draft_status(draft.id) == "sent"

    @pytest.mark.parametrize("status_type", ["status", "delivered", "read", "sent"])
    async def test_delivery_receipts_are_not_consent(
        self, client: AsyncClient, status_type: str
    ) -> None:
        prospect, draft = await _seed_sent_outreach()

        resp = await _post(client, {"type": status_type, "customer_number": NUMBER})

        assert resp.status_code == 200
        assert (await _reload_prospect(prospect.id)).opted_in is False
        assert await _draft_status(draft.id) == "sent"

    async def test_a_payload_with_no_number_is_ignored(self, client: AsyncClient) -> None:
        prospect, _draft = await _seed_sent_outreach()

        resp = await _post(client, {"type": "message", "content": {"text": "hi"}})

        assert resp.status_code == 200
        assert (await _reload_prospect(prospect.id)).opted_in is False

    async def test_a_reply_only_touches_the_tenant_that_messaged_the_number(
        self, client: AsyncClient
    ) -> None:
        """Two tenants can hold the same prospect. Only the one that actually
        sent to the number learns that a reply came back."""
        messaged, messaged_draft = await _seed_sent_outreach(workspace_id="w1")
        bystander, _ = await _seed_sent_outreach(workspace_id="w2", send_it=False)

        resp = await _post(client, _inbound())

        assert resp.status_code == 200
        assert (await _reload_prospect(messaged.id, "w1")).opted_in is True
        assert await _draft_status(messaged_draft.id, "w1") == "replied"

        untouched = await _reload_prospect(bystander.id, "w2")
        assert untouched.opted_in is False
        assert untouched.status != "replied"
