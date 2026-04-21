"""Tests for agents domain schemas.

Updated 2026-04-19 (feat/cluster-d-agent-scope-picker): added scope-field
coverage on CreateAgentRequest + UpdateAgentRequest, plus tests for the
new ScopeAssignmentRequest / ScopeAssignmentResponse envelopes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from ee.cloud.agents.schemas import (
    AgentResponse,
    CreateAgentRequest,
    DiscoverRequest,
    ScopeAssignmentRequest,
    ScopeAssignmentResponse,
    UpdateAgentRequest,
)


def test_create_agent_required_fields():
    req = CreateAgentRequest(name="My Agent", slug="my-agent")
    assert req.name == "My Agent" and req.backend == "claude_agent_sdk"


def test_create_agent_with_backend():
    req = CreateAgentRequest(name="A", slug="a", backend="claude_agent_sdk")
    assert req.backend == "claude_agent_sdk"


def test_create_agent_defaults():
    req = CreateAgentRequest(name="Test", slug="test")
    assert req.avatar == ""
    assert req.visibility == "private"
    assert req.backend == "claude_agent_sdk"
    assert req.model == ""


def test_create_agent_all_fields():
    req = CreateAgentRequest(
        name="Full Agent",
        slug="full-agent",
        avatar="https://example.com/avatar.png",
        visibility="workspace",
        model="claude-sonnet-4-5-20250514",
    )
    assert req.name == "Full Agent"
    assert req.slug == "full-agent"
    assert req.avatar == "https://example.com/avatar.png"
    assert req.visibility == "workspace"
    assert req.model == "claude-sonnet-4-5-20250514"


def test_create_agent_public_visibility():
    req = CreateAgentRequest(name="Public", slug="pub", visibility="public")
    assert req.visibility == "public"


def test_create_agent_empty_name_rejected():
    with pytest.raises(PydanticValidationError):
        CreateAgentRequest(name="", slug="ok")


def test_create_agent_empty_slug_rejected():
    with pytest.raises(PydanticValidationError):
        CreateAgentRequest(name="OK", slug="")


def test_create_agent_name_too_long():
    with pytest.raises(PydanticValidationError):
        CreateAgentRequest(name="A" * 101, slug="ok")


def test_create_agent_slug_too_long():
    with pytest.raises(PydanticValidationError):
        CreateAgentRequest(name="OK", slug="a" * 51)


def test_update_agent_all_optional():
    req = UpdateAgentRequest()
    assert req.name is None
    assert req.avatar is None
    assert req.visibility is None
    assert req.config is None


def test_update_agent_partial():
    req = UpdateAgentRequest(name="New Name")
    assert req.name == "New Name"
    assert req.config is None


def test_update_agent_with_config():
    req = UpdateAgentRequest(config={"temperature": 0.5})
    assert req.config["temperature"] == 0.5


def test_update_agent_visibility():
    req = UpdateAgentRequest(visibility="workspace")
    assert req.visibility == "workspace"


def test_update_agent_invalid_visibility():
    with pytest.raises(PydanticValidationError):
        UpdateAgentRequest(visibility="invalid")


def test_discover_defaults():
    req = DiscoverRequest()
    assert req.page == 1 and req.page_size == 20
    assert req.query == ""
    assert req.visibility is None


def test_discover_with_filters():
    req = DiscoverRequest(query="test", visibility="workspace", page=2, page_size=50)
    assert req.query == "test"
    assert req.visibility == "workspace"
    assert req.page == 2
    assert req.page_size == 50


def test_discover_page_min():
    with pytest.raises(PydanticValidationError):
        DiscoverRequest(page=0)


def test_discover_page_size_max():
    with pytest.raises(PydanticValidationError):
        DiscoverRequest(page_size=101)


def test_discover_page_size_min():
    with pytest.raises(PydanticValidationError):
        DiscoverRequest(page_size=0)


def test_visibility_validation():
    with pytest.raises(PydanticValidationError):
        CreateAgentRequest(name="A", slug="a", visibility="invalid")


def test_agent_response_model():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    resp = AgentResponse(
        id="abc123",
        workspace="ws1",
        name="Agent",
        slug="agent",
        avatar="",
        visibility="private",
        config={"backend": "claude_agent_sdk"},
        owner="user1",
        created_at=now,
        updated_at=now,
    )
    assert resp.id == "abc123"
    assert resp.config["backend"] == "claude_agent_sdk"


# ---------------------------------------------------------------------------
# Scope field coverage (cluster-d-agent-scope-picker)
# ---------------------------------------------------------------------------


def test_create_agent_with_scopes_normalises():
    req = CreateAgentRequest(
        name="Sales Bot",
        slug="sales-bot",
        scopes=["  Org:Sales:*  ", "org:sales:*"],  # whitespace + dedupe
    )
    assert req.scopes == ["org:sales:*"]


def test_create_agent_with_invalid_scope_rejected():
    with pytest.raises(PydanticValidationError):
        CreateAgentRequest(
            name="Bad", slug="bad", scopes=["org:*:leads"]
        )  # mid-segment wildcard


def test_create_agent_universal_wildcard_rejected():
    with pytest.raises(PydanticValidationError):
        CreateAgentRequest(name="Bad", slug="bad", scopes=["*"])


def test_update_agent_scopes_can_clear_to_empty_list():
    req = UpdateAgentRequest(scopes=[])
    assert req.scopes == []


def test_update_agent_scopes_normalises():
    req = UpdateAgentRequest(scopes=["ORG:SALES:LEADS"])
    assert req.scopes == ["org:sales:leads"]


def test_scope_assignment_request_requires_scopes_field():
    with pytest.raises(PydanticValidationError):
        ScopeAssignmentRequest()  # type: ignore[call-arg]


def test_scope_assignment_request_empty_list_ok():
    req = ScopeAssignmentRequest(scopes=[])
    assert req.scopes == []


def test_scope_assignment_request_normalises():
    req = ScopeAssignmentRequest(scopes=["org:sales", "ORG:SALES"])
    assert req.scopes == ["org:sales"]


def test_scope_assignment_request_rejects_bad_grammar():
    with pytest.raises(PydanticValidationError):
        ScopeAssignmentRequest(scopes=["org::broken"])


def test_scope_assignment_response_envelope():
    resp = ScopeAssignmentResponse(agent_id="abc", scopes=["org:sales:*"])
    assert resp.agent_id == "abc"
    assert resp.scopes == ["org:sales:*"]
