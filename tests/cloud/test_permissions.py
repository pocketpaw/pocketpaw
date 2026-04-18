from __future__ import annotations

import pytest

from ee.cloud.shared.errors import Forbidden
from ee.cloud.shared.permissions import (
    PocketAccess,
    WorkspaceRole,
    check_pocket_access,
    check_workspace_role,
)


def test_workspace_role_hierarchy():
    """OWNER > ADMIN > MEMBER by level."""
    assert WorkspaceRole.OWNER.level > WorkspaceRole.ADMIN.level
    assert WorkspaceRole.ADMIN.level > WorkspaceRole.MEMBER.level


def test_workspace_role_values():
    assert WorkspaceRole.MEMBER.value == "member"
    assert WorkspaceRole.ADMIN.value == "admin"
    assert WorkspaceRole.OWNER.value == "owner"


def test_check_workspace_role_passes():
    """Owner passes an admin-minimum check."""
    check_workspace_role("owner", minimum="admin")
    check_workspace_role("admin", minimum="admin")
    check_workspace_role("owner", minimum="member")


def test_check_workspace_role_fails():
    """Member fails an admin-minimum check with Forbidden."""
    with pytest.raises(Forbidden) as exc_info:
        check_workspace_role("member", minimum="admin")
    assert exc_info.value.status_code == 403


def test_pocket_access_hierarchy():
    """OWNER > EDIT > COMMENT > VIEW by level."""
    assert PocketAccess.OWNER.level > PocketAccess.EDIT.level
    assert PocketAccess.EDIT.level > PocketAccess.COMMENT.level
    assert PocketAccess.COMMENT.level > PocketAccess.VIEW.level


def test_pocket_access_values():
    assert PocketAccess.VIEW.value == "view"
    assert PocketAccess.COMMENT.value == "comment"
    assert PocketAccess.EDIT.value == "edit"
    assert PocketAccess.OWNER.value == "owner"


def test_check_pocket_access_passes():
    """Edit passes a view-minimum check."""
    check_pocket_access("edit", minimum="view")
    check_pocket_access("view", minimum="view")
    check_pocket_access("owner", minimum="edit")


def test_check_pocket_access_fails():
    """View fails an edit-minimum check with Forbidden."""
    with pytest.raises(Forbidden) as exc_info:
        check_pocket_access("view", minimum="edit")
    assert exc_info.value.status_code == 403
