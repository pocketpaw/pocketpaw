# test_pocket_actions_ops.py — Tests for the edit-specialist `actions` ops.
# Created: 2026-05-22 (RFC 05 M2a) — the write-action sibling of
#   test_pocket_sources_ops.py. Covers the pure `actions_ops` helpers, the
#   service-layer `agent_set_action` / `agent_remove_action` functions
#   (happy path, invalid-binding rejection, governance-field carry-through,
#   persistence, emitted mutation), the `set_action` / `remove_action`
#   agent_context wrappers + edit-tool factories, and the owner-only
#   `set_pocket_write_policy` service function.

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import actions_ops, agent_context
from pocketpaw_ee.cloud.pockets import service as pocket_service

# ---------------------------------------------------------------------------
# Fake doc + patch helper (mirrors test_pocket_sources_ops.py)
# ---------------------------------------------------------------------------


class _FakeDoc:
    """Minimum surface to stand in for a ``_PocketDoc``. Mutations operate
    on ``self.rippleSpec`` in place; ``save()`` is an async no-op counted
    via ``saves``."""

    def __init__(self, pocket_id: str, ripple_spec: dict[str, Any]):
        self.id = pocket_id
        self.workspace = "w1"
        self.name = "Test"
        self.description = ""
        self.type = "custom"
        self.icon = ""
        self.color = ""
        self.owner = "u1"
        self.visibility = "workspace"
        self.team: list[str] = []
        self.agents: list[str] = []
        self.widgets: list[Any] = []
        self.rippleSpec = ripple_spec
        self.share_link_token = None
        self.share_link_access = "view"
        self.shared_with: list[str] = []
        self.tool_specs: list[Any] = []
        self.saves = 0

    async def save(self) -> None:
        self.saves += 1

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "_id": self.id,
            "workspace": self.workspace,
            "rippleSpec": self.rippleSpec,
            "owner": self.owner,
        }


@pytest.fixture
def fake_doc() -> _FakeDoc:
    """A persisted pocket with a UI tree + seeded state but NO actions."""
    return _FakeDoc(
        "507f1f77bcf86cd799439011",
        {
            "version": "1.0",
            "state": {"leases": []},
            "ui": {"id": "n_root0000", "type": "flex", "children": []},
        },
    )


def _patches(doc: _FakeDoc):
    """Patch the doc fetch + emit/push seams + identity ContextVars.
    Returns ``(ExitStack, push_calls)``."""
    push_calls: list[dict[str, Any]] = []

    def _capture(payload: dict[str, Any]) -> None:
        push_calls.append(payload)

    stack = ExitStack()
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._PocketDoc.get",
            new=AsyncMock(return_value=doc),
        )
    )
    stack.enter_context(patch("pocketpaw_ee.cloud.pockets.service.emit", new=AsyncMock()))
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._pocket_event_payload",
            new=AsyncMock(return_value={"pocket_id": doc.id}),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.push_pocket_mutation",
            new=MagicMock(side_effect=_capture),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service.normalize_ripple_spec",
            new=lambda s: s,
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=doc.workspace),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value=doc.owner),
        )
    )
    return stack, push_calls


_VALID_BINDING = {
    "kind": "write_binding",
    "method": "POST",
    "path": "/leases/{item.id}/renew",
    "params": {"proposed_rent": "{state.form.rent}"},
    "confirm": False,
}


# ---------------------------------------------------------------------------
# Pure ops — actions_ops.set_action / remove_action
# ---------------------------------------------------------------------------


def test_set_action_creates_actions_dict_when_absent():
    spec: dict[str, Any] = {"ui": {}, "state": {}}
    out = actions_ops.set_action(spec, "mark_renewed", dict(_VALID_BINDING))
    assert out is spec
    assert spec["actions"]["mark_renewed"] == _VALID_BINDING


def test_set_action_adds_alongside_existing_actions():
    spec = {"actions": {"a": dict(_VALID_BINDING)}}
    actions_ops.set_action(spec, "b", dict(_VALID_BINDING))
    assert set(spec["actions"]) == {"a", "b"}


def test_set_action_overwrites_existing_key():
    spec = {"actions": {"mark_renewed": {"method": "PATCH", "path": "/old"}}}
    actions_ops.set_action(spec, "mark_renewed", dict(_VALID_BINDING))
    assert spec["actions"]["mark_renewed"]["path"] == "/leases/{item.id}/renew"


def test_set_action_requires_a_key():
    with pytest.raises(ValueError, match="action key"):
        actions_ops.set_action({}, "", dict(_VALID_BINDING))


def test_set_action_rejects_malformed_actions_block():
    """An `actions` value that is not a dict is a malformed spec — refuse
    to append rather than overwrite it."""
    with pytest.raises(ValueError, match="expected an object"):
        actions_ops.set_action({"actions": ["bad"]}, "x", dict(_VALID_BINDING))


