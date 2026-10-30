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
# Updated: 2026-08-17 (feat/ast-3-atlas-flag-aware, AST-3) — ``TestFlagAwareParity``:
# the role-aware provider produces the SAME ``available`` / ``mode`` for the two
# rollout-flagged primitives as the OSS default (the stamp lives in the shared
# core overlay, so parity is structural, proven here rather than duplicated);
# ``is_granted`` for those primitives is unaffected by the flags (discovery
# hints only); the ``capability:fabric.*`` cards stay ``role:member``-gated
# regardless of mode.
# Updated: 2026-08-17 (AST-5a — review fix V9) — the two ``capability:fabric.*``
# cards a primed MEMBER sees now inherit ``primitive:source-truth``'s mode
# through the same overlay stamp: mode off → both ``available False`` /
# ``mode "off"`` with the source-truth ``enable_hint`` on describe; shadow /
# enforce → available with the mode; and the exact review repro (member, mode
# off, "where did this value come from") never returns an UN-MARKED fabric card
# above the demoted primitive. The role gate itself is unchanged.

from __future__ import annotations

import json

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.atlas_provider as ap  # noqa: E402
from pocketpaw_ee.agent.atlas_provider import RoleAwareEntitlementProvider  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)

from pocketpaw.agents.sdk_mcp_atlas import (  # noqa: E402
    _atlas_describe_handler,
    _atlas_search_handler,
)
from pocketpaw.atlas.model import AtlasEntry, AtlasModel  # noqa: E402
from pocketpaw.atlas.overlay import (  # noqa: E402
    FLAG_ENABLE_HINTS,
    FLAGGED_CAPABILITY_MODES,
    FLAGGED_PRIMITIVE_IDS,
    AtlasOverlay,
    DefaultEntitlementProvider,
)
from pocketpaw.atlas.store import AtlasStore, get_atlas_store  # noqa: E402

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


# ── AST-3: flag-aware availability parity with the OSS default ──────────────


