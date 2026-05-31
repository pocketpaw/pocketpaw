"""Tests for ee.cloud.workspace.dto."""

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember
from pocketpaw_ee.cloud.workspace.dto import (
    invite_to_dto,
    invite_to_validate_dto,
    member_to_dto,
    workspace_to_dto,
)


def _ws(**kw) -> Workspace:
    base = dict(
        id="w1",
        name="Acme",
        slug="acme",
        owner="u1",
        plan="team",
        seats=5,
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        member_count=3,
    )
    base.update(kw)
    return Workspace(**base)


def _member() -> WorkspaceMember:
    return WorkspaceMember(
        user_id="u1",
        email="a@b.c",
        name="Alice",
        avatar="img",
        role="owner",
        joined_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _inv(**kw) -> Invite:
    base = dict(
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
        expires_at=datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC),
    )
    base.update(kw)
    return Invite(**base)


def test_workspace_dto_wire_keys() -> None:
    dump = workspace_to_dto(_ws()).model_dump(by_alias=True)
    assert set(dump.keys()) == {
        "_id",
        "name",
        "slug",
        "owner",
        "plan",
        "seats",
        "createdAt",
        "memberCount",
    }


def test_workspace_dto_values() -> None:
    dump = workspace_to_dto(_ws()).model_dump(by_alias=True)
    assert dump["_id"] == "w1"
    assert dump["memberCount"] == 3
    assert dump["createdAt"] == "2026-04-27T12:00:00+00:00"


def test_member_dto_wire_keys() -> None:
    dump = member_to_dto(_member()).model_dump(by_alias=True)
    assert set(dump.keys()) == {"_id", "email", "name", "avatar", "role", "joinedAt"}


def test_member_dto_values() -> None:
    dump = member_to_dto(_member()).model_dump(by_alias=True)
    assert dump["_id"] == "u1"
    assert dump["role"] == "owner"
    assert dump["joinedAt"] == "2026-01-01T00:00:00+00:00"


def test_invite_dto_wire_keys() -> None:
    dump = invite_to_dto(_inv()).model_dump(by_alias=True)
    assert set(dump.keys()) == {
        "_id",
        "email",
        "role",
        "invitedBy",
        "token",
        "accepted",
        "revoked",
        "expired",
        "expiresAt",
    }


def test_invite_dto_values() -> None:
    dump = invite_to_dto(_inv()).model_dump(by_alias=True)
    assert dump["_id"] == "i1"
    assert dump["invitedBy"] == "u1"
    assert dump["expired"] is False


def test_validate_invite_adds_valid_and_workspace_name() -> None:
    dump = invite_to_validate_dto(_inv(), workspace_name="Acme").model_dump(by_alias=True)
    assert dump["valid"] is True
    assert dump["workspace_name"] == "Acme"


def test_validate_invite_invalid_when_accepted() -> None:
    dump = invite_to_validate_dto(_inv(accepted=True), workspace_name="Acme").model_dump(
        by_alias=True
    )
    assert dump["valid"] is False


def test_validate_invite_invalid_when_revoked() -> None:
    dump = invite_to_validate_dto(_inv(revoked=True), workspace_name="Acme").model_dump(
        by_alias=True
    )
    assert dump["valid"] is False


def test_validate_invite_invalid_when_expired() -> None:
    dump = invite_to_validate_dto(_inv(expired=True), workspace_name="Acme").model_dump(
        by_alias=True
    )
    assert dump["valid"] is False