def test_remove_action_drops_the_key():
    spec = {"actions": {"a": dict(_VALID_BINDING), "b": dict(_VALID_BINDING)}}
    actions_ops.remove_action(spec, "a")
    assert "a" not in spec["actions"]
    assert "b" in spec["actions"]


def test_remove_action_is_a_noop_when_key_absent():
    spec = {"actions": {"a": dict(_VALID_BINDING)}}
    out = actions_ops.remove_action(spec, "nope")
    assert out is spec
    assert "a" in spec["actions"]


def test_remove_action_is_a_noop_when_actions_absent():
    spec: dict[str, Any] = {"ui": {}}
    out = actions_ops.remove_action(spec, "a")
    assert out is spec
    assert "actions" not in spec


# ---------------------------------------------------------------------------
# Service layer — agent_set_action
# ---------------------------------------------------------------------------


async def test_agent_set_action_adds_actions_block_to_existing_pocket(fake_doc):
    """An EXISTING persisted pocket with no ``rippleSpec.actions`` gains a
    write binding when the edit specialist calls ``agent_set_action``."""
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_action(
            fake_doc.id, action_key="mark_renewed", binding=dict(_VALID_BINDING)
        )
    assert err is None
    assert result is not None
    assert fake_doc.rippleSpec["actions"]["mark_renewed"]["method"] == "POST"
    assert fake_doc.rippleSpec["actions"]["mark_renewed"]["path"] == "/leases/{item.id}/renew"
    assert fake_doc.saves == 1


