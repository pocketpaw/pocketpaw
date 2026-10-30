# test_pocket_sources_ops.py — Tests for the edit-specialist `sources` ops.
# Created: 2026-05-21 (RFC 04 alpha follow-up) — closes the edit-path gap
#   where the pocket EDIT specialist could not author a top-level
#   `rippleSpec.sources` block on an existing pocket (create worked, edit
#   did not). Covers the pure `sources_ops` helpers, the service-layer
#   `agent_set_source` / `agent_remove_source` functions (happy path,
#   invalid-binding rejection, persistence, emitted mutation), and the
#   new `set_source` / `remove_source` edit tool factories.

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import agent_context, sources_ops
from pocketpaw_ee.cloud.pockets import service as pocket_service

# ---------------------------------------------------------------------------
# Fake doc + patch helper (mirrors test_pocket_state_ops.py)
# ---------------------------------------------------------------------------


class _FakeDoc:
    """Minimum surface to stand in for a ``_PocketDoc`` in these tests.

    Mutations operate on ``self.rippleSpec`` in place, like a real Beanie
    doc. ``save()`` is an async no-op; calls are counted via ``saves``.
    """

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
    """A persisted pocket with a UI tree + seeded state but NO sources."""
    return _FakeDoc(
        "507f1f77bcf86cd799439011",
        {
            "version": "1.0",
            "state": {"prs": []},
            "ui": {"id": "n_root0000", "type": "flex", "children": []},
        },
    )


def _patches(doc: _FakeDoc):
    """Patch the doc fetch + emit/push seams + identity ContextVars.

    Returns ``(ExitStack, push_calls)`` — use as ``with ctx: ...``.
    """
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
    "method": "GET",
    "path": "/pulls?state=open",
    "bind": "state.prs",
    "refresh": ["pocket_open", "manual"],
}


# ---------------------------------------------------------------------------
# Step 1 — the red test: the edit specialist can add a sources block.
# ---------------------------------------------------------------------------


async def test_agent_set_source_adds_sources_block_to_existing_pocket(fake_doc):
    """The core regression: an EXISTING persisted pocket with no
    ``rippleSpec.sources`` gains a binding when the edit specialist calls
    ``agent_set_source``. This is the gap RFC 04 alpha left open."""
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_source(
            fake_doc.id, source_key="prs", binding=dict(_VALID_BINDING)
        )
    assert err is None
    assert result is not None
    assert fake_doc.rippleSpec["sources"]["prs"]["path"] == "/pulls?state=open"
    assert fake_doc.rippleSpec["sources"]["prs"]["bind"] == "state.prs"
    assert fake_doc.saves == 1


# ---------------------------------------------------------------------------
# Pure ops — sources_ops.set_source / remove_source
# ---------------------------------------------------------------------------


def test_set_source_creates_sources_dict_when_absent():
    spec: dict[str, Any] = {"ui": {}, "state": {}}
    out = sources_ops.set_source(spec, "prs", dict(_VALID_BINDING))
    assert out is spec
    assert spec["sources"]["prs"] == _VALID_BINDING


def test_set_source_adds_alongside_existing_sources():
    spec = {"sources": {"a": dict(_VALID_BINDING)}}
    sources_ops.set_source(spec, "b", dict(_VALID_BINDING))
    assert set(spec["sources"]) == {"a", "b"}


def test_set_source_overwrites_existing_key():
    spec = {"sources": {"prs": {"path": "/old", "bind": "state.prs"}}}
    sources_ops.set_source(spec, "prs", dict(_VALID_BINDING))
    assert spec["sources"]["prs"]["path"] == "/pulls?state=open"


def test_remove_source_drops_the_key():
    spec = {"sources": {"prs": dict(_VALID_BINDING), "issues": dict(_VALID_BINDING)}}
    sources_ops.remove_source(spec, "prs")
    assert "prs" not in spec["sources"]
    assert "issues" in spec["sources"]


def test_remove_source_is_a_noop_when_key_absent():
    spec = {"sources": {"prs": dict(_VALID_BINDING)}}
    out = sources_ops.remove_source(spec, "nope")
    assert out is spec
    assert "prs" in spec["sources"]


def test_remove_source_is_a_noop_when_sources_absent():
    spec: dict[str, Any] = {"ui": {}}
    out = sources_ops.remove_source(spec, "prs")
    assert out is spec
    assert "sources" not in spec


# ---------------------------------------------------------------------------
# Service layer — agent_set_source
# ---------------------------------------------------------------------------


async def test_agent_set_source_returns_view_with_binding(fake_doc):
    """The service layer persists + returns the agent view; the
    pocket_mutation push happens in the agent_context wrapper (covered
    separately below)."""
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_source(
            fake_doc.id, source_key="prs", binding=dict(_VALID_BINDING)
        )
    assert err is None
    assert result is not None
    assert result["source_key"] == "prs"
    assert result["binding"]["path"] == "/pulls?state=open"
    assert result["pocket"]["rippleSpec"]["sources"]["prs"]["bind"] == "state.prs"


async def test_agent_set_source_rejects_invalid_binding(fake_doc):
    """A binding missing required fields (``path``/``bind``) is rejected
    via the ``SourceBinding`` model before any write happens."""
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_source(
            fake_doc.id, source_key="prs", binding={"method": "GET"}
        )
    assert result is None
    assert err is not None
    assert "binding" in err.lower()
    assert "sources" not in fake_doc.rippleSpec
    assert fake_doc.saves == 0
    assert not push_calls


