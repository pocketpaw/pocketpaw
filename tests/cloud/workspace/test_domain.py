"""Tests for ee.cloud.workspace.domain."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember


def test_workspace_is_frozen() -> None:
    ws = Workspace(
        id="w1",
        name="Acme",
        slug="acme",
        owner="u1",
        plan="team",
        seats=5,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        ws.name = "Renamed"  # type: ignore[misc]


def test_workspace_default_member_count_zero() -> None:
    ws = Workspace(
        id="w1",
        name="A",
        slug="a",
        owner="u1",
        plan="team",
        seats=5,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert ws.member_count == 0
    assert ws.deleted_at is None


def test_workspace_member_is_frozen() -> None:
    m = WorkspaceMember(
        user_id="u1",
        email="a@b.c",
        name="A",
        avatar="",
        role="owner",
        joined_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        m.role = "admin"  # type: ignore[misc]


def test_invite_is_frozen() -> None:
    inv = Invite(
        id="i1",
        workspace_id="w1",
        email="a@b.c",
        role="member",
        invited_by="u1",
        token="tok",
        group_id=None,
        accepted=False,
        revoked=False,
        expired=False,
        expires_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        inv.accepted = True  # type: ignore[misc]


def test_invite_with_group() -> None:
    inv = Invite(
        id="i1",
        workspace_id="w1",
        email="a@b.c",
        role="member",
        invited_by="u1",
        token="tok",
        group_id="g1",
        accepted=False,
        revoked=False,
        expired=False,
        expires_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert inv.group_id == "g1"
