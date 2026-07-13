# tests/ee/agent/test_atlas_role_provider.py — the role-aware atlas
#   EntitlementProvider (feat/workspace-admin-tools, WA-3).
#
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-3).
#
# What this pins — the DISCOVERY-layer filter that hides admin capabilities a
# member can't use from atlas_search / atlas_describe (NOT the security gate;
# the RBAC checks inside the tools are the real lock). Driven through the REAL
# core overlay + the REAL packaged store so the provider is exercised end to end:
#   * MEMBER role → member-level admin ``capability`` cards are PRESENT; ADMIN
#     and OWNER cards are ABSENT (filtered). A non-role entry (a primitive) is
#     present regardless.
#   * ADMIN role → member + admin cards present, owner cards absent.
#   * OWNER role → every admin card present.
#   * Role UNRESOLVABLE (no identity on the stream / user not found / not a
#     member) → EVERY ``role:*`` card hidden (fail-closed), never a raise.
#   * ``connected_connector_names`` delegates to the wrapped default provider.
#   * The scope/identity-workspace mismatch fails closed.
#
# Identity is set via the same ``attach_agent_identity`` ContextVars the admin
# tools read; the ``User`` doc load (``_load_user``) is patched so nothing
# touches Mongo — the fake user carries a duck-typed ``.workspaces`` list that
# ``resolve_workspace_role`` reads (``.workspace`` / ``.role``). ``prime()`` is
# awaited exactly as the sdk_mcp_atlas handler awaits it before the sync grant
# filter.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.atlas_provider as ap  # noqa: E402
from pocketpaw_ee.agent.atlas_provider import RoleAwareEntitlementProvider  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)

from pocketpaw.atlas.model import AtlasEntry, AtlasModel  # noqa: E402
from pocketpaw.atlas.overlay import AtlasOverlay  # noqa: E402
from pocketpaw.atlas.store import AtlasStore  # noqa: E402

WS = "w1"
SCOPE = f"ws:{WS}"


# ── fakes ──────────────────────────────────────────────────────────────────


class _Membership:
    def __init__(self, workspace: str, role: str):
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Duck-typed User: a ``.workspaces`` list resolve_workspace_role reads."""

    def __init__(self, memberships: list[_Membership]):
        self.workspaces = memberships
        self.id = "u1"


class _identity:
    """Set the identity ContextVars the provider reads, then reset them."""

    def __init__(self, *, workspace=WS, user="u1"):
        self._ws, self._user = workspace, user
        self._tokens = None

    def __enter__(self):
        self._tokens = attach_agent_identity(workspace_id=self._ws, user_id=self._user)
        return self

    def __exit__(self, *exc):
        detach_agent_identity(self._tokens)
        return False


def _cap(entry_id: str, tier: str) -> AtlasEntry:
    return AtlasEntry(
        id=entry_id,
        kind="capability",
        name=entry_id.split(":", 1)[1],
        summary=f"{entry_id} summary",
        narrative=f"{entry_id} narrative",
        requires=[f"role:{tier}"],
        keywords=["manage", "workspace", "admin"],
    )


def _store() -> AtlasStore:
    """One primitive (role-blind) + one admin card per tier."""
    return AtlasStore(
        AtlasModel(
            entries=[
                AtlasEntry(
                    id="primitive:pocket",
                    kind="primitive",
                    name="Pocket",
                    summary="Pocket summary",
                    narrative="Pocket narrative",
                    keywords=["manage", "workspace"],
                ),
                _cap("capability:admin.members_list", "member"),
                _cap("capability:admin.member_update_role", "admin"),
                _cap("capability:admin.workspace_delete", "owner"),
            ]
        )
    )


@pytest.fixture
def _patch_user(monkeypatch):
    """Patch the provider's User load to return a fake user for the CURRENT
    identity's role — configured per test via the ``role`` closure var."""

    def _install(role: str | None, *, member_of=WS):
        async def _fake_load_user(user_id: str):  # noqa: ANN001, ARG001
            if role is None:
                return None
            return _FakeUser([_Membership(member_of, role)])

        monkeypatch.setattr(
            ap.RoleAwareEntitlementProvider, "_load_user", staticmethod(_fake_load_user)
        )

    return _install


async def _primed(role_provider: RoleAwareEntitlementProvider):
    await role_provider.prime()
    return role_provider


