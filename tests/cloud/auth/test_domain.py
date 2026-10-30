"""Tests for ee.cloud.auth.domain."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.auth.domain import AuthUser, WorkspaceMembershipRef


def test_workspace_membership_ref_is_frozen() -> None:
    m = WorkspaceMembershipRef(workspace="w1", role="admin", joined_at=datetime.now(UTC))
    with pytest.raises(FrozenInstanceError):
        m.role = "owner"  # type: ignore[misc]


def test_auth_user_is_frozen() -> None:
    u = AuthUser(
        id="u1",
        email="a@b.c",
        full_name="A B",
        avatar="",
        status="online",
        active_workspace=None,
        workspaces=(),
        is_verified=True,
        is_superuser=False,
    )
    with pytest.raises(FrozenInstanceError):
        u.email = "x@y.z"  # type: ignore[misc]


def test_auth_user_minimal_fields() -> None:
    u = AuthUser(
        id="u1",
        email="a@b.c",
        full_name="",
        avatar="",
        status="offline",
        active_workspace=None,
        workspaces=(),
        is_verified=False,
        is_superuser=False,
    )
    assert u.id == "u1"
    assert u.workspaces == ()
    assert u.active_workspace is None


def test_auth_user_with_workspaces() -> None:
    m1 = WorkspaceMembershipRef(
        workspace="w1", role="owner", joined_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    m2 = WorkspaceMembershipRef(
        workspace="w2", role="member", joined_at=datetime(2026, 2, 1, tzinfo=UTC)
    )
    u = AuthUser(
        id="u1",
        email="a@b.c",
        full_name="A",
        avatar="",
        status="online",
        active_workspace="w1",
        workspaces=(m1, m2),
        is_verified=True,
        is_superuser=False,
    )
    assert len(u.workspaces) == 2
    assert u.workspaces[0].role == "owner"