class TestFlagAwareParity:
    """The rollout-flagged primitives' ``available`` / ``mode`` are stamped in the
    shared core overlay (``_overlay_one``), so the role-aware provider gets them
    identically to the OSS default — no EE duplication. Pinned per mode."""

    @pytest.fixture
    def _flags(self, monkeypatch):
        from pocketpaw.config import get_settings

        def _set(*, fabric="off", deep_work="off", cloud_plan="off"):
            settings = get_settings()
            monkeypatch.setattr(settings, "fabric_source_truth_mode", fabric)
            monkeypatch.setattr(settings, "deep_work_verify_mode", deep_work)
            monkeypatch.setattr(settings, "cloud_plan_verify_mode", cloud_plan)
            monkeypatch.setattr(settings, "deep_work_verify_loop_enabled", False)
            monkeypatch.setattr(settings, "cloud_plan_verify_loop_enabled", False)

        _set()
        return _set

    @staticmethod
    def _flagged(provider) -> dict[str, tuple[bool | None, str | None]]:
        store = get_atlas_store()
        entries = [store.describe(i) for i in FLAGGED_PRIMITIVE_IDS]
        return {o.entry.id: (o.available, o.mode) for o in AtlasOverlay.apply(entries, provider)}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fabric", "deep_work", "cloud_plan"),
        [("off", "off", "off"), ("shadow", "shadow", "off"), ("enforce", "off", "enforce")],
    )
    async def test_same_available_and_mode_as_default(
        self, _flags, _patch_user, fabric, deep_work, cloud_plan
    ):
        _flags(fabric=fabric, deep_work=deep_work, cloud_plan=cloud_plan)
        _patch_user("member")
        with _identity():
            role_aware = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            via_role = self._flagged(role_aware)
        via_default = self._flagged(DefaultEntitlementProvider(scope_key=SCOPE))
        assert via_role == via_default
        assert via_role["primitive:source-truth"] == (fabric != "off", fabric)
        expected_verify = "enforce" if "enforce" in (deep_work, cloud_plan) else deep_work
        assert via_role["primitive:verify-loop"] == (expected_verify != "off", expected_verify)

    @pytest.mark.asyncio
    async def test_flags_never_touch_is_granted(self, _flags, _patch_user):
        """Discovery hints only: the flagged primitives are granted at EVERY
        mode, and the role:member fabric capability cards keep their role gate
        (granted to a member, hidden when the role is unresolved) at every mode."""
        store = get_atlas_store()
        flagged = [store.describe(i) for i in FLAGGED_PRIMITIVE_IDS]
        fabric_caps = [e for e in store.entries if e.id.startswith("capability:fabric.")]
        assert fabric_caps and all("role:member" in e.requires for e in fabric_caps)
        for mode in ("off", "shadow", "enforce"):
            _flags(fabric=mode, deep_work=mode)
            _patch_user("member")
            with _identity():
                member = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
                assert all(member.is_granted(e) is True for e in flagged), mode
                assert all(member.is_granted(e) is True for e in fabric_caps), mode
            unresolved = RoleAwareEntitlementProvider(scope_key=SCOPE)  # never primed
            assert all(unresolved.is_granted(e) is True for e in flagged), mode
            assert all(unresolved.is_granted(e) is False for e in fabric_caps), mode

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fabric", ["off", "shadow", "enforce"])
    async def test_member_fabric_cards_inherit_source_truth_mode(self, _flags, _patch_user, fabric):
        """AST-5a (V9): the two member-level capability:fabric.* cards carry
        the SAME mode / available as primitive:source-truth for a primed member;
        off → describe renders the source-truth enable_hint on the card too."""
        _flags(fabric=fabric)
        _patch_user("member")
        store = get_atlas_store()
        cards = [store.describe(i) for i in FLAGGED_CAPABILITY_MODES]
        assert all(c is not None for c in cards)
        with _identity():
            member = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            overlaid = {o.entry.id: o for o in AtlasOverlay.apply(cards, member)}
            assert set(overlaid) == set(FLAGGED_CAPABILITY_MODES), "member sees both cards"
            for card_id, o in overlaid.items():
                assert (o.available, o.mode) == (fabric != "off", fabric), card_id
            for card_id in FLAGGED_CAPABILITY_MODES:
                out = await _atlas_describe_handler({"id": card_id}, member)
                assert not out.get("is_error")
                payload = json.loads(out["content"][0]["text"])
                assert payload["mode"] == fabric
                if fabric == "off":
                    assert payload["available"] is False
                    assert payload["enable_hint"] == FLAG_ENABLE_HINTS["primitive:source-truth"]
                else:
                    assert payload["available"] is True
                    assert "enable_hint" not in payload

    @pytest.mark.asyncio
    async def test_review_repro_member_search_off_marks_every_fabric_card(
        self, _flags, _patch_user
    ):
        """The exact V9 repro: member provider, source-truth off,
        atlas_search "where did this value come from". Before: #1 was
        capability:fabric.provenance_read (available None, mode None) above #2
        primitive:source-truth (available False, mode off). Now no fabric card
        in the answer is un-marked, so the agent can't read it as live."""
        _flags(fabric="off")
        _patch_user("member")
        with _identity():
            member = await _primed(RoleAwareEntitlementProvider(scope_key=SCOPE))
            out = await _atlas_search_handler({"intent": "where did this value come from"}, member)
        cards = json.loads(out["content"][0]["text"])["results"]
        ids = [c["id"] for c in cards]
        assert "primitive:source-truth" in ids and "capability:fabric.provenance_read" in ids, ids
        fabric_cards = [c for c in cards if c["id"] in FLAGGED_CAPABILITY_MODES]
        assert fabric_cards
        for card in fabric_cards:
            assert card["available"] is False and card["mode"] == "off", card
        primitive_at = ids.index("primitive:source-truth")
        assert not [
            c["id"]
            for c in cards[:primitive_at]
            if c["id"].startswith("capability:fabric.") and "mode" not in c
        ]
