# tests/cloud/auth/test_guest.py — BYOK-first guest accounts: mint, budget,
# upgrade, and the never-log-the-key rule.
#
# Created 2026-09-01 (feat/byok-guest-backend).
#
# What is worth testing here, in order of what it costs us to get wrong:
#
#   1. The key never leaks — not into a log record, not into the user row,
#      not into any stored plaintext. (Redaction sweep + storage assertions.)
#   2. The turn budget fails CLOSED and the refusal fixture genuinely EXCEEDS
#      the cap (the workspace's signature bug is a gate that reads as
#      switched-off; a test that never crosses the cap proves nothing).
#   3. A dead key mints NOTHING — validation happens before any row exists.
#   4. Upgrade keeps the SAME user id (the whole reason guests are minted
#      server-side).
#   5. The wire contract (top-level code/kind) is pinned byte-for-byte — the
#      sibling frontend builds against it.

from __future__ import annotations

import logging

import pytest
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    GuestKeyRequired,
    GuestLimitError,
    GuestUploadForbidden,
    ValidationError,
)
from pocketpaw_ee.cloud.auth import guest as guest_service
from pocketpaw_ee.cloud.auth import guest_budget
from pocketpaw_ee.cloud.models.user import GuestLimits, User

pytestmark = pytest.mark.asyncio

_KEY = "sk-ant-api03-" + "sekrit" * 8


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())


