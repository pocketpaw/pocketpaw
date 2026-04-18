"""Tests for pockets domain schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from ee.cloud.pockets.schemas import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    ReorderWidgetsRequest,
    ShareLinkRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
)


def test_create_pocket_defaults():
    req = CreatePocketRequest(name="My Pocket")
    assert req.visibility == "private" and req.session_id is None


def test_create_pocket_with_session():
    req = CreatePocketRequest(name="P", session_id="s123")
    assert req.session_id == "s123"


def test_create_pocket_all_fields():
    req = CreatePocketRequest(
        name="Full Pocket",
        description="A full pocket",
        type="dashboard",
        icon="star",
        color="#FF0000",
        visibility="workspace",
        session_id="sess1",
    )
    assert req.name == "Full Pocket"
    assert req.description == "A full pocket"
    assert req.type == "dashboard"
    assert req.icon == "star"
    assert req.color == "#FF0000"
    assert req.visibility == "workspace"
    assert req.session_id == "sess1"


def test_create_pocket_empty_name_rejected():
    with pytest.raises(PydanticValidationError):
        CreatePocketRequest(name="")


def test_create_pocket_name_too_long():
    with pytest.raises(PydanticValidationError):
        CreatePocketRequest(name="A" * 101)


def test_visibility_validation():
    with pytest.raises(PydanticValidationError):
        CreatePocketRequest(name="P", visibility="invalid")


def test_visibility_public():
    req = CreatePocketRequest(name="P", visibility="public")
    assert req.visibility == "public"


def test_visibility_workspace():
    req = CreatePocketRequest(name="P", visibility="workspace")
    assert req.visibility == "workspace"


def test_share_link_request():
    req = ShareLinkRequest(access="edit")
    assert req.access == "edit"


def test_share_link_request_default():
    req = ShareLinkRequest()
    assert req.access == "view"


def test_share_link_access_validation():
    with pytest.raises(PydanticValidationError):
        ShareLinkRequest(access="admin")


def test_add_widget_defaults():
    req = AddWidgetRequest(name="Chart")
    assert req.type == "custom" and req.data_source_type == "static"


def test_add_widget_all_fields():
    req = AddWidgetRequest(
        name="Sales Chart",
        type="chart",
        icon="bar-chart",
        color="#00FF00",
        span="col-span-2",
        data_source_type="api",
        config={"endpoint": "/api/sales"},
        props={"title": "Sales"},
        assigned_agent="agent1",
    )
    assert req.name == "Sales Chart"
    assert req.type == "chart"
    assert req.span == "col-span-2"
    assert req.data_source_type == "api"
    assert req.config["endpoint"] == "/api/sales"
    assert req.assigned_agent == "agent1"


def test_add_widget_empty_name_rejected():
    with pytest.raises(PydanticValidationError):
        AddWidgetRequest(name="")


def test_add_widget_name_too_long():
    with pytest.raises(PydanticValidationError):
        AddWidgetRequest(name="A" * 101)


def test_update_widget_all_optional():
    req = UpdateWidgetRequest()
    assert req.name is None
    assert req.type is None
    assert req.config is None
    assert req.data is None


def test_update_widget_partial():
    req = UpdateWidgetRequest(name="New Name", config={"k": "v"})
    assert req.name == "New Name"
    assert req.config == {"k": "v"}


def test_reorder_widgets():
    req = ReorderWidgetsRequest(widget_ids=["w1", "w2", "w3"])
    assert len(req.widget_ids) == 3


def test_reorder_widgets_empty():
    req = ReorderWidgetsRequest(widget_ids=[])
    assert req.widget_ids == []


def test_add_collaborator():
    req = AddCollaboratorRequest(user_id="u1", access="comment")
    assert req.access == "comment"


def test_add_collaborator_default_access():
    req = AddCollaboratorRequest(user_id="u1")
    assert req.access == "edit"


def test_add_collaborator_invalid_access():
    with pytest.raises(PydanticValidationError):
        AddCollaboratorRequest(user_id="u1", access="admin")


def test_update_pocket_all_optional():
    req = UpdatePocketRequest()
    assert req.name is None
    assert req.ripple_spec is None


def test_update_pocket_with_ripple_spec():
    req = UpdatePocketRequest(ripple_spec={"widgets": [{"name": "w1"}]})
    assert req.ripple_spec is not None
    assert req.ripple_spec["widgets"][0]["name"] == "w1"


def test_pocket_response_model():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    from ee.cloud.pockets.schemas import PocketResponse

    resp = PocketResponse(
        id="p1",
        workspace="ws1",
        name="Test Pocket",
        description="desc",
        type="custom",
        icon="",
        color="",
        owner="u1",
        visibility="private",
        team=[],
        agents=[],
        widgets=[],
        shared_with=[],
        created_at=now,
        updated_at=now,
    )
    assert resp.id == "p1"
    assert resp.share_link_token is None
    assert resp.share_link_access == "view"
