"""OpenCode server backend for PocketPaw.

Communicates with a running OpenCode server via its REST API.
Start the server with: ``opencode --server``

Requires: OpenCode server running at ``opencode_base_url`` (default http://localhost:4096).

API reference (from opencode server.mdx / sdk.mdx):
  POST /session              → {id, createdAt}
  POST /session/{id}/message → {info, parts}
    body: {parts, model?, system?, noReply?}
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class OpenCodeBackend:
    """OpenCode server backend — communicates via REST API."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="opencode",
            display_name="OpenCode",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=[],
            tool_policy_map={},
            required_keys=[],
            supported_providers=[],
            install_hint={
                "external_cmd": "go install github.com/opencode-ai/opencode@latest",
                "docs_url": "https://github.com/opencode-ai/opencode",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._base_url = settings.opencode_base_url.rstrip("/")
        self._stop_flag = False
        self._session_map: dict[str, str] = {}  # pocketpaw key → opencode session ID
        self._client: httpx.AsyncClient | None = None
        logger.info("OpenCode backend targeting %s", self._base_url)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=None)
        return self._client

    async def _check_health(self) -> bool:
        """Return True if the OpenCode server is reachable."""
        try:
            resp = await self._get_client().get("/")
            return resp.status_code < 500
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def _get_or_create_session(self, key: str = "_default") -> str:
        """Return an OpenCode session ID, creating one if needed."""
        if key in self._session_map:
            return self._session_map[key]

        resp = await self._get_client().post("/session")
        resp.raise_for_status()
        data = resp.json()
        # API returns {id, createdAt} directly
        session_id = data["id"]
        self._session_map[key] = session_id
        logger.info("Created OpenCode session %s for key %s", session_id, key)
        return session_id

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self._stop_flag = False

        # 1. Health check
        if not await self._check_health():
            yield AgentEvent(
                type="error",
                content=(
                    f"OpenCode server unreachable at {self._base_url}.\n\n"
                    "Start with: opencode --server"
                ),
            )
            return

        try:
            # 2. Get or create session
            session_id = await self._get_or_create_session()

            if self._stop_flag:
                yield AgentEvent(type="done", content="")
                return

            # 3. Build request body — /session/{id}/message supports
            #    system, model, noReply, and parts directly
            effective_system = system_prompt or ""
            try:
                from pocketpaw.agents.tool_bridge import get_tool_instructions_compact

                tool_section = get_tool_instructions_compact(self.settings)
                if tool_section:
                    effective_system = (effective_system + "\n" + tool_section).strip()
            except ImportError:
                pass

            payload: dict[str, Any] = {
                "parts": [{"type": "text", "text": message}],
            }
            if effective_system:
                payload["system"] = effective_system
            model = self.settings.opencode_model
            if model:
                payload["model"] = model

            # 4. Send message
            client = self._get_client()
            resp = await client.post(f"/session/{session_id}/message", json=payload)
            resp.raise_for_status()
            data = resp.json()

            # 5. Parse response — API returns {info: Message, parts: Part[]}
            parts = []
            if isinstance(data, dict):
                parts = data.get("parts", [])

            for part in parts:
                if self._stop_flag:
                    break
                part_type = part.get("type", "text")
                if part_type == "text":
                    text = part.get("text", "")
                    if text:
                        yield AgentEvent(type="message", content=text)
                elif part_type == "tool":
                    tool_name = (
                        part.get("tool", {}).get("name", "tool")
                        if isinstance(part.get("tool"), dict)
                        else str(part.get("tool", "tool"))
                    )
                    yield AgentEvent(
                        type="tool_use",
                        content=f"Using {tool_name}...",
                        metadata={"name": tool_name},
                    )
                    state = part.get("state", {})
                    if isinstance(state, dict) and state.get("output"):
                        yield AgentEvent(
                            type="tool_result",
                            content=str(state["output"])[:200],
                            metadata={"name": tool_name},
                        )

            # If no parts were found, try fallback fields
            if not parts:
                text = ""
                if isinstance(data, dict):
                    text = data.get("content", "") or data.get("text", "")
                if text:
                    yield AgentEvent(type="message", content=text)

        except httpx.HTTPStatusError as e:
            logger.error("OpenCode HTTP error: %s", e)
            yield AgentEvent(
                type="error",
                content=f"OpenCode server error: {e.response.status_code}",
            )
        except Exception as e:
            logger.error("OpenCode backend error: %s", e)
            yield AgentEvent(type="error", content=f"OpenCode error: {e}")

        yield AgentEvent(type="done", content="")

    async def stop(self) -> None:
        self._stop_flag = True
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_status(self) -> dict[str, Any]:
        reachable = await self._check_health()
        return {
            "backend": "opencode",
            "server_url": self._base_url,
            "reachable": reachable,
            "model": self.settings.opencode_model or "server default",
            "sessions": len(self._session_map),
        }
