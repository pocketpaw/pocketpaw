# Tests for A2A Protocol — Phase 1: Server implementation.
# Created: 2026-03-07
#
# Covers:
#  - Agent Card structure and required fields
#  - POST /a2a/tasks/send (non-streaming, mocked AgentLoop)
#  - GET  /a2a/tasks/{task_id} polling
#  - POST /a2a/tasks/{task_id}/cancel
#  - POST /a2a/tasks/send/stream (SSE format validation)
#  - Model validation: TaskState enum, TextPart, FilePart, DataPart, Artifact
#  - JSON-RPC 2.0 dispatcher and POST /a2a endpoint
#  - State transition validation
#  - Streaming artifact events

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.a2a.errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TASK_NOT_CANCELABLE,
    TASK_NOT_FOUND,
    UNSUPPORTED_OPERATION,
    JSONRPCError,
    json_rpc_error_response,
    json_rpc_success_response,
)
from pocketpaw.a2a.jsonrpc import A2ADispatcher
from pocketpaw.a2a.models import (
    A2AMessage,
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    JSONRPCErrorData,
    JSONRPCRequest,
    JSONRPCResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    validate_transition,
)
from pocketpaw.a2a.server import (
    _A2ASessionBridge,
    _cancel_events,
    _check_a2a_enabled,
    _extract_message_text,
    _format_sse,
    _tasks,
    jsonrpc_router,
    tasks_router,
    well_known_router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app():
    """Minimal FastAPI app with all A2A routers mounted."""
    from fastapi import Request

    app = FastAPI()
    app.dependency_overrides[_check_a2a_enabled] = lambda: None

    @app.middleware("http")
    async def mock_auth_middleware(request: Request, call_next):
        class MockAPIKey:
            scopes = ["chat", "admin"]

        request.state.api_key = MockAPIKey()
        return await call_next(request)

    app.include_router(well_known_router)
    app.include_router(tasks_router)
    app.include_router(jsonrpc_router)
    return app


@pytest_asyncio.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    headers = {"Content-Type": "application/json"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        yield client


@pytest.fixture(autouse=True)
def clear_task_store():
    """Isolate the in-memory task store between tests."""
    _tasks.clear()
    _cancel_events.clear()
    yield
    _tasks.clear()
    _cancel_events.clear()


def _make_send_params(text: str = "Hello PocketPaw", task_id: str = "test-task-001"):
    return TaskSendParams(
        id=task_id,
        message=A2AMessage(role="user", parts=[TextPart(text=text)]),
    )


# ---------------------------------------------------------------------------
# Tests: Pydantic Models
# ---------------------------------------------------------------------------


class TestModels:
    def test_task_state_values(self):
        assert TaskState.SUBMITTED == "submitted"
        assert TaskState.WORKING == "working"
        assert TaskState.COMPLETED == "completed"
        assert TaskState.FAILED == "failed"
        assert TaskState.CANCELED == "canceled"

    def test_new_task_states(self):
        assert TaskState.REJECTED == "rejected"
        assert TaskState.AUTH_REQUIRED == "auth-required"

    def test_text_part_defaults(self):
        part = TextPart(text="hello")
        assert part.type == "text"
        assert part.text == "hello"

    def test_file_part(self):
        part = FilePart(name="test.txt", media_type="text/plain", uri="file:///test.txt")
        assert part.type == "file"
        assert part.name == "test.txt"
        assert part.media_type == "text/plain"

    def test_file_part_with_bytes(self):
        part = FilePart(name="img.png", media_type="image/png", bytes_data="aGVsbG8=")
        assert part.bytes_data == "aGVsbG8="

    def test_data_part(self):
        part = DataPart(data={"key": "value", "count": 42})
        assert part.type == "data"
        assert part.data["key"] == "value"

    def test_part_discriminator(self):
        """Parts should serialize/deserialize with type discriminator."""
        msg = A2AMessage(
            role="agent",
            parts=[
                TextPart(text="hello"),
                DataPart(data={"x": 1}),
            ],
        )
        data = msg.model_dump()
        assert data["parts"][0]["type"] == "text"
        assert data["parts"][1]["type"] == "data"

    def test_a2a_message_serialization(self):
        msg = A2AMessage(role="agent", parts=[TextPart(text="hi")])
        data = msg.model_dump()
        assert data["role"] == "agent"
        assert data["parts"][0]["text"] == "hi"

    def test_a2a_message_has_id(self):
        msg = A2AMessage(role="user", parts=[TextPart(text="test")])
        assert msg.message_id  # auto-generated

    def test_a2a_message_metadata(self):
        msg = A2AMessage(role="user", parts=[TextPart(text="hi")], metadata={"source": "test"})
        assert msg.metadata["source"] == "test"

    def test_agent_card_defaults(self):
        card = AgentCard(
            name="Test", description="Desc", url="http://localhost:8888", version="1.0"
        )
        assert card.capabilities.streaming is True
        assert card.default_input_modes == ["text/plain"]
        assert card.default_output_modes == ["text/plain"]

    def test_agent_card_provider(self):
        card = AgentCard(
            name="Test",
            description="Desc",
            url="http://localhost:8888",
            version="1.0",
            provider={"organization": "TestOrg", "url": "https://test.org"},
        )
        assert card.provider["organization"] == "TestOrg"

    def test_agent_card_supported_interfaces(self):
        card = AgentCard(
            name="Test",
            description="Desc",
            url="http://localhost:8888",
            version="1.0",
            supported_interfaces=[
                {"url": "http://localhost/a2a", "protocol_binding": "jsonrpc-over-https"}
            ],
        )
        assert len(card.supported_interfaces) == 1

    def test_agent_card_has_protocol_version(self):
        card = AgentCard(name="Test", description="Test", url="http://localhost", version="1.0")
        dumped = card.model_dump(mode="json")
        assert "protocol_version" in dumped
        assert dumped["protocol_version"] == "0.2.5"

    def test_agent_skill(self):
        skill = AgentSkill(id="test", name="Test Skill", description="A test skill")
        assert skill.id == "test"
        assert skill.input_modes == ["text/plain"]
        assert skill.output_modes == ["text/plain"]

    def test_agent_skill_with_tags(self):
        skill = AgentSkill(
            id="web", name="Web Search", description="Search the web", tags=["search", "web"]
        )
        assert "search" in skill.tags

    def test_task_send_params_auto_id(self):
        params = TaskSendParams(message=A2AMessage(role="user", parts=[TextPart(text="test")]))
        assert params.id  # auto-generated

    def test_task_send_params_context_id(self):
        params = TaskSendParams(
            context_id="ctx-123",
            message=A2AMessage(role="user", parts=[TextPart(text="test")]),
        )
        assert params.context_id == "ctx-123"

    def test_task_status_default_timestamp(self):
        status = TaskStatus(state=TaskState.SUBMITTED)
        assert status.timestamp is not None

    def test_artifact_defaults(self):
        artifact = Artifact(parts=[TextPart(text="result")])
        assert artifact.artifact_id  # auto-generated
        assert artifact.name is None
        assert len(artifact.parts) == 1

    def test_artifact_with_metadata(self):
        artifact = Artifact(
            name="output",
            description="The result",
            parts=[TextPart(text="data")],
            metadata={"format": "markdown"},
        )
        assert artifact.metadata["format"] == "markdown"

    def test_task_has_artifacts(self):
        task = Task(
            id="t1",
            status=TaskStatus(state=TaskState.COMPLETED),
            artifacts=[Artifact(parts=[TextPart(text="result")])],
        )
        assert len(task.artifacts) == 1

    def test_task_context_id(self):
        task = Task(
            id="t1",
            context_id="ctx-abc",
            status=TaskStatus(state=TaskState.SUBMITTED),
        )
        assert task.context_id == "ctx-abc"


# ---------------------------------------------------------------------------
# Tests: State transition validation
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_submitted_to_working(self):
        assert validate_transition(TaskState.SUBMITTED, TaskState.WORKING) is True

    def test_submitted_to_rejected(self):
        assert validate_transition(TaskState.SUBMITTED, TaskState.REJECTED) is True

    def test_working_to_completed(self):
        assert validate_transition(TaskState.WORKING, TaskState.COMPLETED) is True

    def test_working_to_failed(self):
        assert validate_transition(TaskState.WORKING, TaskState.FAILED) is True

    def test_working_to_canceled(self):
        assert validate_transition(TaskState.WORKING, TaskState.CANCELED) is True

    def test_working_to_input_required(self):
        assert validate_transition(TaskState.WORKING, TaskState.INPUT_REQUIRED) is True

    def test_completed_is_terminal(self):
        assert validate_transition(TaskState.COMPLETED, TaskState.WORKING) is False
        assert validate_transition(TaskState.COMPLETED, TaskState.FAILED) is False

    def test_failed_is_terminal(self):
        assert validate_transition(TaskState.FAILED, TaskState.WORKING) is False

    def test_canceled_is_terminal(self):
        assert validate_transition(TaskState.CANCELED, TaskState.WORKING) is False

    def test_rejected_is_terminal(self):
        assert validate_transition(TaskState.REJECTED, TaskState.WORKING) is False

    def test_input_required_to_working(self):
        assert validate_transition(TaskState.INPUT_REQUIRED, TaskState.WORKING) is True

    def test_invalid_transition(self):
        assert validate_transition(TaskState.SUBMITTED, TaskState.COMPLETED) is False


# ---------------------------------------------------------------------------
# Tests: JSON-RPC envelope models
# ---------------------------------------------------------------------------


class TestJSONRPCModels:
    def test_jsonrpc_request(self):
        req = JSONRPCRequest(method="message/send", params={"message": {}})
        assert req.jsonrpc == "2.0"
        assert req.method == "message/send"

    def test_jsonrpc_request_with_id(self):
        req = JSONRPCRequest(id=42, method="tasks/get")
        assert req.id == 42

    def test_jsonrpc_response_success(self):
        resp = JSONRPCResponse(id=1, result={"status": "ok"})
        assert resp.result == {"status": "ok"}
        assert resp.error is None

    def test_jsonrpc_response_error(self):
        resp = JSONRPCResponse(id=1, error=JSONRPCErrorData(code=-32600, message="Invalid request"))
        assert resp.error.code == -32600

    def test_jsonrpc_error_data(self):
        err = JSONRPCErrorData(code=-32700, message="Parse error", data="details")
        assert err.code == -32700
        assert err.data == "details"


# ---------------------------------------------------------------------------
# Tests: Streaming event models
# ---------------------------------------------------------------------------


class TestStreamingEvents:
    def test_task_status_update_event(self):
        evt = TaskStatusUpdateEvent(
            task_id="t1",
            status=TaskStatus(state=TaskState.WORKING),
            final=False,
        )
        assert evt.task_id == "t1"
        assert evt.final is False

    def test_task_status_update_with_context(self):
        evt = TaskStatusUpdateEvent(
            task_id="t1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.COMPLETED),
            final=True,
        )
        assert evt.context_id == "ctx-1"
        assert evt.final is True

    def test_task_artifact_update_event(self):
        artifact = Artifact(parts=[TextPart(text="chunk")])
        evt = TaskArtifactUpdateEvent(
            task_id="t1",
            artifact=artifact,
            append=True,
            last_chunk=False,
        )
        assert evt.append is True
        assert evt.last_chunk is False

    def test_task_artifact_final_chunk(self):
        artifact = Artifact(parts=[TextPart(text="full response")])
        evt = TaskArtifactUpdateEvent(
            task_id="t1",
            artifact=artifact,
            append=False,
            last_chunk=True,
        )
        assert evt.last_chunk is True


# ---------------------------------------------------------------------------
# Tests: Error helpers
# ---------------------------------------------------------------------------


class TestErrors:
    def test_jsonrpc_error_exception(self):
        err = JSONRPCError(-32600, "Invalid request")
        assert err.code == -32600
        assert err.message == "Invalid request"

    def test_jsonrpc_error_to_response(self):
        err = JSONRPCError(-32600, "Invalid request", data={"field": "method"})
        resp = err.to_response(42)
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 42
        assert resp["error"]["code"] == -32600
        assert resp["error"]["data"]["field"] == "method"

    def test_json_rpc_error_response_helper(self):
        resp = json_rpc_error_response(1, PARSE_ERROR, "Parse error")
        assert resp["error"]["code"] == PARSE_ERROR

    def test_json_rpc_success_response_helper(self):
        resp = json_rpc_success_response(1, {"status": "ok"})
        assert resp["result"]["status"] == "ok"
        assert "error" not in resp

    def test_error_codes(self):
        assert PARSE_ERROR == -32700
        assert INVALID_REQUEST == -32600
        assert METHOD_NOT_FOUND == -32601
        assert INVALID_PARAMS == -32602
        assert INTERNAL_ERROR == -32603
        assert TASK_NOT_FOUND == -32001
        assert TASK_NOT_CANCELABLE == -32002
        assert UNSUPPORTED_OPERATION == -32004


# ---------------------------------------------------------------------------
# Tests: JSON-RPC Dispatcher
# ---------------------------------------------------------------------------


class TestA2ADispatcher:
    @pytest.mark.asyncio
    async def test_dispatch_parse_error(self):
        dispatcher = A2ADispatcher()
        result = await dispatcher.dispatch(b"not json{{{")
        assert result["error"]["code"] == PARSE_ERROR

    @pytest.mark.asyncio
    async def test_dispatch_invalid_request_missing_jsonrpc(self):
        dispatcher = A2ADispatcher()
        result = await dispatcher.dispatch(json.dumps({"method": "test"}).encode())
        assert result["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_dispatch_invalid_request_missing_method(self):
        dispatcher = A2ADispatcher()
        result = await dispatcher.dispatch(json.dumps({"jsonrpc": "2.0"}).encode())
        assert result["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_dispatch_method_not_found(self):
        dispatcher = A2ADispatcher()
        result = await dispatcher.dispatch(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "unknown/method"}).encode()
        )
        assert result["error"]["code"] == METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_dispatch_valid_call(self):
        dispatcher = A2ADispatcher()

        async def echo(params, req_id):
            return {"echo": params}

        dispatcher.register("test/echo", echo)
        result = await dispatcher.dispatch(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "test/echo", "params": {"msg": "hi"}}
            ).encode()
        )
        assert result["result"]["echo"]["msg"] == "hi"

    @pytest.mark.asyncio
    async def test_dispatch_batch_request(self):
        dispatcher = A2ADispatcher()

        async def echo(params, req_id):
            return {"echo": params}

        dispatcher.register("test/echo", echo)
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "test/echo", "params": {"n": 1}},
            {"jsonrpc": "2.0", "id": 2, "method": "test/echo", "params": {"n": 2}},
        ]
        result = await dispatcher.dispatch(json.dumps(batch).encode())
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["result"]["echo"]["n"] == 1
        assert result[1]["result"]["echo"]["n"] == 2

    @pytest.mark.asyncio
    async def test_dispatch_empty_batch(self):
        dispatcher = A2ADispatcher()
        result = await dispatcher.dispatch(json.dumps([]).encode())
        assert result["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_dispatch_stream(self):
        dispatcher = A2ADispatcher()

        async def stream_handler(params, req_id):
            yield {"chunk": 1}
            yield {"chunk": 2}

        dispatcher.register_stream("test/stream", stream_handler)
        events = []
        async for event in dispatcher.dispatch_stream(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test/stream"}).encode()
        ):
            events.append(event)
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_dispatch_invalid_params_type(self):
        dispatcher = A2ADispatcher()
        result = await dispatcher.dispatch(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test", "params": [1, 2]}).encode()
        )
        assert result["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Tests: GET /.well-known/agent.json
# ---------------------------------------------------------------------------


class TestAgentCard:
    @pytest.mark.asyncio
    async def test_agent_card_returns_200(self, client):
        resp = await client.get("/.well-known/agent.json")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_agent_card_alias_returns_200(self, client):
        resp = await client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_agent_card_content_type(self, client):
        resp = await client.get("/.well-known/agent.json")
        assert "application/json" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_agent_card_required_fields(self, client):
        resp = await client.get("/.well-known/agent.json")
        data = resp.json()
        assert "name" in data
        assert "description" in data
        assert "url" in data
        assert "version" in data
        assert "capabilities" in data
        assert "skills" in data

    @pytest.mark.asyncio
    async def test_agent_card_name(self, client):
        resp = await client.get("/.well-known/agent.json")
        assert resp.json()["name"] == "PocketPaw"

    @pytest.mark.asyncio
    async def test_agent_card_capabilities_streaming(self, client):
        resp = await client.get("/.well-known/agent.json")
        caps = resp.json()["capabilities"]
        assert caps["streaming"] is True

    @pytest.mark.asyncio
    async def test_agent_card_skills_list(self, client):
        resp = await client.get("/.well-known/agent.json")
        skills = resp.json()["skills"]
        assert isinstance(skills, list)
        assert len(skills) >= 1
        skill = skills[0]
        assert "id" in skill
        assert "name" in skill
        assert "description" in skill

    @pytest.mark.asyncio
    async def test_agent_card_has_provider(self, client):
        resp = await client.get("/.well-known/agent.json")
        data = resp.json()
        assert "provider" in data
        assert data["provider"]["organization"] == "PocketPaw"

    @pytest.mark.asyncio
    async def test_agent_card_has_supported_interfaces(self, client):
        resp = await client.get("/.well-known/agent.json")
        data = resp.json()
        assert "supported_interfaces" in data
        assert len(data["supported_interfaces"]) >= 1
        iface = data["supported_interfaces"][0]
        assert "protocol_binding" in iface

    @pytest.mark.asyncio
    async def test_agent_card_mime_types(self, client):
        resp = await client.get("/.well-known/agent.json")
        data = resp.json()
        assert data["default_input_modes"] == ["text/plain"]
        assert data["default_output_modes"] == ["text/plain"]


# ---------------------------------------------------------------------------
# Tests: POST /a2a/tasks/send (non-streaming)
# ---------------------------------------------------------------------------


class TestTasksSend:
    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_send_returns_completed_task(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:test-task-001"

        async def _load():
            await q.put({"type": "chunk", "content": "Here is the answer."})
            await q.put({"type": "stream_end"})

        await _load()

        params = _make_send_params()
        resp = await client.post("/a2a/tasks/send", content=params.model_dump_json())
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "test-task-001"
        assert data["status"]["state"] == TaskState.COMPLETED
        assert "Here is the answer." in data["status"]["message"]["parts"][0]["text"]

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_send_includes_artifact(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:artifact-test"

        async def _load():
            await q.put({"type": "chunk", "content": "Result data."})
            await q.put({"type": "stream_end"})

        await _load()

        params = _make_send_params(task_id="artifact-test")
        resp = await client.post("/a2a/tasks/send", content=params.model_dump_json())
        data = resp.json()
        assert len(data["artifacts"]) == 1
        assert data["artifacts"][0]["name"] == "response"
        assert data["artifacts"][0]["parts"][0]["text"] == "Result data."

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_send_failed_on_error_event(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:test-task-001"

        async def _load():
            await q.put({"type": "error", "message": "Something went wrong"})

        await _load()

        params = _make_send_params()
        resp = await client.post("/a2a/tasks/send", content=params.model_dump_json())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]["state"] == TaskState.FAILED

    @pytest.mark.asyncio
    async def test_send_invalid_body_returns_422(self, client):
        resp = await client.post("/a2a/tasks/send", json={"bad": "body"})
        assert resp.status_code == 422

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_send_stores_task_history(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:test-task-001"

        async def _load():
            await q.put({"type": "chunk", "content": "Done."})
            await q.put({"type": "stream_end"})

        await _load()

        params = _make_send_params()
        resp = await client.post("/a2a/tasks/send", content=params.model_dump_json())
        data = resp.json()
        assert len(data["history"]) >= 2
        assert data["history"][0]["role"] == "user"
        assert data["history"][-1]["role"] == "agent"


# ---------------------------------------------------------------------------
# Tests: GET /a2a/tasks/{task_id}
# ---------------------------------------------------------------------------


class TestTasksGet:
    @pytest.mark.asyncio
    async def test_get_task_not_found(self, client):
        resp = await client.get("/a2a/tasks/nonexistent-id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_task_found(self, client):
        task = Task(
            id="known-task",
            status=TaskStatus(state=TaskState.WORKING),
        )
        _tasks["known-task"] = task

        resp = await client.get("/a2a/tasks/known-task")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "known-task"
        assert data["status"]["state"] == TaskState.WORKING


# ---------------------------------------------------------------------------
# Tests: POST /a2a/tasks/{task_id}/cancel
# ---------------------------------------------------------------------------


class TestTasksCancel:
    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, client):
        resp = await client.post("/a2a/tasks/ghost-task/cancel")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cancel_sets_state(self, client):
        task = Task(
            id="cancel-me",
            status=TaskStatus(state=TaskState.WORKING),
        )
        _tasks["cancel-me"] = task
        cancel_evt = asyncio.Event()
        _cancel_events["cancel-me"] = cancel_evt

        resp = await client.post("/a2a/tasks/cancel-me/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "canceled"
        assert _tasks["cancel-me"].status.state == TaskState.CANCELED
        assert cancel_evt.is_set()

    @pytest.mark.asyncio
    async def test_cancel_terminal_state_rejected(self, client):
        """Canceling a completed task should return 409."""
        task = Task(
            id="done-task",
            status=TaskStatus(state=TaskState.COMPLETED),
        )
        _tasks["done-task"] = task

        resp = await client.post("/a2a/tasks/done-task/cancel")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_failed_task_rejected(self, client):
        """Canceling a failed task should return 409."""
        task = Task(
            id="fail-task",
            status=TaskStatus(state=TaskState.FAILED),
        )
        _tasks["fail-task"] = task

        resp = await client.post("/a2a/tasks/fail-task/cancel")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests: POST /a2a/tasks/send/stream (SSE)
# ---------------------------------------------------------------------------


class TestTasksSendStream:
    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_stream_returns_sse_content_type(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:stream-task"

        async def _load():
            await q.put({"type": "chunk", "content": "Streaming..."})
            await q.put({"type": "stream_end"})

        await _load()
        params = _make_send_params(task_id="stream-task")

        async with client.stream(
            "POST", "/a2a/tasks/send/stream", content=params.model_dump_json()
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_stream_sse_events_valid_format(self, mock_bridge_cls, mock_dispatch, client):
        """Each SSE event must follow: event: <type>\\ndata: <json>\\n\\n"""
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:sse-val"

        async def _load():
            await q.put({"type": "chunk", "content": "partial answer"})
            await q.put({"type": "stream_end"})

        await _load()
        params = _make_send_params(task_id="sse-val")
        async with client.stream(
            "POST", "/a2a/tasks/send/stream", content=params.model_dump_json()
        ) as resp:
            raw = await resp.aread()
            raw = raw.decode()

        events = [e.strip() for e in raw.split("\n\n") if e.strip()]
        assert len(events) >= 2

        for block in events:
            lines = block.split("\n")
            event_line = next((line for line in lines if line.startswith("event:")), None)
            data_line = next((line for line in lines if line.startswith("data:")), None)
            assert event_line is not None, f"Missing 'event:' in: {block!r}"
            assert data_line is not None, f"Missing 'data:' in: {block!r}"
            data_str = data_line.split(":", 1)[1].strip()
            parsed = json.loads(data_str)
            # Every SSE data payload should be a valid dict
            assert isinstance(parsed, dict)

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_stream_final_event_has_completed_state(
        self, mock_bridge_cls, mock_dispatch, client
    ):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:final-test"

        async def _load():
            await q.put({"type": "chunk", "content": "Final answer."})
            await q.put({"type": "stream_end"})

        await _load()
        params = _make_send_params(task_id="final-test")
        async with client.stream(
            "POST", "/a2a/tasks/send/stream", content=params.model_dump_json()
        ) as resp:
            raw = await resp.aread()
            raw = raw.decode()

        events = [e.strip() for e in raw.split("\n\n") if e.strip()]
        final = events[-1]
        data_line = next(line for line in final.split("\n") if line.startswith("data:"))
        parsed = json.loads(data_line.split(":", 1)[1].strip())
        assert parsed.get("final") is True
        assert parsed["status"]["state"] == TaskState.COMPLETED

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_stream_emits_artifact_events(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:artifact-stream"

        async def _load():
            await q.put({"type": "chunk", "content": "part1"})
            await q.put({"type": "chunk", "content": "part2"})
            await q.put({"type": "stream_end"})

        await _load()
        params = _make_send_params(task_id="artifact-stream")
        async with client.stream(
            "POST", "/a2a/tasks/send/stream", content=params.model_dump_json()
        ) as resp:
            raw = await resp.aread()
            raw = raw.decode()

        events = [e.strip() for e in raw.split("\n\n") if e.strip()]
        # Should have artifact update events
        artifact_events = [e for e in events if "task_artifact_update" in e]
        assert len(artifact_events) >= 1


# ---------------------------------------------------------------------------
# Tests: POST /a2a (JSON-RPC 2.0)
# ---------------------------------------------------------------------------


class TestJSONRPCEndpoint:
    @pytest.mark.asyncio
    async def test_jsonrpc_parse_error(self, client):
        resp = await client.post("/a2a", content=b"not json{{{")
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"]["code"] == PARSE_ERROR

    @pytest.mark.asyncio
    async def test_jsonrpc_method_not_found(self, client):
        resp = await client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 1, "method": "unknown/method"},
        )
        data = resp.json()
        assert data["error"]["code"] == METHOD_NOT_FOUND

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_jsonrpc_message_send(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:jsonrpc-task"

        async def _load():
            await q.put({"type": "chunk", "content": "JSONRPC response"})
            await q.put({"type": "stream_end"})

        await _load()

        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "id": "jsonrpc-task",
                    "message": {
                        "role": "user",
                        "parts": [{"type": "text", "text": "Hello via JSON-RPC"}],
                    },
                },
            },
        )
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert data["result"]["status"]["state"] == "completed"

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_get_not_found(self, client):
        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tasks/get",
                "params": {"id": "nonexistent"},
            },
        )
        data = resp.json()
        assert data["error"]["code"] == TASK_NOT_FOUND

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_get_found(self, client):
        task = Task(id="rpc-task", status=TaskStatus(state=TaskState.WORKING))
        _tasks["rpc-task"] = task

        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tasks/get",
                "params": {"id": "rpc-task"},
            },
        )
        data = resp.json()
        assert data["result"]["id"] == "rpc-task"
        assert data["result"]["status"]["state"] == "working"

    @pytest.mark.asyncio
    async def test_tasks_get_respects_history_length(self, client):
        _tasks["hist-task"] = Task(
            id="hist-task",
            status=TaskStatus(state=TaskState.WORKING),
            history=[
                A2AMessage(role="user", parts=[TextPart(text="msg1")]),
                A2AMessage(role="agent", parts=[TextPart(text="reply1")]),
                A2AMessage(role="user", parts=[TextPart(text="msg2")]),
                A2AMessage(role="agent", parts=[TextPart(text="reply2")]),
            ],
        )

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/get",
            "params": {"id": "hist-task", "history_length": 2},
        }
        resp = await client.post("/a2a", json=payload)
        body = resp.json()
        result = body["result"]
        assert len(result["history"]) == 2
        # Should return the LAST 2 messages
        assert result["history"][0]["parts"][0]["text"] == "msg2"
        assert result["history"][1]["parts"][0]["text"] == "reply2"

    @pytest.mark.asyncio
    async def test_tasks_get_history_length_zero(self, client):
        _tasks["hist-task-0"] = Task(
            id="hist-task-0",
            status=TaskStatus(state=TaskState.WORKING),
            history=[
                A2AMessage(role="user", parts=[TextPart(text="msg1")]),
            ],
        )

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/get",
            "params": {"id": "hist-task-0", "history_length": 0},
        }
        resp = await client.post("/a2a", json=payload)
        body = resp.json()
        result = body["result"]
        assert result["history"] == []

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_cancel(self, client):
        task = Task(id="rpc-cancel", status=TaskStatus(state=TaskState.WORKING))
        _tasks["rpc-cancel"] = task
        _cancel_events["rpc-cancel"] = asyncio.Event()

        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tasks/cancel",
                "params": {"id": "rpc-cancel"},
            },
        )
        data = resp.json()
        assert data["result"]["status"] == "canceled"

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_cancel_terminal_state(self, client):
        task = Task(id="rpc-done", status=TaskStatus(state=TaskState.COMPLETED))
        _tasks["rpc-done"] = task

        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tasks/cancel",
                "params": {"id": "rpc-done"},
            },
        )
        data = resp.json()
        assert data["error"]["code"] == TASK_NOT_CANCELABLE

    @pytest.mark.asyncio
    async def test_jsonrpc_push_notification_unsupported(self, client):
        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tasks/pushNotificationConfig/set",
                "params": {},
            },
        )
        data = resp.json()
        assert data["error"]["code"] == UNSUPPORTED_OPERATION

    @pytest.mark.asyncio
    async def test_jsonrpc_push_notification_get_unsupported(self, client):
        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tasks/pushNotificationConfig/get",
                "params": {},
            },
        )
        data = resp.json()
        assert data["error"]["code"] == UNSUPPORTED_OPERATION

    @pytest.mark.asyncio
    async def test_message_send_rejects_terminal_task(self, client):
        """Sending to a completed task must return a JSON-RPC error."""
        from pocketpaw.a2a.server import _tasks as tasks_store

        tasks_store["terminal-task"] = Task(
            id="terminal-task",
            status=TaskStatus(
                state=TaskState.COMPLETED,
                message=A2AMessage(role="agent", parts=[TextPart(text="Done")]),
            ),
        )

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "id": "terminal-task",
                "message": {"role": "user", "parts": [{"type": "text", "text": "More?"}]},
            },
        }
        resp = await client.post("/a2a", json=payload)
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32003

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_jsonrpc_message_stream_returns_sse(self, mock_bridge_cls, mock_dispatch, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:rpc-stream"

        async def _load():
            await q.put({"type": "chunk", "content": "streaming"})
            await q.put({"type": "stream_end"})

        await _load()

        async with client.stream(
            "POST",
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "message/stream",
                "params": {
                    "id": "rpc-stream",
                    "message": {
                        "role": "user",
                        "parts": [{"type": "text", "text": "Stream me"}],
                    },
                },
            },
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            raw = await resp.aread()
            raw = raw.decode()

        events = [e.strip() for e in raw.split("\n\n") if e.strip()]
        assert len(events) >= 2
        # Each SSE event should have JSON-RPC wrapper
        for block in events:
            data_line = next((line for line in block.split("\n") if line.startswith("data:")), None)
            assert data_line is not None
            parsed = json.loads(data_line.split(":", 1)[1].strip())
            assert parsed["jsonrpc"] == "2.0"

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_resubscribe_not_found(self, client):
        """Resubscribing to a nonexistent task should error."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/resubscribe",
            "params": {"id": "nonexistent"},
        }
        # tasks/resubscribe is a streaming method; the SSE stream should contain an error
        async with client.stream("POST", "/a2a", json=payload) as resp:
            raw = await resp.aread()
            raw = raw.decode()
        # Parse the SSE data
        for line in raw.split("\n"):
            if line.startswith("data:"):
                body = json.loads(line.split(":", 1)[1].strip())
                assert "error" in body
                assert body["error"]["code"] == TASK_NOT_FOUND
                break
        else:
            pytest.fail("No SSE data line found in response")

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_resubscribe_terminal_returns_final(self, client):
        """Resubscribing to a completed task should return its final status."""
        _tasks["resub-done"] = Task(
            id="resub-done",
            status=TaskStatus(
                state=TaskState.COMPLETED,
                message=A2AMessage(role="agent", parts=[TextPart(text="All done")]),
            ),
        )
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/resubscribe",
            "params": {"id": "resub-done"},
        }
        async with client.stream("POST", "/a2a", json=payload) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            raw = await resp.aread()
            raw = raw.decode()

        # Should have exactly one SSE event with the final status
        events = [e.strip() for e in raw.split("\n\n") if e.strip()]
        assert len(events) >= 1
        data_line = next((line for line in events[0].split("\n") if line.startswith("data:")), None)
        assert data_line is not None
        parsed = json.loads(data_line.split(":", 1)[1].strip())
        assert parsed["jsonrpc"] == "2.0"
        result = parsed["result"]
        assert result["status"]["state"] == "completed"
        assert result["final"] is True

    @pytest.mark.asyncio
    async def test_message_send_rejects_unsupported_output_modes(self, client):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {"role": "user", "parts": [{"type": "text", "text": "Hi"}]},
                "configuration": {
                    "accepted_output_modes": ["video/mp4"],
                },
            },
        }
        resp = await client.post("/a2a", json=payload)
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32005

    @patch("pocketpaw.a2a.server._dispatch_to_agent")
    @patch("pocketpaw.a2a.server._A2ASessionBridge")
    async def test_message_send_accepts_compatible_output_modes(
        self, mock_bridge_cls, mock_dispatch, client
    ):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_dispatch.return_value = "a2a:compat-modes"

        async def _load():
            await q.put({"type": "chunk", "content": "OK"})
            await q.put({"type": "stream_end"})

        await _load()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {"role": "user", "parts": [{"type": "text", "text": "Hi"}]},
                "configuration": {
                    "accepted_output_modes": ["text/plain", "application/json"],
                },
            },
        }
        resp = await client.post("/a2a", json=payload)
        body = resp.json()
        # Should NOT have an error about output modes
        if "error" in body:
            assert body["error"]["code"] != -32005


# ---------------------------------------------------------------------------
# Tests: MessageSendConfiguration model
# ---------------------------------------------------------------------------


def test_message_send_configuration_model():
    from pocketpaw.a2a.models import MessageSendConfiguration

    config = MessageSendConfiguration(accepted_output_modes=["text/plain", "application/json"])
    assert config.accepted_output_modes == ["text/plain", "application/json"]
    assert config.history_length is None
    assert config.blocking is False


# ---------------------------------------------------------------------------
# Tests: SSE format helper
# ---------------------------------------------------------------------------


class TestFormatSSE:
    def test_format_sse_basic(self):
        result = _format_sse("test_event", {"key": "value"})
        assert result.startswith("event: test_event\n")
        assert "data:" in result
        assert result.endswith("\n\n")

    def test_format_sse_json_valid(self):
        result = _format_sse("update", {"id": "t1", "status": "working"})
        data_line = [line for line in result.split("\n") if line.startswith("data:")][0]
        parsed = json.loads(data_line.split(":", 1)[1].strip())
        assert parsed["id"] == "t1"


# ---------------------------------------------------------------------------
# Tests: _A2ASessionBridge
# ---------------------------------------------------------------------------


class TestA2ASessionBridge:
    @pytest.mark.asyncio
    async def test_bridge_creation(self):
        bridge = _A2ASessionBridge("chat-abc")
        assert bridge.chat_id == "chat-abc"
        assert isinstance(bridge.queue, asyncio.Queue)

    @pytest.mark.asyncio
    async def test_bridge_queue_chunk(self):
        bridge = _A2ASessionBridge("x")
        await bridge.queue.put({"type": "chunk", "content": "hello"})
        item = await bridge.queue.get()
        assert item["type"] == "chunk"
        assert item["content"] == "hello"


# ---------------------------------------------------------------------------
# Tests: Channel enum
# ---------------------------------------------------------------------------


class TestChannelEnum:
    def test_a2a_channel_exists(self):
        from pocketpaw.bus.events import Channel

        assert Channel.A2A == "a2a"

    def test_a2a_channel_is_distinct(self):
        from pocketpaw.bus.events import Channel

        assert Channel.A2A != Channel.WEBSOCKET


# ---------------------------------------------------------------------------
# Tests: _extract_message_text
# ---------------------------------------------------------------------------


def test_extract_message_text_all_part_types():
    """_extract_message_text should handle TextPart, FilePart, and DataPart."""
    msg = A2AMessage(
        role="user",
        parts=[
            TextPart(text="Check this file"),
            FilePart(name="report.csv", uri="https://example.com/report.csv"),
            DataPart(data={"key": "value", "count": 42}),
        ],
    )
    text = _extract_message_text(msg)
    assert "Check this file" in text
    assert "report.csv" in text
    assert "https://example.com/report.csv" in text
    assert '"key"' in text


def test_extract_message_text_embedded_file():
    msg = A2AMessage(
        role="user",
        parts=[FilePart(name="data.bin", bytes_data="AQID")],
    )
    text = _extract_message_text(msg)
    assert "data.bin" in text
    assert "embedded" in text
