# tests/cloud/growth/test_email_dispatch.py — the email branch of
# ``growth.dispatch`` (G-5): an approved draft actually leaves the building.
#
# What this suite proves:
#
#   1. HAPPY PATH — an ``approved`` email draft calls the Mailtrap send API
#      exactly ONCE with the right payload (from-address on the configured
#      secondary sending domain, recipient, subject, body, ``Api-Token``
#      header), flips the draft approved→sent through the EXISTING
#      ``service.gate_transition`` seam, and writes a ``sent`` MessageLog row
#      carrying the provider's message id.
#   2. GATE — a draft in ANY other status (draft / proposed / sent / replied /
#      rejected) makes NO send call and changes no state. Parametrized; this is
#      the dispatcher's half of the send gate.
#   3. FAILURE — a connector error writes ``MessageLog(outcome="failed")``,
#      leaves the draft ``approved`` (retryable — the human approval stands),
#      and lets NO exception escape the job.
#   4. CREDENTIAL HYGIENE — the workspace's Mailtrap token never appears in
#      logs, in the MessageLog row, or in any value the dispatch path returns,
#      even when the provider echoes it back in an error body (grep-style
#      assertion over every captured surface).
#   5. SENDING DOMAIN — an unset ``GROWTH_SENDING_DOMAIN``, a from-address off
#      that domain, and a sending domain that IS the deployment's apex all fail
#      closed with no HTTP call.
#
# NETWORK-FREE: every send goes through ``httpx.MockTransport`` injected at the
# ``connector._http_client`` seam. The credential itself is read the real way —
# through the cloud connector state store, off a ``WorkspaceConnector`` row in
# the mongomock DB — so the connector-state pattern is exercised, not faked.
#
# Created 2026-07-27 (feat/growth-g5): new module.

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from pocketpaw_ee.cloud.growth import connector as growth_connector
from pocketpaw_ee.cloud.growth import email_dispatch
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth import worker as growth_worker
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector
from pocketpaw_ee.cloud.models.draft import Draft as _DraftDoc
from pocketpaw_ee.cloud.models.message_log import MessageLog as _MessageLogDoc
from pocketpaw_ee.cloud.models.prospect import Prospect as _ProspectDoc

WORKSPACE = "w1"
TOKEN = "mt-super-secret-token-9f3a"
SENDING_DOMAIN = "outbound-paw.dev"

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Recorder:
    """Captures the requests the connector puts on the wire."""

    def __init__(self, response_factory) -> None:
        self.requests: list[httpx.Request] = []
        self._response_factory = response_factory

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response_factory(request)


def _ok_response(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "message_ids": ["0c7fd939-0000-4000-8000-000000000001"]}
    )


@pytest.fixture
def sending_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(growth_connector.SENDING_DOMAIN_ENV, SENDING_DOMAIN)
    monkeypatch.setenv(growth_connector.PUBLIC_BASE_URL_ENV, "https://app.pocketpaw.test")


def _install_transport(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler), timeout=5.0)

    monkeypatch.setattr(growth_connector, "_http_client", _client)


async def _seed_connector(config: dict[str, Any] | None = None) -> None:
    """Create the workspace's enabled Mailtrap connector row."""
    await WorkspaceConnector(
        workspace=WORKSPACE,
        name=growth_connector.MAILTRAP_CONNECTOR_NAME,
        enabled=True,
        scope="workspace",
        config=config if config is not None else {growth_connector.TOKEN_KEY: TOKEN},
    ).insert()


async def _seed_draft(status: str = "approved", *, channel: str = "email") -> tuple[str, str]:
    """Insert a prospect + a draft in the given status. Returns (draft_id, prospect_id)."""
    prospect = _ProspectDoc(
        workspace=WORKSPACE,
        name="Sam Founder",
        company="Acme Dental",
        domain="acme-dental.com",
        source="manual",
        emails=["sam@acme-dental.com"],
    )
    await prospect.insert()
    draft = _DraftDoc(
        workspace=WORKSPACE,
        prospect_id=str(prospect.id),
        channel=channel,
        subject="Quick question about Acme Dental",
        body="Hi Sam — noticed you run three clinics. Worth a look?",
        status=status,
    )
    await draft.insert()
    return str(draft.id), str(prospect.id)