async def test_agent_set_action_returns_view_with_binding(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_action(
            fake_doc.id, action_key="mark_renewed", binding=dict(_VALID_BINDING)
        )
    assert err is None
    assert result is not None
    assert result["action_key"] == "mark_renewed"
    assert result["binding"]["method"] == "POST"
    assert result["pocket"]["rippleSpec"]["actions"]["mark_renewed"]["confirm"] is False


async def test_agent_set_action_rejects_invalid_binding(fake_doc):
    """A binding missing ``method`` is rejected via the ``ActionBinding``
    model before any write happens."""
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_action(
            fake_doc.id, action_key="bad", binding={"kind": "write_binding", "path": "/x"}
        )
    assert result is None
    assert err is not None
    assert "binding" in err.lower()
    assert "actions" not in fake_doc.rippleSpec
    assert fake_doc.saves == 0
    assert not push_calls


async def test_agent_set_action_rejects_read_verb(fake_doc):
    """``method`` is a write-verb Literal — a GET binding is rejected."""
    ctx, _ = _patches(fake_doc)
    bad = dict(_VALID_BINDING, method="GET")
    with ctx:
        result, err = await pocket_service.agent_set_action(
            fake_doc.id, action_key="bad", binding=bad
        )
    assert result is None
    assert err is not None


async def test_agent_set_action_requires_a_key(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_action(
            fake_doc.id, action_key="", binding=dict(_VALID_BINDING)
        )
    assert result is None
    assert err is not None
    assert "action_key" in err


async def test_agent_set_action_carries_governance_fields_through(fake_doc):
    """M2b governance fields (``requires_instinct`` / ``outcome``) survive
    on the persisted binding. RFC 05 M2b.1 promoted them to real declared
    ``ActionBinding`` fields, so they round-trip through validation."""
    ctx, _ = _patches(fake_doc)
    binding = {
        **_VALID_BINDING,
        "requires_instinct": True,
        "outcome": "renewal_completed",
    }
    with ctx:
        result, err = await pocket_service.agent_set_action(
            fake_doc.id, action_key="mark_renewed", binding=binding
        )
    assert err is None
    stored = fake_doc.rippleSpec["actions"]["mark_renewed"]
    assert stored["requires_instinct"] is True
    assert stored["outcome"] == "renewal_completed"
    # The validated core fields are still present.
    assert stored["method"] == "POST"


# ---------------------------------------------------------------------------
# Service layer — agent_remove_action
# ---------------------------------------------------------------------------


async def test_agent_remove_action_drops_the_binding():
    doc = _FakeDoc(
        "507f1f77bcf86cd799439022",
        {
            "version": "1.0",
            "actions": {"mark_renewed": dict(_VALID_BINDING)},
            "state": {},
            "ui": {"id": "n_root0000", "type": "flex"},
        },
    )
    ctx, _ = _patches(doc)
    with ctx:
        result, err = await pocket_service.agent_remove_action(doc.id, action_key="mark_renewed")
    assert err is None
    assert result is not None
    assert "mark_renewed" not in doc.rippleSpec.get("actions", {})
    assert doc.saves == 1


async def test_agent_remove_action_noop_when_absent(fake_doc):
    """Removing an action that was never declared still succeeds."""
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_remove_action(fake_doc.id, action_key="ghost")
    assert err is None
    assert result is not None
    assert fake_doc.saves == 1


# ---------------------------------------------------------------------------
# agent_context wrappers
# ---------------------------------------------------------------------------


async def test_set_action_for_agent_returns_ok_shape(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_action_for_agent(
            fake_doc.id, "mark_renewed", dict(_VALID_BINDING)
        )
    assert result["ok"] is True
    assert fake_doc.rippleSpec["actions"]["mark_renewed"]["method"] == "POST"
    assert push_calls[0]["action"] == "replace"


async def test_set_action_for_agent_surfaces_error(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_action_for_agent(
            fake_doc.id, "bad", {"kind": "write_binding", "path": "/x"}
        )
    assert result["ok"] is False
    assert "error" in result


async def test_remove_action_for_agent_returns_ok_shape():
    doc = _FakeDoc(
        "507f1f77bcf86cd799439033",
        {"version": "1.0", "actions": {"mark_renewed": dict(_VALID_BINDING)}, "ui": {}},
    )
    ctx, _ = _patches(doc)
    with ctx:
        result = await agent_context.remove_action_for_agent(doc.id, "mark_renewed")
    assert result["ok"] is True
    assert "mark_renewed" not in doc.rippleSpec.get("actions", {})


# ---------------------------------------------------------------------------
# Edit tool factories
# ---------------------------------------------------------------------------


async def test_action_tools_registered_in_edit_toolset():
    from pocketpaw_ee.agent.pocket_specialist.tools import make_edit_pocket_tools

    tools = make_edit_pocket_tools(pocket_id="507f1f77bcf86cd799439011")
    names = {t.name for t in tools}
    assert "set_action" in names
    assert "remove_action" in names


async def test_set_action_tool_runs_and_captures_op(fake_doc):
    from pocketpaw_ee.agent.pocket_specialist.tools import make_set_action_tool

    capture: dict[str, Any] = {}
    tool = make_set_action_tool(pocket_id=fake_doc.id, capture=capture)
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await tool.coroutine(
            action_key="mark_renewed",
            method="post",  # lower-case — the tool upper-cases it
            path="/leases/{item.id}/renew",
            params={"proposed_rent": "{state.form.rent}"},
            confirm=False,
            on_success=[{"action": "run_source", "source": "leases"}],
        )
    assert result["ok"] is True
    stored = fake_doc.rippleSpec["actions"]["mark_renewed"]
    assert stored["method"] == "POST"
    assert stored["on_success"] == [{"action": "run_source", "source": "leases"}]
    assert capture["ops"][0]["op"] == "set_action"


async def test_remove_action_tool_runs_and_captures_op():
    from pocketpaw_ee.agent.pocket_specialist.tools import make_remove_action_tool

    doc = _FakeDoc(
        "507f1f77bcf86cd799439044",
        {"version": "1.0", "actions": {"mark_renewed": dict(_VALID_BINDING)}, "ui": {}},
    )
    capture: dict[str, Any] = {}
    tool = make_remove_action_tool(pocket_id=doc.id, capture=capture)
    ctx, _ = _patches(doc)
    with ctx:
        result = await tool.coroutine(action_key="mark_renewed")
    assert result["ok"] is True
    assert "mark_renewed" not in doc.rippleSpec.get("actions", {})
    assert capture["ops"][0]["op"] == "remove_action"


# ---------------------------------------------------------------------------
# Service layer — set_pocket_write_policy (owner-only, needs a backend)
# ---------------------------------------------------------------------------


async def test_set_write_policy_persists_allowlist(mongo_db):
    """Setting the policy on a pocket with a backend stores the allowlist
    and the executor/summary then carry it."""
    await pocket_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    result = await pocket_service.set_pocket_write_policy(
        "w1",
        "u1",
        "pocket-1",
        [{"method": "POST", "path_pattern": "/leases/*/renew"}],
    )
    assert result["allowed_writes"] == [{"method": "POST", "path_pattern": "/leases/*/renew"}]
    # The executor tuple now carries the same allowlist.
    creds = await pocket_service.get_pocket_backend_for_executor("w1", "pocket-1")
    assert creds is not None
    assert creds[4] == [{"method": "POST", "path_pattern": "/leases/*/renew"}]


async def test_set_write_policy_empty_list_revokes_all(mongo_db):
    """An empty list is valid and revokes every write (fail-closed)."""
    await pocket_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    await pocket_service.set_pocket_write_policy(
        "w1", "u1", "pocket-1", [{"method": "POST", "path_pattern": "/x"}]
    )
    result = await pocket_service.set_pocket_write_policy("w1", "u1", "pocket-1", [])
    assert result["allowed_writes"] == []


async def test_set_write_policy_rejects_when_no_backend(mongo_db):
    """A write policy with no backend to apply to is meaningless — the
    service rejects it rather than storing a dangling policy."""
    from pocketpaw_ee.cloud.shared.errors import ValidationError

    with pytest.raises(ValidationError):
        await pocket_service.set_pocket_write_policy(
            "w1", "u1", "no-backend-pocket", [{"method": "POST", "path_pattern": "/x"}]
        )
