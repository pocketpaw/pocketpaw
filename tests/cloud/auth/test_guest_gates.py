# tests/cloud/auth/test_guest_gates.py — the seams where guest limits fire:
# session-create, the check-only HTTP turn gate, and the uploads block.
#
# Created 2026-09-01 (feat/byok-guest-backend).
#
# Every refusal test here EXCEEDS its cap inside the test (cap 2 -> two real
# rows exist before the third ask) — a gate test that never crosses the line
# proves only that the happy path is happy, which is exactly how a
# switched-off gate ships green. The sessions-service test drives the REAL
# ``sessions_service.create`` so the lazy-import wiring is exercised, not just
# the helper.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import (
    GuestKeyRequired,
    GuestLimitError,
    GuestUploadForbidden,
)
from pocketpaw_ee.cloud.auth import guest_gates
from pocketpaw_ee.cloud.models.user import GuestLimits, User

pytestmark = pytest.mark.asyncio


async def _mk_guest(*, sessions=2, turns=40, workspace="w_g") -> User:
    doc = User(
        email=f"guest-{id(object())}@guest.invalid",
        hashed_password="x",
        is_active=True,
        is_guest=True,
        active_workspace=workspace,
        guest_limits=GuestLimits(sessions=sessions, turns_per_day=turns),
    )
    await doc.insert()
    return doc


async def _mk_session(owner: str, n: int) -> None:
    from pocketpaw_ee.cloud.models.session import Session

    for i in range(n):
        await Session(
            sessionId=f"sess-{owner}-{i}",
            context_type="session",
            workspace="w_g",
            owner=owner,
            title="t",
        ).insert()


async def _store_key(workspace: str) -> None:
    from pocketpaw_ee.cloud.models.byok_key import ByokProviderKey

    await ByokProviderKey(
        workspace=workspace,
        provider="anthropic",
        encrypted_key="gAAAA-fake-envelope",
        last4="zzzz",
        key_hint="sk-ant-api03",
    ).insert()


# ---------------------------------------------------------------------------
# Session cap
# ---------------------------------------------------------------------------


class TestSessionCap:
    async def test_a_guest_AT_the_cap_is_refused(self, mongo_db):
        guest = await _mk_guest(sessions=2)
        await _mk_session(str(guest.id), 2)  # the fixture reaches the cap
        with pytest.raises(GuestLimitError) as exc:
            await guest_gates.assert_guest_can_create_session(str(guest.id))
        assert exc.value.kind == "sessions"

    async def test_a_guest_under_the_cap_passes(self, mongo_db):
        guest = await _mk_guest(sessions=2)
        await _mk_session(str(guest.id), 1)
        await guest_gates.assert_guest_can_create_session(str(guest.id))

    async def test_another_users_sessions_do_not_count(self, mongo_db):
        """The count must be scoped to the guest — otherwise every guest is
        refused as soon as anyone else has sessions."""
        guest = await _mk_guest(sessions=2)
        await _mk_session("someone_else", 5)
        await guest_gates.assert_guest_can_create_session(str(guest.id))

    async def test_a_non_guest_is_never_capped(self, mongo_db):
        doc = User(email="real@x.co", hashed_password="x", is_active=True)
        await doc.insert()
        await _mk_session(str(doc.id), 10)
        await guest_gates.assert_guest_can_create_session(str(doc.id))

    async def test_the_REAL_sessions_service_create_refuses_at_the_cap(self, mongo_db):
        """Drive sessions_service.create itself — proves the gate is WIRED,
        not merely that the helper works when called."""
        from pocketpaw_ee.cloud.sessions import service as sessions_service
        from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest

        guest = await _mk_guest(sessions=2)
        ctx = sessions_service.legacy_ctx(str(guest.id), "w_g")
        await sessions_service.create(ctx, "w_g", CreateSessionRequest(title="one"))
        await sessions_service.create(ctx, "w_g", CreateSessionRequest(title="two"))
        with pytest.raises(GuestLimitError) as exc:
            await sessions_service.create(ctx, "w_g", CreateSessionRequest(title="three"))
        assert exc.value.kind == "sessions"

    async def test_relinking_an_EXISTING_session_is_not_capped(self, mongo_db):
        """The upsert branch mints nothing, so it must stay uncapped — a guest
        at their cap re-opening a session they already own is not a new spend."""
        from pocketpaw_ee.cloud.sessions import service as sessions_service
        from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest

        guest = await _mk_guest(sessions=1)
        ctx = sessions_service.legacy_ctx(str(guest.id), "w_g")
        made = await sessions_service.create(ctx, "w_g", CreateSessionRequest(title="one"))
        again = await sessions_service.create(
            ctx, "w_g", CreateSessionRequest(session_id=made.sessionId, title="one")
        )
        assert again.sessionId == made.sessionId


# ---------------------------------------------------------------------------
# Check-only turn gate (the HTTP chokepoint)
# ---------------------------------------------------------------------------