async def test_agent_set_source_rejects_write_verb(fake_doc):
    """``method`` is a Literal["GET"] — a POST binding is rejected."""
    ctx, _ = _patches(fake_doc)
    bad = dict(_VALID_BINDING, method="POST")
    with ctx:
        result, err = await pocket_service.agent_set_source(
            fake_doc.id, source_key="prs", binding=bad
        )
    assert result is None
    assert err is not None


async def test_agent_set_source_requires_a_key(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_set_source(
            fake_doc.id, source_key="", binding=dict(_VALID_BINDING)
        )
    assert result is None
    assert err is not None
    assert "source_key" in err


async def test_agent_set_source_persists_defaults_for_omitted_refresh(fake_doc):
    """``refresh`` defaults to ``["pocket_open"]`` via SourceBinding."""
    ctx, _ = _patches(fake_doc)
    binding = {"method": "GET", "path": "/pulls", "bind": "state.prs"}
    with ctx:
        result, err = await pocket_service.agent_set_source(
            fake_doc.id, source_key="prs", binding=binding
        )
    assert err is None
    assert fake_doc.rippleSpec["sources"]["prs"]["refresh"] == ["pocket_open"]


# ---------------------------------------------------------------------------
# Service layer — agent_remove_source
# ---------------------------------------------------------------------------


async def test_agent_remove_source_drops_the_binding():
    doc = _FakeDoc(
        "507f1f77bcf86cd799439022",
        {
            "version": "1.0",
            "sources": {"prs": dict(_VALID_BINDING)},
            "state": {"prs": []},
            "ui": {"id": "n_root0000", "type": "flex"},
        },
    )
    ctx, _ = _patches(doc)
    with ctx:
        result, err = await pocket_service.agent_remove_source(doc.id, source_key="prs")
    assert err is None
    assert result is not None
    assert "prs" not in doc.rippleSpec.get("sources", {})
    assert doc.saves == 1


async def test_agent_remove_source_noop_when_absent(fake_doc):
    """Removing a source that was never declared still succeeds (idempotent)."""
    ctx, _ = _patches(fake_doc)
    with ctx:
        result, err = await pocket_service.agent_remove_source(fake_doc.id, source_key="ghost")
    assert err is None
    assert result is not None
    assert fake_doc.saves == 1


# ---------------------------------------------------------------------------
# agent_context wrappers
# ---------------------------------------------------------------------------


async def test_set_source_for_agent_returns_ok_shape(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_source_for_agent(fake_doc.id, "prs", dict(_VALID_BINDING))
    assert result["ok"] is True
    assert fake_doc.rippleSpec["sources"]["prs"]["bind"] == "state.prs"
    assert push_calls[0]["action"] == "replace"


async def test_set_source_for_agent_surfaces_error(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_source_for_agent(fake_doc.id, "prs", {"method": "GET"})
    assert result["ok"] is False
    assert "error" in result


async def test_remove_source_for_agent_returns_ok_shape():
    doc = _FakeDoc(
        "507f1f77bcf86cd799439033",
        {"version": "1.0", "sources": {"prs": dict(_VALID_BINDING)}, "ui": {}},
    )
    ctx, _ = _patches(doc)
    with ctx:
        result = await agent_context.remove_source_for_agent(doc.id, "prs")
    assert result["ok"] is True
    assert "prs" not in doc.rippleSpec.get("sources", {})


# ---------------------------------------------------------------------------
# Edit tool factories
# ---------------------------------------------------------------------------


async def test_set_source_tool_is_registered_in_edit_toolset():
    from pocketpaw_ee.agent.pocket_specialist.tools import make_edit_pocket_tools

    tools = make_edit_pocket_tools(pocket_id="507f1f77bcf86cd799439011")
    names = {t.name for t in tools}
    assert "set_source" in names
    assert "remove_source" in names


async def test_set_source_tool_runs_and_captures_op(fake_doc):
    from pocketpaw_ee.agent.pocket_specialist.tools import make_set_source_tool

    capture: dict[str, Any] = {}
    tool = make_set_source_tool(pocket_id=fake_doc.id, capture=capture)
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await tool.coroutine(
            source_key="prs",
            path="/pulls?state=open",
            bind="state.prs",
            method="GET",
            refresh=["pocket_open", "manual"],
        )
    assert result["ok"] is True
    assert fake_doc.rippleSpec["sources"]["prs"]["path"] == "/pulls?state=open"
    assert capture["ops"][0]["op"] == "set_source"


async def test_remove_source_tool_runs_and_captures_op():
    from pocketpaw_ee.agent.pocket_specialist.tools import make_remove_source_tool

    doc = _FakeDoc(
        "507f1f77bcf86cd799439044",
        {"version": "1.0", "sources": {"prs": dict(_VALID_BINDING)}, "ui": {}},
    )
    capture: dict[str, Any] = {}
    tool = make_remove_source_tool(pocket_id=doc.id, capture=capture)
    ctx, _ = _patches(doc)
    with ctx:
        result = await tool.coroutine(source_key="prs")
    assert result["ok"] is True
    assert "prs" not in doc.rippleSpec.get("sources", {})
    assert capture["ops"][0]["op"] == "remove_source"
