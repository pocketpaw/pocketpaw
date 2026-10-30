# tests/ee/test_rules_service.py — EE persistence tests for the rules entity (S2-R1).
#
# Exercises ``pocketpaw_ee.cloud.rules.service`` against the in-memory
# ``beanie_test_db`` fixture (mongomock-motor). Covers:
#   - create_rule persists an InstinctRuleDoc and returns a wire dict
#   - get_active_rules(workspace_id) returns the created rule
#   - a SECOND workspace's get_active_rules returns NONE (tenancy isolation)
#   - archived rules are excluded from get_active_rules
# ``tests/ee`` has no autouse RecordingBus, so a local inert bus fixture keeps
# the service's ``emit`` call from raising (mirrors test_discovery_propose.py).

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.rules import service as rules_service
from pocketpaw_ee.cloud.rules.dto import CreateRuleRequest


@pytest.fixture(autouse=True)
def recording_bus():
    """Inert recording EventBus so service ``emit()`` calls don't raise.

    The cloud create path emits via ``_core.realtime.emit.emit`` which asserts
    a bus is set; ``tests/cloud`` has an autouse fixture for this but
    ``tests/ee`` does not, so we mint a minimal one (per test_discovery_propose).
    """
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler: Any) -> None:  # noqa: ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]
    yield bus_mod._bus
    bus_mod._bus = prev  # type: ignore[attr-defined]


def _draft_blob(workspace_id: str = "ws_alpha", name: str = "Escalate refunds") -> dict:
    return {
        "name": name,
        "description": "Refunds over $1000 need a human.",
        "when": "amount > 1000",
        "action": "require_approval",
        "scope": {"workspace_id": workspace_id, "pocket_id": "pk_refunds"},
        "confidence": 0.82,
        "provenance": ["audit:row1", "correction:c2"],
    }


def _create_body(workspace_id: str = "ws_alpha", name: str = "Escalate refunds") -> dict:
    return {
        "draft": _draft_blob(workspace_id=workspace_id, name=name),
        "owner_user_id": "user_1",
    }


async def test_create_rule_persists_and_returns_wire_dict(beanie_test_db) -> None:
    wire = await rules_service.create_rule(
        workspace_id="ws_alpha",
        user_id="user_1",
        body=_create_body(),
    )
    assert isinstance(wire, dict)
    assert wire["id"]
    assert wire["workspace_id"] == "ws_alpha"
    assert wire["owner_user_id"] == "user_1"
    assert wire["status"] == "active"
    assert wire["name"] == "Escalate refunds"
    assert wire["when"] == "amount > 1000"
    assert wire["action"] == "require_approval"
    assert wire["scope"]["workspace_id"] == "ws_alpha"
    assert wire["confidence"] == pytest.approx(0.82)
    assert wire["provenance"] == ["audit:row1", "correction:c2"]


async def test_create_rule_accepts_typed_request(beanie_test_db) -> None:
    """create_rule re-validates at entry, so a typed CreateRuleRequest works
    identically to a plain dict (internal callers pass the typed form)."""
    body = CreateRuleRequest.model_validate(_create_body())
    wire = await rules_service.create_rule(workspace_id="ws_alpha", user_id="user_1", body=body)
    assert wire["name"] == "Escalate refunds"


async def test_get_active_rules_returns_created(beanie_test_db) -> None:
    await rules_service.create_rule(workspace_id="ws_alpha", user_id="user_1", body=_create_body())
    rows = await rules_service.get_active_rules("ws_alpha")
    assert len(rows) == 1
    assert rows[0]["name"] == "Escalate refunds"
    assert rows[0]["workspace_id"] == "ws_alpha"
    assert rows[0]["status"] == "active"


async def test_get_active_rules_tenant_isolated(beanie_test_db) -> None:
    """A second workspace sees NONE of the first workspace's rules."""
    await rules_service.create_rule(
        workspace_id="ws_alpha", user_id="user_1", body=_create_body("ws_alpha")
    )
    rows_other = await rules_service.get_active_rules("ws_beta")
    assert rows_other == []


async def test_get_active_rules_excludes_archived(beanie_test_db) -> None:
    """Archived rules are filtered out of the active read."""
    wire = await rules_service.create_rule(
        workspace_id="ws_alpha", user_id="user_1", body=_create_body()
    )
    # Directly archive via the service-owned archive helper.
    archived = await rules_service.archive_rule(
        workspace_id="ws_alpha", user_id="user_1", rule_id=wire["id"]
    )
    assert archived["status"] == "archived"

    rows = await rules_service.get_active_rules("ws_alpha")
    assert rows == []


async def test_create_rule_rejects_workspace_mismatch(beanie_test_db) -> None:
    """The draft scope workspace must match the caller's workspace — a
    mismatch is a ValidationError (CloudError), not a silent cross-tenant write."""
    from pocketpaw_ee.cloud._core.errors import CloudError

    body = _create_body(workspace_id="ws_other")  # scope says ws_other
    with pytest.raises(CloudError):
        await rules_service.create_rule(
            workspace_id="ws_alpha",  # caller is ws_alpha
            user_id="user_1",
            body=body,
        )