def _visible_admin_ids(store, provider) -> set[str]:
    return {
        e.id
        for e in (o.entry for o in AtlasOverlay.apply(store.entries, provider))
        if e.id.startswith("capability:admin.")
    }


# ── the three role tiers ────────────────────────────────────────────────────


class TestRoleTiersFilterAdminEntries:
    @pytest.mark.asyncio
    async def test_member_sees_only_member_admin_cards(self, _patch_user):
        _patch_user("member")
        store = _store()
        with _identity():
            provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            admin_ids = _visible_admin_ids(store, provider)
            # member-level read card present; admin + owner cards ABSENT.
            assert admin_ids == {"capability:admin.members_list"}
            # a non-admin describe (delete) reads exactly like an unknown id.
            assert (
                AtlasOverlay.describe(store, "capability:admin.workspace_delete", provider) is None
            )
            # the role-blind primitive is always present.
            assert AtlasOverlay.describe(store, "primitive:pocket", provider) is not None

    @pytest.mark.asyncio
    async def test_member_search_hides_admin_entries(self, _patch_user):
        _patch_user("member")
        store = _store()
        with _identity():
            provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            ids = [o.entry.id for o in AtlasOverlay.search(store, "manage the workspace", provider)]
            assert "capability:admin.member_update_role" not in ids
            assert "capability:admin.workspace_delete" not in ids

    @pytest.mark.asyncio
    async def test_admin_sees_member_and_admin_not_owner(self, _patch_user):
        _patch_user("admin")
        store = _store()
        with _identity():
            provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            assert _visible_admin_ids(store, provider) == {
                "capability:admin.members_list",
                "capability:admin.member_update_role",
            }

    @pytest.mark.asyncio
    async def test_owner_sees_all_admin_cards(self, _patch_user):
        _patch_user("owner")
        store = _store()
        with _identity():
            provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            assert _visible_admin_ids(store, provider) == {
                "capability:admin.members_list",
                "capability:admin.member_update_role",
                "capability:admin.workspace_delete",
            }


# ── fail-closed ──────────────────────────────────────────────────────────────


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_no_identity_hides_all_role_entries(self, _patch_user):
        # No _identity() context → the ContextVars are unset → role unresolved.
        _patch_user("owner")  # even though the user WOULD be owner, no identity → hidden
        store = _store()
        provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
        assert _visible_admin_ids(store, provider) == set()
        # the role-blind primitive is still visible.
        assert AtlasOverlay.describe(store, "primitive:pocket", provider) is not None

    @pytest.mark.asyncio
    async def test_user_not_found_hides_all_role_entries(self, _patch_user):
        _patch_user(None)  # _load_user returns None
        store = _store()
        with _identity():
            provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            assert _visible_admin_ids(store, provider) == set()

    @pytest.mark.asyncio
    async def test_not_a_member_hides_all_role_entries(self, _patch_user):
        # The user IS loaded but is a member of a DIFFERENT workspace → Forbidden
        # inside resolve_workspace_role → role unresolved → hidden.
        _patch_user("owner", member_of="other-workspace")
        store = _store()
        with _identity():
            provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            assert _visible_admin_ids(store, provider) == set()

    @pytest.mark.asyncio
    async def test_scope_identity_workspace_mismatch_hides(self, _patch_user):
        # Provider bound to ws:w1 but the stream identity is ws:other → refuse.
        _patch_user("owner")
        store = _store()
        with _identity(workspace="other"):
            provider = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            assert _visible_admin_ids(store, provider) == set()

    @pytest.mark.asyncio
    async def test_prime_never_raises_on_load_error(self, monkeypatch):
        async def _boom(user_id):  # noqa: ANN001, ARG001
            raise RuntimeError("db down")

        monkeypatch.setattr(ap.RoleAwareEntitlementProvider, "_load_user", staticmethod(_boom))
        store = _store()
        with _identity():
            provider = RoleAwareEntitlementProvider(scope_key=SCOPE)
            await provider.prime()  # must not raise
            assert _visible_admin_ids(store, provider) == set()

    @pytest.mark.asyncio
    async def test_is_granted_without_prime_hides_role_entries(self, _patch_user):
        # If prime() never runs, the role stays unresolved → role entries hidden.
        _patch_user("owner")
        store = _store()
        with _identity():
            provider = RoleAwareEntitlementProvider(scope_key=SCOPE)  # NOT primed
            assert _visible_admin_ids(store, provider) == set()