async def _logs() -> list[_MessageLogDoc]:
    return await _MessageLogDoc.find({"workspace": WORKSPACE}).to_list()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_draft_sends_once_flips_to_sent_and_logs(mongo_db, sending_env, monkeypatch):
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, prospect_id = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    # Sent exactly once, to the right endpoint, with the right payload.
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert str(request.url) == growth_connector.MAILTRAP_SEND_URL
    assert request.method == "POST"
    assert request.headers["Api-Token"] == TOKEN
    import json

    payload = json.loads(request.content)
    assert payload["from"]["email"] == f"outreach@{SENDING_DOMAIN}"
    assert payload["to"] == [{"email": "sam@acme-dental.com"}]
    assert payload["subject"] == "Quick question about Acme Dental"
    assert payload["text"].startswith("Hi Sam")

    # The draft moved through the gate seam.
    draft = await _DraftDoc.get(draft_id)
    assert draft.status == "sent"

    # One audit row, marked sent, carrying the provider's message id.
    logs = await _logs()
    assert len(logs) == 1
    row = logs[0]
    assert row.outcome == "sent"
    assert row.provider == "mailtrap"
    assert row.channel == "email"
    assert row.draft_id == draft_id
    assert row.prospect_id == prospect_id
    assert row.to_address == "sam@acme-dental.com"
    assert row.provider_message_id == "0c7fd939-0000-4000-8000-000000000001"
    assert row.sent_at is not None
    assert row.error is None


@pytest.mark.asyncio
async def test_worker_dispatch_job_routes_email_to_the_dispatcher(
    mongo_db, sending_env, monkeypatch
):
    """The arq job's ``email`` branch reaches the real delivery path."""
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved")

    await growth_worker.dispatch({}, draft_id, "email")

    assert len(recorder.requests) == 1
    assert (await _DraftDoc.get(draft_id)).status == "sent"


@pytest.mark.asyncio
async def test_from_name_is_included_when_configured(mongo_db, sending_env, monkeypatch):
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector(
        {
            growth_connector.TOKEN_KEY: TOKEN,
            growth_connector.FROM_EMAIL_KEY: f"sam@{SENDING_DOMAIN}",
            growth_connector.FROM_NAME_KEY: "Sam at Paw",
        }
    )
    draft_id, _ = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    import json

    payload = json.loads(recorder.requests[0].content)
    assert payload["from"] == {"email": f"sam@{SENDING_DOMAIN}", "name": "Sam at Paw"}


# ---------------------------------------------------------------------------
# 2. The gate — only ``approved`` sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "proposed", "sent", "replied", "rejected"])
async def test_non_approved_draft_never_sends_and_changes_nothing(
    mongo_db, sending_env, monkeypatch, status
):
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft(status)

    await email_dispatch.dispatch_email(draft_id)

    assert recorder.requests == []
    assert (await _DraftDoc.get(draft_id)).status == status
    assert await _logs() == []


@pytest.mark.asyncio
async def test_missing_draft_is_a_no_op(mongo_db, sending_env, monkeypatch):
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()

    await email_dispatch.dispatch_email("not-an-object-id")

    assert recorder.requests == []
    assert await _logs() == []


@pytest.mark.asyncio
async def test_non_email_channel_draft_is_refused(mongo_db, sending_env, monkeypatch):
    """The email dispatcher never touches another channel's draft."""
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved", channel="linkedin")

    await email_dispatch.dispatch_email(draft_id)

    assert recorder.requests == []
    assert (await _DraftDoc.get(draft_id)).status == "approved"


# ---------------------------------------------------------------------------
# 3. Failure path — retryable, recorded, silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_rejection_logs_failed_and_leaves_the_draft_approved(
    mongo_db, sending_env, monkeypatch
):
    recorder = _Recorder(lambda _r: httpx.Response(422, json={"errors": ["sender not verified"]}))
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved")

    # No exception escapes the job.
    await email_dispatch.dispatch_email(draft_id)

    assert len(recorder.requests) == 1
    assert (await _DraftDoc.get(draft_id)).status == "approved"  # retryable
    logs = await _logs()
    assert len(logs) == 1
    assert logs[0].outcome == "failed"
    assert logs[0].sent_at is None
    assert logs[0].provider_message_id is None
    assert "422" in (logs[0].error or "")


@pytest.mark.asyncio
async def test_transport_error_logs_failed_without_raising(mongo_db, sending_env, monkeypatch):
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    recorder = _Recorder(_boom)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    assert (await _DraftDoc.get(draft_id)).status == "approved"
    logs = await _logs()
    assert len(logs) == 1
    assert logs[0].outcome == "failed"
    assert "ConnectError" in (logs[0].error or "")


@pytest.mark.asyncio
async def test_unconfigured_connector_fails_closed_with_no_http_call(
    mongo_db, sending_env, monkeypatch
):
    """No connector row at all — nothing goes on the wire."""
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    draft_id, _ = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    assert recorder.requests == []
    assert (await _DraftDoc.get(draft_id)).status == "approved"
    logs = await _logs()
    assert len(logs) == 1
    assert logs[0].outcome == "failed"
    assert "not configured" in (logs[0].error or "")


@pytest.mark.asyncio
async def test_disabled_connector_revokes_sending(mongo_db, sending_env, monkeypatch):
    """Disabling the connector row immediately stops sends — the state store
    only resolves ENABLED rows, so there is no separate kill switch."""
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await WorkspaceConnector(
        workspace=WORKSPACE,
        name=growth_connector.MAILTRAP_CONNECTOR_NAME,
        enabled=False,
        scope="workspace",
        config={growth_connector.TOKEN_KEY: TOKEN},
    ).insert()
    draft_id, _ = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    assert recorder.requests == []
    assert (await _DraftDoc.get(draft_id)).status == "approved"


