"""Tests for workspace domain schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from ee.cloud.workspace.schemas import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateMemberRoleRequest,
    UpdateWorkspaceRequest,
)


def test_create_workspace_required_fields():
    req = CreateWorkspaceRequest(name="Acme Corp", slug="acme-corp")
    assert req.name == "Acme Corp"
    assert req.slug == "acme-corp"


def test_create_workspace_slug_validation():
    with pytest.raises(PydanticValidationError):
        CreateWorkspaceRequest(name="Test", slug="Invalid Slug!")


def test_create_workspace_single_char_slug():
    req = CreateWorkspaceRequest(name="X", slug="x")
    assert req.slug == "x"


def test_create_workspace_slug_no_leading_hyphen():
    with pytest.raises(PydanticValidationError):
        CreateWorkspaceRequest(name="Test", slug="-bad")


def test_create_workspace_slug_no_trailing_hyphen():
    with pytest.raises(PydanticValidationError):
        CreateWorkspaceRequest(name="Test", slug="bad-")


def test_create_workspace_slug_no_uppercase():
    with pytest.raises(PydanticValidationError):
        CreateWorkspaceRequest(name="Test", slug="BadSlug")


def test_create_workspace_empty_name_rejected():
    with pytest.raises(PydanticValidationError):
        CreateWorkspaceRequest(name="", slug="ok")


def test_create_workspace_empty_slug_rejected():
    with pytest.raises(PydanticValidationError):
        CreateWorkspaceRequest(name="OK", slug="")


def test_update_workspace_all_optional():
    req = UpdateWorkspaceRequest()
    assert req.name is None
    assert req.settings is None


def test_update_workspace_with_values():
    req = UpdateWorkspaceRequest(name="New Name", settings={"key": "value"})
    assert req.name == "New Name"
    assert req.settings == {"key": "value"}


def test_create_invite_defaults():
    req = CreateInviteRequest(email="test@example.com")
    assert req.role == "member"
    assert req.group_id is None


def test_create_invite_admin_role():
    req = CreateInviteRequest(email="test@example.com", role="admin")
    assert req.role == "admin"


def test_create_invite_role_validation():
    with pytest.raises(PydanticValidationError):
        CreateInviteRequest(email="test@example.com", role="superadmin")


def test_create_invite_with_group():
    req = CreateInviteRequest(email="a@b.com", role="member", group_id="grp123")
    assert req.group_id == "grp123"


def test_update_member_role_request():
    req = UpdateMemberRoleRequest(role="admin")
    assert req.role == "admin"


def test_update_member_role_owner():
    req = UpdateMemberRoleRequest(role="owner")
    assert req.role == "owner"


def test_update_member_role_invalid():
    with pytest.raises(PydanticValidationError):
        UpdateMemberRoleRequest(role="superadmin")
