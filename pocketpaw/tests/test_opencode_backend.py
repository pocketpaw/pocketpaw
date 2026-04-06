"""Tests for OpenCode REST API backend."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings


def _make_backend(**overrides):
    """Create an OpenCodeBackend with optional settings overrides."""
    from pocketpaw.agents.opencode import OpenCodeBackend

    settings = Settings(**overrides)
    return OpenCodeBackend(settings)


class TestOpenCodeInfo:
    def test_info_name(self):
        from pocketpaw.agents.opencode import OpenCodeBackend

        info = OpenCodeBackend.info()
        assert info.name == "opencode"

    def test_info_display_name(self):
        from pocketpaw.agents.opencode import OpenCodeBackend

        info = OpenCodeBackend.info()
        assert info.display_name == "OpenCode"

    def test_info_capabilities(self):
        from pocketpaw.agents.opencode import OpenCodeBackend

        info = OpenCodeBackend.info()
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities
        assert Capability.CUSTOM_SYSTEM_PROMPT in info.capabilities

    def test_info_no_builtin_tools(self):
        from pocketpaw.agents.opencode import OpenCodeBackend

        info = OpenCodeBackend.info()
        assert info.builtin_tools == []
        assert info.tool_policy_map == {}


class TestOpenCodeInit:
    def test_default_base_url(self):
        backend = _make_backend()
        assert backend._base_url == "http://localhost:4096"

    def test_custom_base_url(self):
        backend = _make_backend(opencode_base_url="http://myserver:8080/")
        assert backend._base_url == "http://myserver:8080"  # trailing slash stripped

    def test_session_map_empty_on_init(self):
        backend = _make_backend()
        assert backend._session_map == {}


class TestOpenCodeHealth:
    @pytest.mark.asyncio
    async def test_health_success(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get = AsyncMock(return_value=mock_resp)
        backend._client = mock_client

        assert await backend._check_health() is True
        mock_client.get.assert_called_once_with("/")

    @pytest.mark.asyncio
    async def test_health_server_error(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client.get = AsyncMock(return_value=mock_resp)
        backend._client = mock_client

        assert await backend._check_health() is False

    @pytest.mark.asyncio
    async def test_health_connect_error(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        backend._client = mock_client

        assert await backend._check_health() is False

    @pytest.mark.asyncio
    async def test_health_timeout(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        backend._client = mock_client

        assert await backend._check_health() is False


class TestOpenCodeSession:
    @pytest.mark.asyncio
    async def test_create_session(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "sess-123", "createdAt": "2026-01-01"}
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        backend._client = mock_client

        session_id = await backend._get_or_create_session("test-key")
        assert session_id == "sess-123"
        assert backend._session_map["test-key"] == "sess-123"
        mock_client.post.assert_called_once_with("/session")

    @pytest.mark.asyncio
    async def test_session_cached(self):
        backend = _make_backend()
        backend._session_map["cached-key"] = "sess-cached"

        session_id = await backend._get_or_create_session("cached-key")
        assert session_id == "sess-cached"


class TestOpenCodeRun:
    @pytest.mark.asyncio
    async def test_run_server_unreachable(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        backend._client = mock_client

        events = []
        async for event in backend.run("hello"):
            events.append(event)

        assert any(e.type == "error" for e in events)
        assert "unreachable" in events[0].content.lower()

    @pytest.mark.asyncio
    async def test_run_text_response(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-1"}
        session_resp.raise_for_status = MagicMock()

        msg_resp = MagicMock()
        msg_resp.json.return_value = {
            "info": {"id": "msg-1"},
            "parts": [{"type": "text", "text": "Hello, world!"}],
        }
        msg_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, msg_resp])
        backend._client = mock_client

        events = []
        async for event in backend.run("hi"):
            events.append(event)

        types = [e.type for e in events]
        assert "message" in types
        assert events[-1].type == "done"
        msg_events = [e for e in events if e.type == "message"]
        assert msg_events[0].content == "Hello, world!"

    @pytest.mark.asyncio
    async def test_run_tool_response(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-2"}
        session_resp.raise_for_status = MagicMock()

        msg_resp = MagicMock()
        msg_resp.json.return_value = {
            "info": {"id": "msg-2"},
            "parts": [
                {
                    "type": "tool",
                    "tool": {"name": "bash"},
                    "state": {"status": "completed", "output": "file.txt"},
                },
                {"type": "text", "text": "Done running bash."},
            ],
        }
        msg_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, msg_resp])
        backend._client = mock_client

        events = []
        async for event in backend.run("list files"):
            events.append(event)

        types = [e.type for e in events]
        assert "tool_use" in types
        assert "tool_result" in types
        assert "message" in types
        assert events[-1].type == "done"

    @pytest.mark.asyncio
    async def test_run_with_system_prompt(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-3"}
        session_resp.raise_for_status = MagicMock()

        msg_resp = MagicMock()
        msg_resp.json.return_value = {
            "info": {"id": "msg-3"},
            "parts": [{"type": "text", "text": "ok"}],
        }
        msg_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, msg_resp])
        backend._client = mock_client

        events = []
        async for event in backend.run("hi", system_prompt="Be helpful"):
            events.append(event)

        # 2 POST calls: session + message (system sent inline)
        assert mock_client.post.call_count == 2
        # The message call should contain the system param (may include tool instructions)
        msg_call = mock_client.post.call_args_list[-1]
        payload = msg_call.kwargs.get("json") or msg_call[1].get("json")
        assert "Be helpful" in payload["system"]

    @pytest.mark.asyncio
    async def test_run_http_error(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-err"}
        session_resp.raise_for_status = MagicMock()

        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=error_resp
            )
        )

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, error_resp])
        backend._client = mock_client

        events = []
        async for event in backend.run("fail"):
            events.append(event)

        assert any(e.type == "error" for e in events)
        assert events[-1].type == "done"

    @pytest.mark.asyncio
    async def test_run_with_model(self):
        backend = _make_backend(opencode_model="anthropic/claude-sonnet-4-5-20250929")
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-m"}
        session_resp.raise_for_status = MagicMock()

        msg_resp = MagicMock()
        msg_resp.json.return_value = {
            "info": {"id": "msg-m"},
            "parts": [{"type": "text", "text": "response"}],
        }
        msg_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, msg_resp])
        backend._client = mock_client

        events = []
        async for event in backend.run("test"):
            events.append(event)

        # Check the message POST included model as string
        msg_call = mock_client.post.call_args_list[-1]
        payload = msg_call.kwargs.get("json") or msg_call[1].get("json")
        assert payload["model"] == "anthropic/claude-sonnet-4-5-20250929"

    @pytest.mark.asyncio
    async def test_run_uses_message_endpoint(self):
        """Verify we POST to /session/{id}/message, not /prompt."""
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-ep"}
        session_resp.raise_for_status = MagicMock()

        msg_resp = MagicMock()
        msg_resp.json.return_value = {
            "info": {},
            "parts": [{"type": "text", "text": "hi"}],
        }
        msg_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, msg_resp])
        backend._client = mock_client

        events = []
        async for event in backend.run("test"):
            events.append(event)

        msg_call = mock_client.post.call_args_list[-1]
        endpoint = msg_call.args[0] if msg_call.args else msg_call[0][0]
        assert endpoint == "/session/sess-ep/message"


class TestOpenCodeToolInstructions:
    """Tests for PocketPaw tool instruction injection."""

    @pytest.mark.asyncio
    async def test_system_payload_includes_tool_instructions(self):
        """Tool instructions are appended to the system prompt in the payload."""
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-tools"}
        session_resp.raise_for_status = MagicMock()

        msg_resp = MagicMock()
        msg_resp.json.return_value = {
            "info": {},
            "parts": [{"type": "text", "text": "ok"}],
        }
        msg_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, msg_resp])
        backend._client = mock_client

        with patch(
            "pocketpaw.agents.tool_bridge.get_tool_instructions_compact",
            return_value="# PocketPaw Tools\n- `web_search` — Search the web",
        ):
            async for _ in backend.run("hi", system_prompt="Be helpful"):
                pass

        msg_call = mock_client.post.call_args_list[-1]
        payload = msg_call.kwargs.get("json") or msg_call[1].get("json")
        assert "PocketPaw Tools" in payload["system"]
        assert "Be helpful" in payload["system"]

    @pytest.mark.asyncio
    async def test_tool_section_appended_without_system_prompt(self):
        """Tool instructions appear even when no system_prompt is given."""
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False

        health_resp = MagicMock()
        health_resp.status_code = 200

        session_resp = MagicMock()
        session_resp.json.return_value = {"id": "sess-tools2"}
        session_resp.raise_for_status = MagicMock()

        msg_resp = MagicMock()
        msg_resp.json.return_value = {
            "info": {},
            "parts": [{"type": "text", "text": "ok"}],
        }
        msg_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=health_resp)
        mock_client.post = AsyncMock(side_effect=[session_resp, msg_resp])
        backend._client = mock_client

        with patch(
            "pocketpaw.agents.tool_bridge.get_tool_instructions_compact",
            return_value="# PocketPaw Tools\n- `recall` — Recall memories",
        ):
            async for _ in backend.run("hi"):
                pass

        msg_call = mock_client.post.call_args_list[-1]
        payload = msg_call.kwargs.get("json") or msg_call[1].get("json")
        assert "PocketPaw Tools" in payload["system"]


class TestOpenCodeStop:
    @pytest.mark.asyncio
    async def test_stop_sets_flag(self):
        backend = _make_backend()
        await backend.stop()
        assert backend._stop_flag is True

    @pytest.mark.asyncio
    async def test_stop_closes_client(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()
        backend._client = mock_client

        await backend.stop()
        mock_client.aclose.assert_called_once()
        assert backend._client is None


class TestOpenCodeStatus:
    @pytest.mark.asyncio
    async def test_status_reachable(self):
        backend = _make_backend(opencode_model="openai/gpt-4o")
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get = AsyncMock(return_value=mock_resp)
        backend._client = mock_client
        backend._session_map = {"a": "1", "b": "2"}

        status = await backend.get_status()
        assert status["backend"] == "opencode"
        assert status["server_url"] == "http://localhost:4096"
        assert status["reachable"] is True
        assert status["model"] == "openai/gpt-4o"
        assert status["sessions"] == 2

    @pytest.mark.asyncio
    async def test_status_unreachable(self):
        backend = _make_backend()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        backend._client = mock_client

        status = await backend.get_status()
        assert status["reachable"] is False
        assert status["model"] == "server default"
        assert status["sessions"] == 0
