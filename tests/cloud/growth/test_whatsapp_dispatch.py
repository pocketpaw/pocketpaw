# tests/cloud/growth/test_whatsapp_dispatch.py — the G-6 WhatsApp dispatch
# slice (``ee/cloud/growth/{whatsapp,msg91,worker,service}.py``).
#
# This is the COMPLIANCE suite. The load-bearing assertion in the whole file is
# ``fake_client.calls == []`` on the not-opted-in path: Meta bans WABA numbers
# that send business-initiated templates to people who never consented, so
# "the provider was never called" has to be provable, not inferred from a
# status field. Everything else here exists to keep that guarantee honest.
#
# What it proves:
#   1. An opted-in approved draft sends exactly once, flips approved→sent
#      through the gate seam, and writes a ``sent`` compliance row.
#   2. A NOT-opted-in prospect makes ZERO provider calls, raises the typed
#      ``OptInRequired``, leaves the draft ``approved``, and writes a
#      ``blocked``/``not_opted_in`` row. A companion test booby-traps credential
#      resolution to prove the guard fires before credentials are even read.
#   3. A draft that isn't ``approved`` (the gate didn't clear it) never sends.
#   4. The hourly cap refuses the N+1th send with no provider call — and
#      refused attempts do NOT consume the window.
#   5. A provider failure is recorded as ``failed`` and leaves the draft
#      ``approved`` (the approval stands, the delivery didn't).
#   6. Credentials never reach a log record, a response, or the send log —
#      including through ``repr()``, the usual leak path.
#   7. The arq worker routes ``channel="whatsapp"`` into the real branch.
#
# Network-free by construction: the suite monkeypatches ``whatsapp._build_client``,
# so ``Msg91WhatsAppClient`` (the only thing that imports httpx) is never
# constructed. No ``httpx`` mocking, no recorded cassettes, no socket.
#
# Created 2026-07-27 (feat/growth-g6): new module.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth import whatsapp as growth_whatsapp
from pocketpaw_ee.cloud.growth.dto import CreateDraftRequest, CreateProspectRequest
from pocketpaw_ee.cloud.growth.msg91 import Msg91Credentials, Msg91Error, Msg91NotConfigured

# A distinctive sentinel so a substring search over logs / serialised output is
# a meaningful leak assertion rather than a coincidence.
SECRET_AUTHKEY = "msg91-authkey-DO-NOT-LEAK-8f3a91"

_CREDS = Msg91Credentials(
    authkey=SECRET_AUTHKEY,
    integrated_number="919000000000",
    template_name="paw_first_touch",
)


class FakeMsg91Client:
    """Records every send instead of making one. The assertion surface for
    "no provider call happened" is simply ``calls == []``."""

    def __init__(self, credentials: Msg91Credentials, *, raises: Exception | None = None) -> None:
        self.credentials = credentials
        self.calls: list[tuple[str, str]] = []
        self._raises = raises

    async def send_template(self, *, to_number: str, body_text: str) -> str:
        self.calls.append((to_number, body_text))
        if self._raises is not None:
            raise self._raises
        return f"msg91-{len(self.calls)}"


