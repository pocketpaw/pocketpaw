# tests/test_studio_flow_build.py
# Created: 2026-08-21 (feat/studio-real-backend).
#
# Tests for the ``build_studio_flow`` tool + SDK MCP handler that scaffold
# /studio Flow node graphs from a natural-language goal.
#
# What's asserted:
#   1. ``validate_flow_spec`` normalises + validates the node/edge graph and
#      echoes ``flow_id`` verbatim (so the loop/frontend know WHICH flow the
#      graph belongs to).
#   2. ``persist_flow_spec`` writes the graph to the flow-projects store the
#      moment the tool is called — the fix for "agent-built flows never reach
#      the backend". Workspace is resolved from the per-stream identity
#      ContextVar (the pocket-specialist seam); the record lands under the
#      SAME workspace the /studio flow-projects router reads.
#   3. A save miss (no flow_id, no workspace identity, EE store unavailable)
#      degrades to False — never raises — so the tool call still succeeds.
#
# ``persist_flow_spec`` imports ``pocketpaw_ee`` LAZILY inside the function
# (a save miss must never fail the tool call on an OSS install), so the
# EE-backed assertions below are guarded by ``importorskip`` — the pure
# validation tests always run.

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.tools.builtin.studio_flow_tool import (
    StudioFlowTool,
    dump_flow_spec,
    persist_flow_spec,
    validate_flow_spec,
)


def _valid_graph() -> tuple[list[dict], list[dict]]:
    nodes = [
        {"id": "m1", "type": "model", "position": {"x": 0, "y": 0}, "data": {}},
        {"id": "t1", "type": "text", "position": {"x": 320, "y": 0}, "data": {"text": "goal"}},
        {"id": "i1", "type": "image", "position": {"x": 640, "y": 0}, "data": {"prompt": "x"}},
        {"id": "o1", "type": "output", "position": {"x": 960, "y": 0}, "data": {}},
    ]
    edges = [
        {"source": "m1", "target": "t1"},
        {"source": "t1", "target": "i1"},
        {"source": "i1", "target": "o1"},
    ]
    return nodes, edges


class TestValidateFlowSpec:
    def test_echoes_flow_id(self) -> None:
        nodes, edges = _valid_graph()
        spec, err = validate_flow_spec(nodes, edges, goal="cinematic", flow_id="flow-42")
        assert err is None
        assert spec["flow_id"] == "flow-42"
        assert spec["goal"] == "cinematic"
        assert len(spec["nodes"]) == 4
        assert len(spec["edges"]) == 3

    def test_rejects_unknown_kind(self) -> None:
        nodes = [{"id": "x", "type": "warp_drive"}]
        spec, err = validate_flow_spec(nodes)
        assert spec is None
        assert "warp_drive" in err

    def test_rejects_dangling_edge(self) -> None:
        nodes = [{"id": "a", "type": "text"}]
        spec, err = validate_flow_spec(nodes, [{"source": "a", "target": "ghost"}])
        assert spec is None
        assert "ghost" in err

    def test_serializes_envelope(self) -> None:
        nodes, edges = _valid_graph()
        spec, _ = validate_flow_spec(nodes, edges, goal="g", flow_id="f1")
        text = dump_flow_spec(spec)
        assert '"studio_flow"' in text
        assert '"flow_id": "f1"' in text


# ── EE-backed persistence (skips on an OSS-only install) ────────────────────

ee = pytest.importorskip("pocketpaw_ee")


