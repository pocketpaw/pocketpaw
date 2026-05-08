# tests/scenarios/test_s2_rbac_tour.py — Phase 4 / Scenario 2.
# Created: 2026-05-08 — Org-owner + invited-member RBAC tour.
#
# Outcome under test (per docs/playbooks/grand-smoke.md §S2):
#
#   Owner invites a fresh member → member accepts → member can perform
#   member-tier actions but is BLOCKED on admin-tier actions → owner
#   promotes member to admin → previously blocked action now succeeds.
#
# This is the smallest end-to-end proof that the workspace-RBAC stack
# (#1058–#1061) and the cloud guards added in #1059 actually enforce
# role boundaries through HTTP — not just at the dep level.
#
# What we DON'T cover here (out of scope, separate scenarios):
#   - Plan-tier gates (Fabric / Instinct require_plan_feature) — covered
#     by paw-enterprise contract suite (#171).
#   - Pocket-level visibility (owner / shared / workspace) — covered by
#     S4 in the playbook roster.
#
# Cleanup is best-effort: we revoke invites and demote the test member
# back to 'member' if anything still references them. The fresh member
# user account itself is left in Mongo — fastapi-users doesn't expose a
# self-delete on the runtime API and adding admin teardown for it would
# require master access. Use unique-per-run emails so cruft doesn't
# block reruns.

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers — bearer login, register, header builders
# ---------------------------------------------------------------------------


