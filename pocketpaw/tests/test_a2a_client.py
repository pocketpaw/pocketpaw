from __future__ import annotations

import json
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketpaw.a2a.client import A2AClient, _check_status, _handle_response
from pocketpaw.a2a.models import (
    A2AMessage,
    AgentCard,
    Artifact,
    Task,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TextPart,
)
from pocketpaw.tools.builtin.a2a_delegate import A2ADelegateTool


@pytest.fixture
def mock_agent_card() -> AgentCard:
    return AgentCard(
        name="TestAgent",
        description="A test agent",
        url="http://localhost:8001",
        version="1.0.0",
        capabilities={
            "streaming": True,
            "push_notifications": False,
            "state_transition_history": True,
        },
        skills=[],
    )


@pytest.fixture
def mock_task() -> Task:
    return Task(
        id="test-task-123",
        session_id="test-session",
        status=TaskStatus(
            state=TaskState.COMPLETED,
            message=A2AMessage(role="agent", parts=[TextPart(text="Task completed.")]),
        ),
        history=[],
        metadata={},
    )


class TestA2AClient:
    def test_handle_response_error(self):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        error = httpx.HTTPStatusError("Error", request=MagicMock(), response=mock_response)
        mock_response.raise_for_status.side_effect = error

        with pytest.raises(RuntimeError, match="A2A remote agent error 400: Bad Request"):
            _handle_response(mock_response)

    def test_check_status_error(self):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 500

        error = httpx.HTTPStatusError("Server Error", request=MagicMock(), response=mock_response)
        mock_response.raise_for_status.side_effect = error

        with pytest.raises(RuntimeError, match="A2A remote agent error 500"):
            _check_status(mock_response)

    async def test_get_agent_card_success(self, mock_agent_card):
        client = A2AClient()
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = mock_agent_card.model_dump_json().encode()

        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            result = await client.get_agent_card("http://localhost:8001")

            assert result.name == "TestAgent"
            mock_client_instance.get.assert_called_once_with(
                "http://localhost:8001/.well-known/agent.json"
            )

    async def test_send_task_success(self, mock_task):
        client = A2AClient()
        params = TaskSendParams(message=A2AMessage(role="user", parts=[TextPart(text="Do this")]))

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = mock_task.model_dump_json().encode()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            result = await client.send_task("http://localhost:8001", params)

            assert result.id == "test-task-123"
            assert result.status.state == TaskState.COMPLETED
            mock_client_instance.post.assert_called_once_with(
                "http://localhost:8001/a2a/tasks/send",
                json=params.model_dump(mode="json", exclude_none=True),
            )

    async def test_get_task_success(self, mock_task):
        client = A2AClient()
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = mock_task.model_dump_json().encode()

        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            result = await client.get_task("http://localhost:8001", "test-task-123")

            assert result.id == "test-task-123"
            mock_client_instance.get.assert_called_once_with(
                "http://localhost:8001/a2a/tasks/test-task-123"
            )

    async def test_cancel_task_success(self):
        client = A2AClient()
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = b""

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            await client.cancel_task("http://localhost:8001", "test-task-123")
            mock_client_instance.post.assert_called_once_with(
                "http://localhost:8001/a2a/tasks/test-task-123/cancel"
            )

    async def test_send_task_stream_success(self):
        client = A2AClient()
        params = TaskSendParams(message=A2AMessage(role="user", parts=[TextPart(text="Do this")]))

        mock_response = AsyncMock(spec=httpx.Response)

        async def mock_aiter_lines():
            yield 'data: {"event":"task_status_update"}'
            yield ""
            yield 'data: {"event":"task_status_update"}'

        mock_response.aiter_lines.side_effect = mock_aiter_lines

        mock_stream_context = MagicMock()
        mock_stream_context.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_context.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = AsyncMock()
        mock_client_instance.stream = MagicMock(return_value=mock_stream_context)
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            events = []
            async for event in client.send_task_stream("http://localhost:8001", params):
                events.append(event)

            assert len(events) == 2
            assert events[0] == '{"event":"task_status_update"}'

            mock_client_instance.stream.assert_called_once_with(
                "POST",
                "http://localhost:8001/a2a/tasks/send/stream",
                json=params.model_dump(mode="json", exclude_none=True),
            )

    async def test_send_task_stream_failure(self):
        client = A2AClient()
        params = TaskSendParams(message=A2AMessage(role="user", parts=[TextPart(text="Fail")]))

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 403
        error = httpx.HTTPStatusError("Forbidden", request=AsyncMock(), response=mock_response)
        mock_response.raise_for_status.side_effect = error

        mock_stream_context = MagicMock()
        mock_stream_context.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_context.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = AsyncMock()
        mock_client_instance.stream = MagicMock(return_value=mock_stream_context)
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            with pytest.raises(RuntimeError, match="A2A remote agent error 403"):
                async for _ in client.send_task_stream("http://localhost:8001", params):
                    pass

    async def test_context_manager_reuses_client(self, mock_task):
        """Verify that using A2AClient as a context manager shares a single httpx client."""
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = mock_task.model_dump_json().encode()

        mock_httpx_client = AsyncMock()
        mock_httpx_client.get.return_value = mock_response
        mock_httpx_client.post.return_value = mock_response
        mock_httpx_client.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_httpx_client) as MockHttpxClient:
            async with A2AClient() as a2a_client:
                await a2a_client.get_task("http://localhost:8001", "task-1")
                await a2a_client.get_task("http://localhost:8001", "task-2")

            # httpx.AsyncClient must only be constructed once (shared for both calls)
            MockHttpxClient.assert_called_once()
            assert mock_httpx_client.aclose.called, "Shared client should be closed on exit"

    async def test_agent_card_cached_on_second_call(self, mock_agent_card):
        """Second call to get_agent_card should use cache, not HTTP."""
        client = A2AClient()
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = mock_agent_card.model_dump_json().encode()

        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            card1 = await client.get_agent_card("http://localhost:8001")
            card2 = await client.get_agent_card("http://localhost:8001")
            assert card1.name == card2.name
            # HTTP GET should only be called once (second was cached)
            assert mock_client_instance.get.call_count == 1

    async def test_auth_headers_passed_to_requests(self, mock_agent_card):
        """Auth headers should be included in HTTP requests."""
        client = A2AClient(auth_headers={"Authorization": "Bearer test-token"})
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = mock_agent_card.model_dump_json().encode()

        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance) as MockHttpxClient:
            await client.get_agent_card("http://localhost:8001")
            # Verify auth headers were passed to AsyncClient constructor
            call_kwargs = MockHttpxClient.call_args
            assert call_kwargs.kwargs.get("headers") == {"Authorization": "Bearer test-token"}

    async def test_send_task_handles_message_response(self):
        """When remote agent returns a Message instead of Task, client wraps it."""
        client = A2AClient()
        params = TaskSendParams(
            message=A2AMessage(role="user", parts=[TextPart(text="Quick question")])
        )
        message_response = {
            "role": "agent",
            "message_id": "msg-123",
            "parts": [{"type": "text", "text": "Direct reply"}],
        }

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.content = json.dumps(message_response).encode()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_client_instance.__aenter__.return_value = mock_client_instance

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            result = await client.send_task("http://localhost:8001", params)
            # Should return a Task wrapping the message
            assert result.status.state == TaskState.COMPLETED
            assert result.status.message is not None
            assert result.status.message.parts[0].text == "Direct reply"
            assert len(result.history) == 2