class TestPersistFlowSpec:
    def test_persists_to_flow_projects_store(self) -> None:
        nodes, edges = _valid_graph()
        spec, _ = validate_flow_spec(nodes, edges, goal="cinematic", flow_id="flow-42")
        saved: dict = {}

        def _fake_save(project_id, workspace_id, *, name, nodes, edges):
            saved["project_id"] = project_id
            saved["workspace_id"] = workspace_id
            saved["name"] = name
            saved["nodes"] = nodes
            saved["edges"] = edges

        with patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value="ws-1",
        ):
            with patch(
                "pocketpaw_ee.cloud.studio.service.save_flow_project",
                side_effect=_fake_save,
            ):
                ok = persist_flow_spec(spec)

        assert ok is True
        assert saved["project_id"] == "flow-42"
        assert saved["workspace_id"] == "ws-1"
        assert saved["name"] is None  # keep existing title; store falls back
        assert len(saved["nodes"]) == 4
        # The validator's edges carry no id — persist_flow_spec synthesizes one
        # onto the FlowEdge models it hands the store.
        assert len(saved["edges"]) == 3
        assert all(e.id for e in saved["edges"])

    def test_noop_without_flow_id(self) -> None:
        nodes, edges = _valid_graph()
        spec, _ = validate_flow_spec(nodes, edges)
        assert "flow_id" not in spec
        assert persist_flow_spec(spec) is False

    def test_noop_without_workspace_identity(self) -> None:
        nodes, edges = _valid_graph()
        spec, _ = validate_flow_spec(nodes, edges, flow_id="flow-42")
        with patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value=None,
        ):
            assert persist_flow_spec(spec) is False

    def test_store_unavailable_returns_false_not_raises(self) -> None:
        nodes, edges = _valid_graph()
        spec, _ = validate_flow_spec(nodes, edges, flow_id="flow-42")
        with patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value="ws-1",
        ):
            with patch(
                "pocketpaw_ee.cloud.studio.service.save_flow_project",
                side_effect=RuntimeError("store down"),
            ):
                assert persist_flow_spec(spec) is False


class TestStudioFlowTool:
    def test_execute_persists_and_returns_spec(self) -> None:
        import asyncio

        nodes, edges = _valid_graph()
        tool = StudioFlowTool()
        with patch(
            "pocketpaw.tools.builtin.studio_flow_tool.persist_flow_spec",
            return_value=True,
        ) as mock_persist:
            out = asyncio.run(tool.execute(nodes, edges, goal="cinematic", flow_id="flow-42"))
        assert '"studio_flow"' in out
        assert mock_persist.call_count == 1
        assert mock_persist.call_args.args[0]["flow_id"] == "flow-42"


# ── EE chat-path threading (flow_context rides the run the /studio surface uses) ──


class TestEEChatFlowContext:
    """The /studio surface chats over ``/cloud/chat/session/{id}/agent`` (EE),
    NOT the OSS ``/api/v1/chat`` — so ``flow_context`` must thread through the
    EE request schema → RunSpec → prompt, and the ``studio_flow`` SSE frame must
    be extracted from the tool result. Without this the agent never learns the
    ACTIVE FLOW ID and build_studio_flow omits it (→ nothing persists)."""

    def test_chat_request_accepts_flow_context(self) -> None:
        from pocketpaw_ee.cloud.chat.agent_schemas import CloudAgentChatRequest

        req = CloudAgentChatRequest(
            content="build a poster flow",
            flow_context={"flow_id": "proj_f1152a97", "project_name": "Flow 2"},
        )
        assert req.flow_context == {
            "flow_id": "proj_f1152a97",
            "project_name": "Flow 2",
        }

    def test_run_spec_carries_flow_context(self) -> None:
        from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

        spec = RunSpec(
            run_id="r1",
            workspace_id="ws",
            context_type="session",
            scope_id="s",
            session_key="k",
            group=None,
            user_id="u",
            agent_id="a",
            client_message_id="c",
            user_message_id="um",
            content="x",
            history=[],
            intent=None,
            flow_context={"flow_id": "proj_f1152a97", "project_name": "Flow 2"},
        )
        assert spec.flow_context["flow_id"] == "proj_f1152a97"

    def test_agent_router_forwards_flow_context_to_run_spec(self) -> None:
        import pocketpaw_ee.cloud.chat.agent_router as ar

        # The router builds RunSpec from body — flow_context must be threaded
        # so the executor can inject the ACTIVE FLOW ID into the agent's prompt.
        src = Path(ar.__file__).read_text()
        assert "flow_context=body.flow_context" in src

    def test_studio_flow_payload_extracts_spec_and_id(self) -> None:
        import json

        from pocketpaw_ee.cloud.chat.runs.run_core import _studio_flow_payload

        spec = {
            "goal": "poster",
            "nodes": [{"id": "a", "type": "text", "position": {"x": 0, "y": 0}, "data": {}}],
            "edges": [],
            "flow_id": "proj_f1152a97",
        }
        payload = _studio_flow_payload(json.dumps({"studio_flow": spec}))
        assert payload is not None
        assert payload["spec"]["flow_id"] == "proj_f1152a97"
        assert payload["flow_id"] == "proj_f1152a97"
        assert payload["spec"]["goal"] == "poster"

    def test_studio_flow_payload_passes_through_other_tools(self) -> None:
        from pocketpaw_ee.cloud.chat.runs.run_core import _studio_flow_payload

        assert _studio_flow_payload('{"bash": "ls"}') is None
        assert _studio_flow_payload("plain text") is None