class TestTurnGateCheckOnly:
    async def test_a_keyless_guest_is_refused_with_key_required(self, mongo_db):
        guest = await _mk_guest()
        with pytest.raises(GuestKeyRequired):
            await guest_gates.assert_guest_turn_allowed(str(guest.id), "w_g")

    async def test_a_guest_at_the_turn_cap_is_refused(self, mongo_db):
        from pocketpaw_ee.cloud.auth import guest_budget

        guest = await _mk_guest(turns=2)
        await _store_key("w_g")
        # EXCEED the line: two real spends land the guest at cap 2.
        assert (await guest_budget.try_spend_turn(str(guest.id), 2))[0] is True
        assert (await guest_budget.try_spend_turn(str(guest.id), 2))[0] is True
        with pytest.raises(GuestLimitError) as exc:
            await guest_gates.assert_guest_turn_allowed(str(guest.id), "w_g")
        assert exc.value.kind == "turns"

    async def test_the_check_does_NOT_increment(self, mongo_db):
        """The executor owns the single spend; a checking route must not make
        every turn cost two."""
        from pocketpaw_ee.cloud.auth import guest_budget

        guest = await _mk_guest(turns=5)
        await _store_key("w_g")
        await guest_gates.assert_guest_turn_allowed(str(guest.id), "w_g")
        await guest_gates.assert_guest_turn_allowed(str(guest.id), "w_g")
        assert await guest_budget.turns_used_today(str(guest.id)) == 0

    async def test_a_guest_under_cap_with_a_key_passes(self, mongo_db):
        guest = await _mk_guest(turns=5)
        await _store_key("w_g")
        await guest_gates.assert_guest_turn_allowed(str(guest.id), "w_g")

    async def test_a_non_guest_passes_with_no_key_and_no_counter(self, mongo_db):
        doc = User(email="real3@x.co", hashed_password="x", is_active=True)
        await doc.insert()
        await guest_gates.assert_guest_turn_allowed(str(doc.id), "w_any")


# ---------------------------------------------------------------------------
# Uploads block — drive the REAL route function; the gate fires before any
# file processing, so no UploadFile fixtures are needed.
# ---------------------------------------------------------------------------


class TestUploadsBlocked:
    async def test_a_guest_upload_is_403_guest_upload_forbidden(self, mongo_db):
        from pocketpaw_ee.cloud.uploads.router import upload

        guest = await _mk_guest()
        with pytest.raises(GuestUploadForbidden):
            await upload(
                files=[],
                chat_id=None,
                path=None,
                pocket_id=None,
                workspace="w_g",
                user_id=str(guest.id),
            )

    async def test_a_non_guest_upload_is_not_blocked_by_this_gate(self, mongo_db):
        """files=[] sails past the guest gate and returns an empty result —
        proving the gate keys on is_guest, not on everyone."""
        from pocketpaw_ee.cloud.uploads.router import upload

        from fastapi import HTTPException

        doc = User(email="real4@x.co", hashed_password="x", is_active=True)
        await doc.insert()
        # An empty batch trips the DOWNSTREAM "empty upload batch" 400 — which
        # is the proof: a non-guest sails PAST the guest gate into the real
        # upload path (a guest never reaches that error).
        with pytest.raises(HTTPException) as exc:
            await upload(
                files=[],
                chat_id=None,
                path=None,
                pocket_id=None,
                workspace="w_g",
                user_id=str(doc.id),
            )
        assert exc.value.status_code == 400
        assert "empty upload batch" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# The HTTP chokepoint — a guest over the cap gets a clean PRE-STREAM 402 whose
# JSON body carries the frozen top-level code/kind the signup prompt keys on.
# Real gate, real budget rows; only scope resolution and auth deps are faked.
# ---------------------------------------------------------------------------


class TestChatRouteFastReject:
    async def _post(self, user_id: str, monkeypatch):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from pocketpaw_ee.cloud._core.http import add_error_handler
        from pocketpaw_ee.cloud.chat import agent_router as mod
        from pocketpaw_ee.cloud.license import require_license
        from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

        async def _fake_resolve(**_):
            from types import SimpleNamespace

            return SimpleNamespace(
                kind=SimpleNamespace(value="session"),
                scope_id="s1",
                workspace_id="w_g",
                user_id=user_id,
                target_agent_id="a1",
                members=[user_id],
                session_id=None,
                intent=None,
                surface_context=None,
                resolved_profile=None,
                pocket_id=None,
                model_override=None,
            )

        monkeypatch.setattr(mod, "resolve_scope_context", _fake_resolve)

        app = FastAPI()
        add_error_handler(app)
        app.include_router(mod.router)
        app.dependency_overrides[current_user_id] = lambda: user_id
        app.dependency_overrides[current_workspace_id] = lambda: "w_g"
        app.dependency_overrides[require_license] = lambda: None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/cloud/chat/session/s1/agent", json={"content": "x"})

    async def test_a_guest_at_the_cap_gets_the_frozen_402_body(self, mongo_db, monkeypatch):
        from pocketpaw_ee.cloud.auth import guest_budget

        guest = await _mk_guest(turns=2)
        await _store_key("w_g")
        uid = str(guest.id)
        # EXCEED the line before asking.
        assert (await guest_budget.try_spend_turn(uid, 2))[0] is True
        assert (await guest_budget.try_spend_turn(uid, 2))[0] is True

        resp = await self._post(uid, monkeypatch)

        assert resp.status_code == 402
        body = resp.json()
        assert body["code"] == "guest_limit_reached"
        assert body["kind"] == "turns"
        # The check must not have SPENT anything — the executor owns the spend.
        assert await guest_budget.turns_used_today(uid) == 2

    async def test_a_keyless_guest_gets_guest_key_required(self, mongo_db, monkeypatch):
        guest = await _mk_guest()  # no stored key
        resp = await self._post(str(guest.id), monkeypatch)
        assert resp.status_code == 402
        assert resp.json()["code"] == "guest_key_required"
