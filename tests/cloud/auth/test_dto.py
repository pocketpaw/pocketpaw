"""Tests for ee.cloud.auth.dto."""

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.auth.domain import AuthUser, WorkspaceMembershipRef
from pocketpaw_ee.cloud.auth.dto import (
    ProfileUpdateRequest,
    SetWorkspaceRequest,
    auth_user_to_profile_out,
)


def _user(**overrides) -> AuthUser:
    base = dict(
        id="u1",
        email="a@b.c",
        full_name="Alice",
        avatar="https://...",
        status="online",
        active_workspace="w1",
        workspaces=(
            WorkspaceMembershipRef(
                workspace="w1",
                role="owner",
                joined_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        is_verified=True,
        is_superuser=False,
    )
    base.update(overrides)
    return AuthUser(**base)


def test_profile_out_keys_match_existing_wire_shape() -> None:
    out = auth_user_to_profile_out(_user())
    dump = out.model_dump()
    # Existing wire shape uses camelCase for some fields
    assert set(dump.keys()) == {
        "id",
        "email",
        "name",
        "image",
        "emailVerified",
        "activeWorkspace",
        "workspaces",
    }


def test_auth_user_to_profile_out_field_mapping() -> None:
    out = auth_user_to_profile_out(_user())
    dump = out.model_dump()
    assert dump["id"] == "u1"
    assert dump["email"] == "a@b.c"
    assert dump["name"] == "Alice"
    assert dump["image"] == "https://..."
    assert dump["emailVerified"] is True
    assert dump["activeWorkspace"] == "w1"
    assert dump["workspaces"] == [{"workspace": "w1", "role": "owner"}]


def test_auth_user_with_no_active_workspace() -> None:
    out = auth_user_to_profile_out(_user(active_workspace=None))
    assert out.model_dump()["activeWorkspace"] is None


def test_auth_user_with_no_workspaces() -> None:
    out = auth_user_to_profile_out(_user(workspaces=()))
    assert out.model_dump()["workspaces"] == []


def test_profile_update_request_all_optional() -> None:
    req = ProfileUpdateRequest()
    assert req.full_name is None
    assert req.avatar is None
    assert req.status is None


def test_set_workspace_request_requires_id() -> None:
    req = SetWorkspaceRequest(workspace_id="w1")
    assert req.workspace_id == "w1"