class TestSDKToolResultExtraction:
    """The MCP ``build_studio_flow`` result rides the SDK's ``user`` message as
    a ``tool_result`` block whose ``content`` is a LIST OF DICTS
    (``[{"type": "text", "text": ...}]``) — NOT a ``ServerToolResultBlock``.
    The old UserMessage branch joined ``getattr(b, "text")`` (dataclass-only),
    which is ``""`` for dicts, so the tool_result was SILENTLY DROPPED and the
    ``studio_flow`` SSE frame never reached the frontend. These pin the
    unwrap + detection so a build can never vanish again."""

    def _raw_user_tool_result(self, payload: str) -> dict:
        return {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "call_00_SfKqIm4Q0HYfqcZpT5rk4432",
                        "type": "tool_result",
                        "content": [{"type": "text", "text": payload}],
                    }
                ],
            },
        }

    def test_mcp_result_text_unwraps_cli_user_tool_result_list(self) -> None:
        import json

        from pocketpaw.agents.claude_sdk import _mcp_result_text

        spec = json.dumps({"studio_flow": {"goal": "g", "nodes": [], "edges": []}})
        text = _mcp_result_text([{"type": "text", "text": spec}])
        assert '"studio_flow"' in text
        assert '"goal": "g"' in text

    def test_sdk_parses_mcp_tool_result_as_tool_result_block(self) -> None:
        import json

        from claude_agent_sdk._internal.message_parser import parse_message
        from claude_agent_sdk.types import ToolResultBlock

        from pocketpaw.agents.claude_sdk import _mcp_result_text

        payload = json.dumps(
            {
                "studio_flow": {
                    "goal": "g",
                    "nodes": [],
                    "edges": [],
                    "flow_id": "proj_f1152a97",
                }
            }
        )
        msg = parse_message(self._raw_user_tool_result(payload))
        assert msg is not None
        blk = msg.content[0]
        assert isinstance(blk, ToolResultBlock)
        extracted = _mcp_result_text(getattr(blk, "content", None))
        assert '"studio_flow"' in extracted

    def test_extracted_payload_reaches_studio_flow_frame(self) -> None:
        import json

        from pocketpaw_ee.cloud.chat.runs.run_core import _studio_flow_payload

        from pocketpaw.agents.claude_sdk import _mcp_result_text

        payload = json.dumps(
            {
                "studio_flow": {
                    "goal": "g",
                    "nodes": [{"id": "m1", "type": "model"}],
                    "edges": [],
                    "flow_id": "proj_f1152a97",
                }
            }
        )
        text = _mcp_result_text([{"type": "text", "text": payload}])
        out = _studio_flow_payload(text)
        assert out is not None
        assert out["flow_id"] == "proj_f1152a97"
        assert out["spec"]["flow_id"] == "proj_f1152a97"
        assert out["spec"]["nodes"][0]["id"] == "m1"