async def _mk_guest(**over) -> User:
    doc = User(
        email=f"guest-test-{id(over)}@guest.invalid",
        hashed_password="x",
        is_active=True,
        is_guest=True,
        guest_limits=GuestLimits(),
        **over,
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# The wire contract — frozen with the BYOK-fe sibling. Change these bodies and
# the signup prompt stops firing; that is why the shape is pinned literally.
# ---------------------------------------------------------------------------


class TestWireContract:
    def test_limit_error_carries_top_level_code_and_kind(self):
        d = GuestLimitError("turns").to_dict()
        assert d["code"] == "guest_limit_reached"
        assert d["kind"] == "turns"
        assert d["error"]["code"] == "guest_limit_reached"
        assert GuestLimitError("turns").status_code == 402

    def test_sessions_kind(self):
        d = GuestLimitError("sessions").to_dict()
        assert d["kind"] == "sessions"

    def test_upload_forbidden_is_403_with_top_level_code(self):
        e = GuestUploadForbidden()
        assert e.status_code == 403
        assert e.to_dict()["code"] == "guest_upload_forbidden"

    def test_key_required_is_402_with_top_level_code(self):
        e = GuestKeyRequired()
        assert e.status_code == 402
        assert e.to_dict()["code"] == "guest_key_required"

    def test_guest_routes_are_registered_before_the_fastapi_users_subrouters(self):
        """Route ORDER is the override mechanism (FastAPI matches first
        registered). If /auth/guest ever slips below the sub-routers a stock
        route could shadow it silently."""
        from pocketpaw_ee.cloud.auth.router import router

        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/auth/guest" in paths
        assert "/auth/guest/upgrade" in paths
        assert paths.index("/auth/guest") < paths.index("/auth/register")

    def test_profile_out_carries_is_guest(self):
        from pocketpaw_ee.cloud.auth.domain import AuthUser
        from pocketpaw_ee.cloud.auth.dto import auth_user_to_profile_out

        u = AuthUser(
            id="1",
            email="g@guest.invalid",
            full_name="Guest",
            avatar="",
            status="online",
            active_workspace="w1",
            workspaces=(),
            is_verified=False,
            is_superuser=False,
            is_guest=True,
        )
        assert auth_user_to_profile_out(u).is_guest is True


# ---------------------------------------------------------------------------
# The daily turn budget — fail-closed, atomic, per-user.
# ---------------------------------------------------------------------------


class TestTurnBudget:
    async def test_the_cap_refuses_the_claim_that_EXCEEDS_it(self, mongo_db):
        """Cap 2, three claims: the fixture crosses the cap, so this test
        exercises the refusal branch — not just the happy path."""
        first = await guest_budget.try_spend_turn("u_g1", 2)
        second = await guest_budget.try_spend_turn("u_g1", 2)
        third = await guest_budget.try_spend_turn("u_g1", 2)
        assert first[0] is True and second[0] is True
        assert third[0] is False, "the third claim on a cap of 2 must be refused"
        assert third[1:] == (2, 2)

    async def test_a_refused_claim_is_rolled_back(self, mongo_db):
        await guest_budget.try_spend_turn("u_g2", 1)
        for _ in range(4):
            await guest_budget.try_spend_turn("u_g2", 1)
        assert await guest_budget.turns_used_today("u_g2") == 1

    async def test_one_guest_cannot_spend_anothers_budget(self, mongo_db):
        await guest_budget.try_spend_turn("u_g3", 1)
        assert (await guest_budget.try_spend_turn("u_g4", 1))[0] is True

    async def test_an_unreadable_counter_fails_CLOSED(self, monkeypatch):
        """A degraded database must not become an unmetered free tier: break
        the collection accessor (as a driver outage or the beanie-1.x accessor
        bug would) and the claim must be refused."""
        from pocketpaw_ee.cloud.models.guest_turn_usage import GuestTurnUsage

        def _broken(*a, **k):
            raise RuntimeError("collection unavailable")

        monkeypatch.setattr(GuestTurnUsage, "get_pymongo_collection", _broken)
        allowed, _spent, _cap = await guest_budget.try_spend_turn("u_g5", 5)
        assert allowed is False

    async def test_a_zero_cap_refuses(self, mongo_db):
        assert (await guest_budget.try_spend_turn("u_g6", 0))[0] is False


# ---------------------------------------------------------------------------
# load_guest — the one read every gate branches on.
# ---------------------------------------------------------------------------


class TestLoadGuest:
    async def test_a_guest_row_loads(self, mongo_db):
        doc = await _mk_guest()
        got = await guest_budget.load_guest(str(doc.id))
        assert got is not None and got.is_guest is True

    async def test_a_non_guest_is_none(self, mongo_db):
        doc = User(email="real@x.co", hashed_password="x", is_active=True)
        await doc.insert()
        assert await guest_budget.load_guest(str(doc.id)) is None

    async def test_a_non_objectid_is_none(self, mongo_db):
        assert await guest_budget.load_guest("u1") is None

    async def test_empty_is_none(self, mongo_db):
        assert await guest_budget.load_guest(None) is None


# ---------------------------------------------------------------------------
# mint_guest — validation BEFORE existence; nothing on a dead key.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _resolver_stub(monkeypatch):
    """workspace_service.create() invalidates the realtime AudienceResolver,
    which only exists after init_realtime — stub it like the workspace-service
    tests do."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: MagicMock()
    )


class TestMintGuest:
    async def test_an_unsupported_provider_is_rejected_before_any_provider_call(
        self, mongo_db, monkeypatch
    ):
        async def _must_not_run(api_key):
            raise AssertionError("validate_key must not be called for an unsupported provider")

        monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.validate_key", _must_not_run)
        with pytest.raises(ValidationError) as exc:
            await guest_service.mint_guest(_KEY, provider="openai")
        assert exc.value.code == "byok.provider_unsupported"

    async def test_a_dead_key_mints_NOTHING(self, mongo_db, monkeypatch):
        async def _dead(api_key):
            raise ValidationError("byok.key_rejected", "Anthropic rejected that key.")

        monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.validate_key", _dead)
        with pytest.raises(ValidationError) as exc:
            await guest_service.mint_guest(_KEY)
        assert exc.value.code == "byok.key_rejected"
        assert await User.find_all().count() == 0, "a dead key must not mint a user row"

    async def test_a_good_key_mints_user_workspace_and_encrypted_key(
        self, mongo_db, monkeypatch, _resolver_stub
    ):
        calls: list[str] = []

        async def _ok(api_key):
            calls.append("validated")

        monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.validate_key", _ok)

        user = await guest_service.mint_guest(_KEY)

        assert user.is_guest is True
        assert user.guest_limits is not None
        assert user.guest_limits.sessions == 2
        assert user.guest_limits.turns_per_day == 40
        # /auth/me must NOT route the guest into the workspace funnel.
        assert user.active_workspace, "active_workspace must be set at mint"
        assert user.workspaces and user.workspaces[0].role == "owner"
        # Validated exactly ONCE — set_key(validate=False) must not re-spend
        # a provider round trip per mint.
        assert calls == ["validated"]

        from pocketpaw_ee.cloud.models.byok_key import ByokProviderKey

        row = await ByokProviderKey.find_one(ByokProviderKey.workspace == user.active_workspace)
        assert row is not None
        assert row.encrypted_key and _KEY not in row.encrypted_key
        assert row.last4 == _KEY[-4:]

        # The key appears NOWHERE in the user row.
        assert _KEY not in user.model_dump_json()

    async def test_the_key_never_reaches_a_log_record(
        self, mongo_db, monkeypatch, caplog, _resolver_stub
    ):
        """The redaction assertion at the seam: run the WHOLE mint at DEBUG
        capture and sweep every record. Break any logger call into including
        the key and this goes red."""

        async def _ok(api_key):
            return None

        monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.validate_key", _ok)
        with caplog.at_level(logging.DEBUG):
            await guest_service.mint_guest(_KEY)
        assert _KEY not in caplog.text
        for rec in caplog.records:
            assert _KEY not in str(rec.args or "")


# ---------------------------------------------------------------------------
# upgrade_guest — same user id, everything kept.
# ---------------------------------------------------------------------------


class TestUpgradeGuest:
    async def test_upgrade_keeps_the_user_id_and_workspace(self, mongo_db):
        doc = await _mk_guest(active_workspace="w_keep")
        before_id = doc.id

        got = await guest_service.upgrade_guest(doc, email="Real@Example.com", password="hunter22")

        assert got.id == before_id
        assert got.is_guest is False
        assert got.guest_limits is None
        assert got.email == "real@example.com"
        assert got.active_workspace == "w_keep"
        fresh = await User.get(before_id)
        assert fresh is not None and fresh.is_guest is False

    async def test_a_taken_email_conflicts(self, mongo_db):
        await User(email="taken@x.co", hashed_password="x", is_active=True).insert()
        doc = await _mk_guest()
        with pytest.raises(CloudError) as exc:
            await guest_service.upgrade_guest(doc, email="taken@x.co", password="hunter22")
        assert exc.value.code == "auth.email_taken"
        assert exc.value.status_code == 409

    async def test_a_non_guest_cannot_upgrade(self, mongo_db):
        doc = User(email="real2@x.co", hashed_password="x", is_active=True)
        await doc.insert()
        with pytest.raises(CloudError) as exc:
            await guest_service.upgrade_guest(doc, email="new@x.co", password="hunter22")
        assert exc.value.code == "auth.not_a_guest"

    async def test_a_short_password_is_rejected(self, mongo_db):
        doc = await _mk_guest()
        with pytest.raises(ValidationError):
            await guest_service.upgrade_guest(doc, email="ok@x.co", password="short")
