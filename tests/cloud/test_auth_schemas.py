"""Tests for auth domain schemas."""

from ee.cloud.auth.schemas import ProfileUpdateRequest, SetWorkspaceRequest, UserResponse


def test_profile_update_optional_fields():
    body = ProfileUpdateRequest()
    assert body.full_name is None and body.avatar is None and body.status is None


def test_profile_update_with_values():
    body = ProfileUpdateRequest(full_name="Rohit", avatar="https://example.com/img.png")
    assert body.full_name == "Rohit"


def test_set_workspace_request():
    body = SetWorkspaceRequest(workspace_id="ws123")
    assert body.workspace_id == "ws123"


def test_user_response():
    resp = UserResponse(
        id="1",
        email="a@b.com",
        name="Test",
        image="",
        email_verified=True,
        active_workspace=None,
        workspaces=[],
    )
    assert resp.email == "a@b.com"