def _bearer_login(api_url: str, email: str, password: str) -> str:
    """Exchange email + password for a fastapi-users bearer JWT."""
    resp = httpx.post(
        f"{api_url}/api/v1/auth/bearer/login",
        data={"username": email, "password": password},
        headers={"content-type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _register_user(api_url: str, email: str, password: str) -> dict[str, Any]:
    """Create a fresh user via the public fastapi-users register route."""
    resp = httpx.post(
        f"{api_url}/api/v1/auth/register",
        json={"email": email, "password": password},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _auth_headers(token: str, workspace_id: str | None = None) -> dict[str, str]:
    """Bearer + optional workspace-scope header."""
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id:
        headers["X-Workspace-Id"] = workspace_id
    return headers


def _get_me(api_url: str, token: str) -> dict[str, Any]:
    resp = httpx.get(
        f"{api_url}/api/v1/auth/me",
        headers=_auth_headers(token),
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _owner_workspace_id(me: dict[str, Any]) -> str:
    """Return the first workspace where this user is owner."""
    for ws in me.get("workspaces", []):
        if ws.get("role") == "owner":
            return ws["workspace"]
    raise AssertionError(
        f"admin seed must own at least one workspace; got {me.get('workspaces')!r}"
    )


# ---------------------------------------------------------------------------
# Per-test fixtures — owner session, fresh member session
# ---------------------------------------------------------------------------


@pytest.fixture
def owner_session(api_url: str, admin_credentials: tuple[str, str]) -> dict[str, Any]:
    """Log in as the seeded admin and snapshot owner identity + workspace."""
    email, password = admin_credentials
    token = _bearer_login(api_url, email, password)
    me = _get_me(api_url, token)
    return {
        "token": token,
        "user_id": me["id"],
        "email": me["email"],
        "workspace_id": _owner_workspace_id(me),
    }


@pytest.fixture
def member_email() -> str:
    """Unique-per-run email so reruns don't collide on the prior member account."""
    return f"s2-member-{int(time.time())}-{uuid.uuid4().hex[:6]}@example.com"


@pytest.fixture
def member_password() -> str:
    """Stable password — the test only needs *a* valid password to drive login."""
    return "ScenarioTest1!"


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


def _revoke_all_invites_for_email(
    api_url: str, owner: dict[str, Any], email: str
) -> None:
    """Owner revokes any outstanding invites that target ``email``."""
    workspace_id = owner["workspace_id"]
    headers = _auth_headers(owner["token"], workspace_id)
    listing = httpx.get(
        f"{api_url}/api/v1/workspaces/{workspace_id}/invites",
        headers=headers,
        timeout=10.0,
    )
    if listing.status_code != 200:
        return
    for invite in listing.json():
        if invite.get("email") == email and not invite.get("revoked"):
            httpx.delete(
                f"{api_url}/api/v1/workspaces/{workspace_id}/invites/{invite['_id']}",
                headers=headers,
                timeout=10.0,
            )


def _demote_member(
    api_url: str, owner: dict[str, Any], member_user_id: str
) -> None:
    """Best-effort demote-to-member so a leaked admin doesn't poison reruns."""
    workspace_id = owner["workspace_id"]
    httpx.patch(
        f"{api_url}/api/v1/workspaces/{workspace_id}/members/{member_user_id}",
        headers=_auth_headers(owner["token"], workspace_id),
        json={"role": "member"},
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# Scenario — single test, staged with explicit assertions per phase.
# ---------------------------------------------------------------------------


def test_owner_invites_member_then_promotes(
    api_url: str,
    owner_session: dict[str, Any],
    member_email: str,
    member_password: str,
) -> Iterator[None]:
    """End-to-end: invite → register → accept → 403 on admin op → promote → 200."""
    workspace_id = owner_session["workspace_id"]
    owner_token = owner_session["token"]

    # ----- Phase 1: owner creates an invite for the new member email -------
    invite_resp = httpx.post(
        f"{api_url}/api/v1/workspaces/{workspace_id}/invites",
        headers=_auth_headers(owner_token, workspace_id),
        json={"email": member_email, "role": "member"},
        timeout=10.0,
    )
    assert invite_resp.status_code == 200, (
        f"owner.create_invite failed: {invite_resp.status_code} {invite_resp.text}"
    )
    invite = invite_resp.json()
    assert invite["email"] == member_email
    assert invite["role"] == "member"
    assert invite["accepted"] is False
    assert invite["revoked"] is False
    invite_token = invite["token"]
    # InviteOut serializes the Mongo id as ``_id`` (alias on the
    # Pydantic field), not ``id``.
    invite_id = invite["_id"]

    # Register cleanup hook so a partial run still tidies the invite.
    member_user_id: str | None = None
    try:
        # ----- Phase 2: member registers a fresh account -------------------
        register_body = _register_user(api_url, member_email, member_password)
        assert register_body["email"] == member_email
        member_user_id = register_body["id"]

        # ----- Phase 3: member logs in and accepts the invite --------------
        member_token = _bearer_login(api_url, member_email, member_password)
        accept_resp = httpx.post(
            f"{api_url}/api/v1/workspaces/invites/{invite_token}/accept",
            headers=_auth_headers(member_token),
            timeout=10.0,
        )
        assert accept_resp.status_code == 200, (
            f"member.accept_invite failed: {accept_resp.status_code} "
            f"{accept_resp.text}"
        )

        # Re-read the member's identity to confirm the workspace membership.
        member_me = _get_me(api_url, member_token)
        member_ws = next(
            (
                ws
                for ws in member_me.get("workspaces", [])
                if ws["workspace"] == workspace_id
            ),
            None,
        )
        assert member_ws is not None, (
            "member must be a member of the workspace after accepting"
        )
        assert member_ws["role"] == "member", (
            f"expected role=member after accept, got {member_ws['role']!r}"
        )

        # ----- Phase 4: ADMIN-only action is blocked for member ------------
        # invite.create is registered as ADMIN-tier in
        # src/pocketpaw/ee/guards/actions.py. A workspace member must hit
        # 403 with code workspace.insufficient_role on the create-invite
        # route.
        blocked_resp = httpx.post(
            f"{api_url}/api/v1/workspaces/{workspace_id}/invites",
            headers=_auth_headers(member_token, workspace_id),
            json={"email": "would-not-be-sent@example.com", "role": "member"},
            timeout=10.0,
        )
        assert blocked_resp.status_code == 403, (
            f"member.create_invite must 403 (admin-tier action); "
            f"got {blocked_resp.status_code} {blocked_resp.text}"
        )
        body = blocked_resp.json()
        # CloudError envelope: {error: {code, message, ...}} per
        # ee/cloud/_core/errors.py. Be lenient on shape — only require
        # the code maps to insufficient_role somewhere in the payload.
        flattened = str(body).lower()
        assert "insufficient_role" in flattened or "forbidden" in flattened, (
            f"403 envelope must indicate role denial; got {body!r}"
        )

        # ----- Phase 5: owner promotes member to admin --------------------
        promote_resp = httpx.patch(
            f"{api_url}/api/v1/workspaces/{workspace_id}/members/{member_user_id}",
            headers=_auth_headers(owner_token, workspace_id),
            json={"role": "admin"},
            timeout=10.0,
        )
        assert promote_resp.status_code == 200, (
            f"owner.promote_to_admin failed: {promote_resp.status_code} "
            f"{promote_resp.text}"
        )

        # ----- Phase 6: previously-blocked admin op now succeeds ----------
        # Use a fresh email so the second invite isn't a duplicate-of-an
        # -outstanding-one collision.
        retry_email = f"s2-second-{int(time.time())}-{uuid.uuid4().hex[:6]}@example.com"
        retry_resp = httpx.post(
            f"{api_url}/api/v1/workspaces/{workspace_id}/invites",
            headers=_auth_headers(member_token, workspace_id),
            json={"email": retry_email, "role": "member"},
            timeout=10.0,
        )
        assert retry_resp.status_code == 200, (
            f"member-now-admin.create_invite must 200; got "
            f"{retry_resp.status_code} {retry_resp.text}"
        )
        retry_invite = retry_resp.json()
        assert retry_invite["email"] == retry_email
        assert retry_invite["role"] == "member"
        # Cleanup the retry invite immediately so we don't leak state.
        httpx.delete(
            f"{api_url}/api/v1/workspaces/{workspace_id}/invites/{retry_invite['_id']}",
            headers=_auth_headers(owner_token, workspace_id),
            timeout=10.0,
        )

    finally:
        # Best-effort cleanup. Each step swallows its own errors so a
        # partial-run failure doesn't mask the underlying assertion.
        try:
            httpx.delete(
                f"{api_url}/api/v1/workspaces/{workspace_id}/invites/{invite_id}",
                headers=_auth_headers(owner_token, workspace_id),
                timeout=10.0,
            )
        except Exception:
            pass
        if member_user_id is not None:
            try:
                _demote_member(api_url, owner_session, member_user_id)
            except Exception:
                pass
        try:
            _revoke_all_invites_for_email(api_url, owner_session, member_email)
        except Exception:
            pass