# ── construction + delegation ────────────────────────────────────────────────


# ── FINDING D — cross-user role bleed on a shared (warm-client) provider ─────
#
# The provider instance lives on the atlas MCP server, which is built once and
# carried on the WARM ClaudeSDKClient shared across users of a workspace/public
# pocket (its cache key omits user_id). ``prime()`` was idempotent (``_primed``),
# so it cached the FIRST caller's role forever: a MEMBER querying atlas after an
# OWNER saw the OWNER's admin/owner capability cards. The fix re-resolves the
# caller's role per turn from the CURRENT identity ContextVars. This is a
# DISCOVERY-layer leak (the RBAC gate inside each admin tool still re-checks the
# live role), but a real capability-disclosure leak worth closing.


class TestCrossUserRoleBleed:
    @pytest.fixture
    def _patch_user_by_id(self, monkeypatch):
        """Patch ``_load_user`` to map user_id → role, so priming as one user
        then switching identity to another reflects the SECOND user's role."""

        def _install(roles_by_user: dict[str, str]):
            async def _fake_load_user(user_id: str):  # noqa: ANN001
                role = roles_by_user.get(user_id)
                if role is None:
                    return None
                return _FakeUser([_Membership(WS, role)])

            monkeypatch.setattr(
                ap.RoleAwareEntitlementProvider, "_load_user", staticmethod(_fake_load_user)
            )

        return _install

    @pytest.mark.asyncio
    async def test_member_after_owner_does_not_see_admin_cards(self, _patch_user_by_id):
        """Prime as OWNER, then a MEMBER queries on the SAME provider instance
        (shared warm client). The member must NOT inherit the owner's cards.

        BEFORE the fix: ``prime()`` was a no-op on the second turn (``_primed``),
        so the cached OWNER role level leaked and every admin card stayed visible
        to the member. AFTER: the role is re-resolved per turn, so the member sees
        only member-level cards.
        """
        _patch_user_by_id({"owner_uid": "owner", "member_uid": "member"})
        store = _store()
        provider = RoleAwareEntitlementProvider(scope_key=SCOPE)

        # Turn 1 — OWNER primes the shared provider and sees every admin card.
        with _identity(user="owner_uid"):
            await provider.prime()
            assert _visible_admin_ids(store, provider) == {
                "capability:admin.members_list",
                "capability:admin.member_update_role",
                "capability:admin.workspace_delete",
            }

        # Turn 2 — a MEMBER on the SAME provider instance. Re-priming must reflect
        # the member's role, not the owner's cached one.
        with _identity(user="member_uid"):
            await provider.prime()
            assert _visible_admin_ids(store, provider) == {"capability:admin.members_list"}

    @pytest.mark.asyncio
    async def test_mid_session_role_change_is_reflected(self, _patch_user_by_id):
        """The P3 case — the SAME user's role changes mid-session (owner→member).
        A re-prime on the next turn reflects the downgrade instead of freezing the
        first-seen role."""
        roles = {"u1": "owner"}
        _patch_user_by_id(roles)
        store = _store()
        provider = RoleAwareEntitlementProvider(scope_key=SCOPE)

        with _identity(user="u1"):
            await provider.prime()
            assert "capability:admin.workspace_delete" in _visible_admin_ids(store, provider)

            # The user is demoted to member mid-session.
            roles["u1"] = "member"
            await provider.prime()
            assert _visible_admin_ids(store, provider) == {"capability:admin.members_list"}


class TestConstructionAndDelegation:
    def test_rejects_non_ws_scope(self):
        with pytest.raises(ValueError):
            RoleAwareEntitlementProvider(scope_key="default")

    def test_connected_connector_names_delegates(self, monkeypatch):
        provider = RoleAwareEntitlementProvider(scope_key=SCOPE)
        monkeypatch.setattr(provider._default, "connected_connector_names", lambda: {"stripe"})
        assert provider.connected_connector_names() == {"stripe"}

    def test_non_role_entries_grant_via_default(self):
        provider = RoleAwareEntitlementProvider(scope_key=SCOPE)
        primitive = AtlasEntry(
            id="primitive:pocket",
            kind="primitive",
            name="Pocket",
            summary="s",
            narrative="n",
        )
        # No prime needed — a role-blind entry delegates to the default (grant).
        assert provider.is_granted(primitive) is True