class TestA2ADelegateTool:
    @pytest.fixture(autouse=True)
    def mock_settings(self):
        with patch("pocketpaw.tools.builtin.a2a_delegate.get_settings") as mock_get_settings:
            mock_get_settings.return_value.a2a_trusted_agents = ["http://localhost:8001"]
            yield mock_get_settings

    async def test_delegate_tool_success(self, mock_agent_card, mock_task):
        tool = A2ADelegateTool()

        with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
            mock_client = MockClient.return_value
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get_agent_card = AsyncMock(return_value=mock_agent_card)
            mock_client.send_task = AsyncMock(return_value=mock_task)

            result = await tool.execute(agent_url="http://localhost:8001", task="Help me")

            assert not result.startswith("Error:")
            parsed = json.loads(result)
            assert parsed["agent_name"] == "TestAgent"
            assert parsed["task_id"] == "test-task-123"
            assert parsed["status"] == "completed"
            assert parsed["reply"] == "Task completed."

    async def test_delegate_tool_card_fetch_failure(self):
        tool = A2ADelegateTool()

        with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
            mock_client = MockClient.return_value
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get_agent_card = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )

            result = await tool.execute(agent_url="http://localhost:8001", task="Help me")

            assert result.startswith("Error:")
            assert "Failed to fetch Agent Card" in result
            assert "Connection refused" in result

    async def test_delegate_tool_multi_turn_success(self, mock_agent_card, mock_task):
        tool = A2ADelegateTool()

        # Setup an existing task with history
        existing_task = Task(
            id="test-task-123",
            session_id="test-session",
            status=TaskStatus(state=TaskState.COMPLETED),
            history=[
                A2AMessage(role="user", parts=[TextPart(text="Hello")]),
                A2AMessage(role="agent", parts=[TextPart(text="Hi there")]),
            ],
        )

        with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
            mock_client = MockClient.return_value
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get_agent_card = AsyncMock(return_value=mock_agent_card)
            mock_client.get_task = AsyncMock(return_value=existing_task)
            mock_client.send_task = AsyncMock(return_value=mock_task)

            result = await tool.execute(
                agent_url="http://localhost:8001", task="Help me more", task_id="test-task-123"
            )

            assert not result.startswith("Error:")
            mock_client.get_task.assert_called_once_with("http://localhost:8001", "test-task-123")

            # Verify send_task was called with the new message separate from history
            call_args = mock_client.send_task.call_args
            sent_params: TaskSendParams = call_args[0][1]
            assert sent_params.id == "test-task-123"

            # The new user turn must be its own message with only the new text
            assert len(sent_params.message.parts) == 1
            assert sent_params.message.parts[0].text == "Help me more"
            assert sent_params.message.role == "user"

            # History must preserve the original message structure (two messages, not flattened)
            assert len(sent_params.history) == 2
            assert sent_params.history[0].role == "user"
            assert sent_params.history[0].parts[0].text == "Hello"
            assert sent_params.history[1].role == "agent"
            assert sent_params.history[1].parts[0].text == "Hi there"

    async def test_delegate_tool_multi_turn_unsupported(self, mock_agent_card, mock_task):
        tool = A2ADelegateTool()

        mock_agent_card.capabilities.state_transition_history = False

        with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
            mock_client = MockClient.return_value
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get_agent_card = AsyncMock(return_value=mock_agent_card)
            mock_client.get_task = AsyncMock(return_value=mock_task)

            result = await tool.execute(
                agent_url="http://localhost:8001", task="Help me more", task_id="test-task-123"
            )

            assert result.startswith("Error:")
            assert "does not support multi-turn" in result

    async def test_delegate_tool_task_send_failure(self, mock_agent_card):
        tool = A2ADelegateTool()

        with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
            mock_client = MockClient.return_value
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get_agent_card = AsyncMock(return_value=mock_agent_card)
            mock_client.send_task = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

            result = await tool.execute(agent_url="http://localhost:8001", task="Help me")

            assert result.startswith("Error:")
            assert "Failed to submit task" in result
            assert "Timeout" in result

    async def test_delegate_tool_includes_artifacts(self, mock_agent_card):
        tool = A2ADelegateTool()

        task_with_artifacts = Task(
            id="art-task",
            session_id="s1",
            status=TaskStatus(
                state=TaskState.COMPLETED,
                message=A2AMessage(role="agent", parts=[TextPart(text="Here's the report")]),
            ),
            artifacts=[
                Artifact(
                    name="report.csv",
                    description="Generated report",
                    parts=[TextPart(text="col1,col2\n1,2")],
                )
            ],
        )

        with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
            mock_client = MockClient.return_value
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get_agent_card = AsyncMock(return_value=mock_agent_card)
            mock_client.send_task = AsyncMock(return_value=task_with_artifacts)

            result = await tool.execute(agent_url="http://localhost:8001", task="Generate report")
            parsed = json.loads(result)
            assert "artifacts" in parsed
            assert len(parsed["artifacts"]) == 1
            assert parsed["artifacts"][0]["name"] == "report.csv"
            assert "col1,col2" in parsed["artifacts"][0]["content"]

    async def test_delegate_tool_no_artifacts_key_when_empty(self, mock_agent_card, mock_task):
        tool = A2ADelegateTool()

        with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
            mock_client = MockClient.return_value
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get_agent_card = AsyncMock(return_value=mock_agent_card)
            mock_client.send_task = AsyncMock(return_value=mock_task)

            result = await tool.execute(agent_url="http://localhost:8001", task="Help me")
            parsed = json.loads(result)
            assert "artifacts" not in parsed