class ExplodingCredentials:
    """Sentinel: any credential resolution at all is a test failure.

    Used to prove the opt-in guard short-circuits BEFORE the provider stack is
    touched — not merely before the HTTP call.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, workspace_id: str) -> Msg91Credentials:
        self.calls += 1
        raise AssertionError(
            "credentials were resolved for a send that should have been refused earlier"
        )


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeMsg91Client:
    """Install a fake provider client + stub credentials at the dispatch seams."""
    client = FakeMsg91Client(_CREDS)

    async def _resolve(workspace_id: str) -> Msg91Credentials:
        return _CREDS

    monkeypatch.setattr(growth_whatsapp, "resolve_credentials", _resolve)
    monkeypatch.setattr(growth_whatsapp, "_build_client", lambda creds: client)
    return client


def _ctx(workspace_id: str, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _seed_prospect(
    workspace_id: str = "w1",
    *,
    domain: str = "acme-dental.com",
    opted_in: bool = True,
    number: str | None = "919876543210",
) -> Any:
    return await growth_service.create(
        _ctx(workspace_id),
        CreateProspectRequest(
            name="Sam Founder",
            company="Acme Dental",
            domain=domain,
            source="manual",
            whatsapp_number=number,
            opted_in=opted_in,
        ),
    )


async def _seed_draft(
    workspace_id: str,
    prospect_id: str,
    *,
    body: str = "Saw your booking flow stops at a contact form — 2 min demo?",
    approve: bool = True,
) -> Any:
    draft = await growth_service.create_draft(
        _ctx(workspace_id),
        prospect_id,
        CreateDraftRequest(channel="whatsapp", body=body),
    )
    await growth_service.gate_transition(workspace_id, draft.id, "proposed")
    if approve:
        await growth_service.gate_transition(workspace_id, draft.id, "approved")
    return draft


@pytest_asyncio.fixture
async def approved_draft(mongo_db: Any) -> Any:  # noqa: ARG001 — forces Beanie init
    prospect = await _seed_prospect()
    draft = await _seed_draft("w1", prospect.id)
    return prospect, draft


async def _logs(workspace_id: str = "w1") -> list[Any]:
    from pocketpaw_ee.cloud.models.whatsapp_send_log import WhatsAppSendLog

    return await WhatsAppSendLog.find({"workspace": workspace_id}).to_list()


async def _draft_status(workspace_id: str, draft_id: str) -> str:
    drafts = await growth_service.list_drafts(_ctx(workspace_id))
    return next(d.status for d in drafts if d.id == draft_id)


# ---------------------------------------------------------------------------
# 1 — the happy path
# ---------------------------------------------------------------------------


class TestOptedInSend:
    async def test_sends_once_flips_to_sent_and_records(
        self, approved_draft: Any, fake_client: FakeMsg91Client
    ) -> None:
        _prospect, draft = approved_draft

        await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert fake_client.calls == [("919876543210", draft.body)]
        assert await _draft_status("w1", draft.id) == "sent"

        logs = await _logs()
        assert len(logs) == 1
        assert logs[0].status == "sent"
        assert logs[0].blocked_reason == ""
        assert logs[0].provider_message_id == "msg91-1"
        assert logs[0].opted_in_at_attempt is True
        assert logs[0].draft_id == draft.id

    async def test_worker_routes_whatsapp_into_the_real_branch(
        self, approved_draft: Any, fake_client: FakeMsg91Client
    ) -> None:
        """The arq job entrypoint, not just the module function."""
        from pocketpaw_ee.cloud.growth import worker

        _prospect, draft = approved_draft
        await worker.dispatch({}, draft.id, "whatsapp")

        assert len(fake_client.calls) == 1
        assert await _draft_status("w1", draft.id) == "sent"

    async def test_worker_leaves_other_channels_on_the_stub(
        self, mongo_db: Any, fake_client: FakeMsg91Client
    ) -> None:
        from pocketpaw_ee.cloud.growth import worker

        prospect = await _seed_prospect(domain="other-co.com")
        draft = await growth_service.create_draft(
            _ctx("w1"), prospect.id, CreateDraftRequest(channel="linkedin", body="hi")
        )
        await growth_service.gate_transition("w1", draft.id, "proposed")
        await growth_service.gate_transition("w1", draft.id, "approved")

        await worker.dispatch({}, draft.id, "linkedin")

        assert fake_client.calls == []
        assert await _draft_status("w1", draft.id) == "approved"


# ---------------------------------------------------------------------------
# 2 — THE critical one: no opt-in, no provider call
# ---------------------------------------------------------------------------


class TestOptInGuard:
    @pytest_asyncio.fixture
    async def not_opted_in(self, mongo_db: Any) -> Any:  # noqa: ARG002
        prospect = await _seed_prospect(opted_in=False)
        draft = await _seed_draft("w1", prospect.id)
        return prospect, draft

    async def test_provider_is_never_called(
        self, not_opted_in: Any, fake_client: FakeMsg91Client
    ) -> None:
        _prospect, draft = not_opted_in

        with pytest.raises(growth_whatsapp.OptInRequired):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        # The whole slice in one line: nothing left the building.
        assert fake_client.calls == []

    async def test_draft_stays_approved(
        self, not_opted_in: Any, fake_client: FakeMsg91Client
    ) -> None:
        _prospect, draft = not_opted_in

        with pytest.raises(growth_whatsapp.OptInRequired):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert await _draft_status("w1", draft.id) == "approved"

    async def test_writes_a_blocked_compliance_row(
        self, not_opted_in: Any, fake_client: FakeMsg91Client
    ) -> None:
        _prospect, draft = not_opted_in

        with pytest.raises(growth_whatsapp.OptInRequired):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        logs = await _logs()
        assert len(logs) == 1
        assert logs[0].status == "blocked"
        assert logs[0].blocked_reason == "not_opted_in"
        assert logs[0].opted_in_at_attempt is False
        assert logs[0].provider_message_id == ""

    async def test_error_carries_a_machine_readable_code(
        self, not_opted_in: Any, fake_client: FakeMsg91Client
    ) -> None:
        _prospect, draft = not_opted_in

        with pytest.raises(growth_whatsapp.OptInRequired) as exc_info:
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert exc_info.value.code == "growth.whatsapp_opt_in_required"

    async def test_guard_fires_before_credentials_are_resolved(
        self, not_opted_in: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not just "before the HTTP call" — before the provider stack at all.

        A guard that ran after credential resolution would still be correct on
        paper, but it would mean an unconfigured-vs-non-consenting prospect
        produce different failures. This pins the order.
        """
        _prospect, draft = not_opted_in
        exploding = ExplodingCredentials()
        monkeypatch.setattr(growth_whatsapp, "resolve_credentials", exploding)
        monkeypatch.setattr(
            growth_whatsapp,
            "_build_client",
            lambda creds: pytest.fail("a provider client was constructed for a refused send"),
        )

        with pytest.raises(growth_whatsapp.OptInRequired):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert exploding.calls == 0

    async def test_opted_in_but_no_number_is_a_data_error_not_a_send(
        self, mongo_db: Any, fake_client: FakeMsg91Client
    ) -> None:
        prospect = await _seed_prospect(opted_in=True, number=None)
        draft = await _seed_draft("w1", prospect.id)

        with pytest.raises(growth_whatsapp.ProspectUnavailable):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert fake_client.calls == []
        logs = await _logs()
        assert [entry.blocked_reason for entry in logs] == ["no_number"]