@pytest.mark.asyncio
async def test_prospect_without_an_email_fails_closed(mongo_db, sending_env, monkeypatch):
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    prospect = _ProspectDoc(
        workspace=WORKSPACE,
        name="No Email",
        company="Acme",
        domain="acme.com",
        source="manual",
        emails=[],
    )
    await prospect.insert()
    draft = _DraftDoc(
        workspace=WORKSPACE,
        prospect_id=str(prospect.id),
        channel="email",
        subject="Hello",
        body="Body",
        status="approved",
    )
    await draft.insert()

    await email_dispatch.dispatch_email(str(draft.id))

    assert recorder.requests == []
    logs = await _logs()
    assert len(logs) == 1 and logs[0].outcome == "failed"


# ---------------------------------------------------------------------------
# 4. Credential hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_never_reaches_logs_or_the_audit_row_even_when_echoed(
    mongo_db, sending_env, monkeypatch, caplog
):
    """The provider echoes the token in its error body — nothing persists it.

    Grep-style: after a full failing dispatch, the token must not appear in any
    captured log record, in any MessageLog field, or in the draft row.
    """

    def _echoing_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid api token: {TOKEN}")

    recorder = _Recorder(_echoing_error)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved")

    with caplog.at_level(logging.DEBUG):
        await email_dispatch.dispatch_email(draft_id)

    logged = "\n".join(
        [r.getMessage() for r in caplog.records] + [str(r.args) for r in caplog.records]
    )
    assert TOKEN not in logged

    logs = await _logs()
    assert len(logs) == 1
    assert TOKEN not in str(logs[0].model_dump())
    assert logs[0].outcome == "failed"

    draft = await _DraftDoc.get(draft_id)
    assert TOKEN not in str(draft.model_dump())


@pytest.mark.asyncio
async def test_success_path_leaves_no_token_in_logs(mongo_db, sending_env, monkeypatch, caplog):
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved")

    with caplog.at_level(logging.DEBUG):
        await email_dispatch.dispatch_email(draft_id)

    logged = "\n".join(
        [r.getMessage() for r in caplog.records] + [str(r.args) for r in caplog.records]
    )
    assert TOKEN not in logged
    assert TOKEN not in str((await _logs())[0].model_dump())


@pytest.mark.asyncio
async def test_draft_response_dto_carries_no_credential(mongo_db, sending_env, monkeypatch):
    """The public draft envelope after a send is credential-free."""
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    response = await growth_service.gate_transition(WORKSPACE, draft_id, "replied")
    assert TOKEN not in response.model_dump_json()


# ---------------------------------------------------------------------------
# 5. Sending-domain guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unset_sending_domain_refuses_to_send(mongo_db, monkeypatch):
    monkeypatch.delenv(growth_connector.SENDING_DOMAIN_ENV, raising=False)
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector()
    draft_id, _ = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    assert recorder.requests == []
    assert (await _DraftDoc.get(draft_id)).status == "approved"
    assert growth_connector.SENDING_DOMAIN_ENV in ((await _logs())[0].error or "")


@pytest.mark.asyncio
async def test_from_address_off_the_sending_domain_refuses_to_send(
    mongo_db, sending_env, monkeypatch
):
    recorder = _Recorder(_ok_response)
    _install_transport(monkeypatch, recorder)
    await _seed_connector(
        {
            growth_connector.TOKEN_KEY: TOKEN,
            growth_connector.FROM_EMAIL_KEY: "sam@pocketpaw.test",  # the apex, not the secondary
        }
    )
    draft_id, _ = await _seed_draft("approved")

    await email_dispatch.dispatch_email(draft_id)

    assert recorder.requests == []
    assert (await _DraftDoc.get(draft_id)).status == "approved"
    assert "sending domain" in ((await _logs())[0].error or "")


def test_sending_domain_may_not_be_the_deployment_apex(monkeypatch):
    """Cold outreach on the apex gambles the transactional domain's reputation."""
    monkeypatch.setenv(growth_connector.PUBLIC_BASE_URL_ENV, "https://app.pocketpaw.test")
    monkeypatch.setenv(growth_connector.SENDING_DOMAIN_ENV, "app.pocketpaw.test")

    with pytest.raises(growth_connector.EmailSendError, match="apex"):
        growth_connector.resolve_sending_domain()


def test_sending_domain_is_normalised(monkeypatch):
    monkeypatch.setenv(growth_connector.PUBLIC_BASE_URL_ENV, "https://app.pocketpaw.test")
    monkeypatch.setenv(growth_connector.SENDING_DOMAIN_ENV, "  WWW.Outbound-Paw.dev. ")

    assert growth_connector.resolve_sending_domain() == "outbound-paw.dev"