class TestSSRFProtection:
    async def test_ssrf_private_ip_blocked(self):
        tool = A2ADelegateTool()
        with patch("pocketpaw.tools.builtin.a2a_delegate.get_settings") as mock_get_settings:
            mock_get_settings.return_value.a2a_trusted_agents = []
            target = "pocketpaw.tools.builtin.a2a_delegate.socket.getaddrinfo"
            with patch(target) as mock_getaddrinfo:
                # Return multiple IPs, one is private
                mock_getaddrinfo.return_value = [
                    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 80)),
                    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.5", 80)),
                ]

                result = await tool.execute(agent_url="http://evil.com", task="Help")
                assert result.startswith("Error:")
                assert "SSRF Protection" in result
                assert "192.168.1.5" in result

    async def test_ssrf_invalid_scheme_blocked(self):
        tool = A2ADelegateTool()
        with patch("pocketpaw.tools.builtin.a2a_delegate.get_settings") as mock_get_settings:
            mock_get_settings.return_value.a2a_trusted_agents = []

            result = await tool.execute(agent_url="ftp://evil.com", task="Help")
            assert result.startswith("Error:")
            assert "Invalid URL scheme" in result

    async def test_ssrf_dns_resolution_failure(self):
        tool = A2ADelegateTool()
        with patch("pocketpaw.tools.builtin.a2a_delegate.get_settings") as mock_get_settings:
            mock_get_settings.return_value.a2a_trusted_agents = []
            target = "pocketpaw.tools.builtin.a2a_delegate.socket.getaddrinfo"
            with patch(target) as mock_getaddrinfo:
                mock_getaddrinfo.side_effect = socket.gaierror("Name or service not known")

                result = await tool.execute(agent_url="http://nonexistent.invalid", task="Help")
                assert result.startswith("Error:")
                assert "Could not resolve hostname" in result

    async def test_ssrf_public_ip_allowed(self, mock_agent_card, mock_task):
        tool = A2ADelegateTool()
        with patch("pocketpaw.tools.builtin.a2a_delegate.get_settings") as mock_get_settings:
            mock_get_settings.return_value.a2a_trusted_agents = []
            target = "pocketpaw.tools.builtin.a2a_delegate.socket.getaddrinfo"
            with patch(target) as mock_getaddrinfo:
                mock_getaddrinfo.return_value = [
                    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 80)),
                ]

                with patch("pocketpaw.tools.builtin.a2a_delegate.A2AClient") as MockClient:
                    mock_client = MockClient.return_value
                    mock_client.__aenter__.return_value = mock_client
                    mock_client.__aexit__ = AsyncMock(return_value=None)
                    mock_client.get_agent_card = AsyncMock(return_value=mock_agent_card)
                    mock_client.send_task = AsyncMock(return_value=mock_task)

                    result = await tool.execute(agent_url="http://good.com", task="Help")
                    assert not result.startswith("Error:")