# ---------------------------------------------------------------------------
# 3 — the gate still owns ``approved``
# ---------------------------------------------------------------------------


class TestGateStillOwnsApproved:
    async def test_unapproved_draft_never_sends(
        self, mongo_db: Any, fake_client: FakeMsg91Client
    ) -> None:
        prospect = await _seed_prospect()
        draft = await _seed_draft("w1", prospect.id, approve=False)  # sits at ``proposed``

        with pytest.raises(growth_whatsapp.DraftNotApproved):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert fake_client.calls == []
        assert await _draft_status("w1", draft.id) == "proposed"
        assert [entry.blocked_reason for entry in await _logs()] == ["draft_not_approved"]

    async def test_missing_draft_is_a_non_event(
        self, mongo_db: Any, fake_client: FakeMsg91Client
    ) -> None:
        await growth_whatsapp.dispatch_whatsapp("000000000000000000000000")
        assert fake_client.calls == []
        assert await _logs() == []


# ---------------------------------------------------------------------------
# 4 — the hourly rate cap
# ---------------------------------------------------------------------------


class TestRateCap:
    async def test_default_is_twenty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GROWTH_WHATSAPP_MAX_PER_HOUR", raising=False)
        assert growth_whatsapp.max_per_hour() == 20

    async def test_garbage_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GROWTH_WHATSAPP_MAX_PER_HOUR", "not-a-number")
        assert growth_whatsapp.max_per_hour() == 20

    async def test_zero_refuses_everything_rather_than_meaning_unlimited(
        self, mongo_db: Any, fake_client: FakeMsg91Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fat-fingered cap must fail closed, not open the floodgates."""
        monkeypatch.setenv("GROWTH_WHATSAPP_MAX_PER_HOUR", "0")
        prospect = await _seed_prospect()
        draft = await _seed_draft("w1", prospect.id)

        with pytest.raises(growth_whatsapp.RateCapExceeded):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert fake_client.calls == []

    async def test_n_plus_one_is_refused_without_a_provider_call(
        self, mongo_db: Any, fake_client: FakeMsg91Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GROWTH_WHATSAPP_MAX_PER_HOUR", "1")
        prospect = await _seed_prospect()
        first = await _seed_draft("w1", prospect.id, body="first touch")
        second = await _seed_draft("w1", prospect.id, body="second touch")

        await growth_whatsapp.dispatch_whatsapp(first.id)
        assert len(fake_client.calls) == 1

        with pytest.raises(growth_whatsapp.RateCapExceeded):
            await growth_whatsapp.dispatch_whatsapp(second.id)

        # The N+1th send never reached the provider, and its draft is untouched.
        assert len(fake_client.calls) == 1
        assert await _draft_status("w1", second.id) == "approved"
        assert [entry.blocked_reason for entry in await _logs()] == ["", "rate_capped"]

    async def test_the_cap_is_per_workspace(
        self, mongo_db: Any, fake_client: FakeMsg91Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One tenant burning its allowance must not throttle another."""
        monkeypatch.setenv("GROWTH_WHATSAPP_MAX_PER_HOUR", "1")
        p1 = await _seed_prospect("w1")
        d1 = await _seed_draft("w1", p1.id)
        p2 = await _seed_prospect("w2")
        d2 = await _seed_draft("w2", p2.id)

        await growth_whatsapp.dispatch_whatsapp(d1.id)
        await growth_whatsapp.dispatch_whatsapp(d2.id)

        assert len(fake_client.calls) == 2
        assert await _draft_status("w2", d2.id) == "sent"

    async def test_refused_attempts_do_not_consume_the_window(
        self, mongo_db: Any, fake_client: FakeMsg91Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blocked attempt never reached Meta, so it must not spend budget."""
        monkeypatch.setenv("GROWTH_WHATSAPP_MAX_PER_HOUR", "1")
        blocked_prospect = await _seed_prospect(domain="no-consent.com", opted_in=False)
        blocked_draft = await _seed_draft("w1", blocked_prospect.id)
        with pytest.raises(growth_whatsapp.OptInRequired):
            await growth_whatsapp.dispatch_whatsapp(blocked_draft.id)

        good_prospect = await _seed_prospect(domain="consented.com")
        good_draft = await _seed_draft("w1", good_prospect.id)
        await growth_whatsapp.dispatch_whatsapp(good_draft.id)

        assert len(fake_client.calls) == 1
        assert await _draft_status("w1", good_draft.id) == "sent"


# ---------------------------------------------------------------------------
# 5 — provider + configuration failures
# ---------------------------------------------------------------------------


class TestFailurePaths:
    async def test_provider_failure_is_recorded_and_leaves_the_draft_approved(
        self, approved_draft: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prospect, draft = approved_draft
        client = FakeMsg91Client(
            _CREDS, raises=Msg91Error("msg91.http_error", "MSG91 returned 502")
        )

        async def _resolve(workspace_id: str) -> Msg91Credentials:
            return _CREDS

        monkeypatch.setattr(growth_whatsapp, "resolve_credentials", _resolve)
        monkeypatch.setattr(growth_whatsapp, "_build_client", lambda creds: client)

        with pytest.raises(growth_whatsapp.WhatsAppDispatchError):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        # The approval stands; only the delivery failed.
        assert await _draft_status("w1", draft.id) == "approved"
        logs = await _logs()
        assert len(logs) == 1
        assert logs[0].status == "failed"
        assert logs[0].error_code == "msg91.http_error"

    async def test_unconfigured_workspace_blocks_without_a_client(
        self, approved_draft: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prospect, draft = approved_draft

        async def _resolve(workspace_id: str) -> Msg91Credentials:
            raise Msg91NotConfigured("no msg91 connector")

        monkeypatch.setattr(growth_whatsapp, "resolve_credentials", _resolve)
        monkeypatch.setattr(
            growth_whatsapp,
            "_build_client",
            lambda creds: pytest.fail("a client was built without credentials"),
        )

        with pytest.raises(growth_whatsapp.WhatsAppNotConfigured):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert await _draft_status("w1", draft.id) == "approved"
        assert [entry.blocked_reason for entry in await _logs()] == ["not_configured"]


# ---------------------------------------------------------------------------
# 6 — the credential must not leak
# ---------------------------------------------------------------------------


class TestCredentialsNeverLeak:
    def test_repr_and_str_redact_the_authkey(self) -> None:
        assert SECRET_AUTHKEY not in repr(_CREDS)
        assert SECRET_AUTHKEY not in str(_CREDS)
        assert SECRET_AUTHKEY not in f"{_CREDS}"
        assert SECRET_AUTHKEY not in "{!r}".format(_CREDS)  # noqa: UP032 — exercising %r path
        assert "***redacted***" in repr(_CREDS)

    def test_client_repr_does_not_unwrap_its_credentials(self) -> None:
        from pocketpaw_ee.cloud.growth.msg91 import Msg91WhatsAppClient

        assert SECRET_AUTHKEY not in repr(Msg91WhatsAppClient(_CREDS))

    async def test_a_full_send_writes_no_credential_anywhere(
        self, approved_draft: Any, fake_client: FakeMsg91Client, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Grep-style: after a real send, the authkey is in no log line, no
        persisted document, and no DTO the routes can return."""
        _prospect, draft = approved_draft

        with caplog.at_level(logging.DEBUG):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert SECRET_AUTHKEY not in caplog.text

        logs = await _logs()
        assert SECRET_AUTHKEY not in str([entry.model_dump() for entry in logs])

        drafts = await growth_service.list_drafts(_ctx("w1"))
        prospects = await growth_service.list_prospects(_ctx("w1"))
        serialised = str([d.model_dump() for d in drafts] + [p.model_dump() for p in prospects])
        assert SECRET_AUTHKEY not in serialised

    async def test_a_refused_send_writes_no_credential_anywhere(
        self, mongo_db: Any, fake_client: FakeMsg91Client, caplog: pytest.LogCaptureFixture
    ) -> None:
        prospect = await _seed_prospect(opted_in=False)
        draft = await _seed_draft("w1", prospect.id)

        with caplog.at_level(logging.DEBUG), pytest.raises(growth_whatsapp.OptInRequired):
            await growth_whatsapp.dispatch_whatsapp(draft.id)

        assert SECRET_AUTHKEY not in caplog.text
        assert SECRET_AUTHKEY not in str([entry.model_dump() for entry in await _logs()])
