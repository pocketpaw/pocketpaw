"""GitHub Copilot SDK backend for PocketPaw.

Uses the github-copilot-sdk Python package which wraps the `copilot` CLI
via JSON-RPC, providing async event-driven agent execution with streaming,
tool use, and session management.

Built-in tools: shell, file operations, git, web search.

Requires: `copilot` CLI on PATH + `pip install github-copilot-sdk`.
"""

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class CopilotSDKBackend:
    """Copilot SDK backend — Python SDK wrapper for GitHub Copilot CLI agent."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="copilot_sdk",
            display_name="Copilot SDK",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["shell", "file_ops", "git", "web_search"],
            tool_policy_map={
                "shell": "shell",
                "file_ops": "write_file",
                "git": "shell",
                "web_search": "browser",
            },
            required_keys=[],
            supported_providers=["copilot", "openai", "azure", "anthropic"],
            install_hint={
                "pip_package": "github-copilot-sdk",
                "pip_spec": "github-copilot-sdk",
                "verify_import": "copilot",
                "verify_attr": "CopilotClient",
                "external_cmd": ("Install copilot CLI from https://github.com/github/copilot-sdk"),
                "docs_url": "https://github.com/github/copilot-sdk",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._cli_available = shutil.which("copilot") is not None
        self._sdk_available = False
        self._client: Any = None
        self._sessions: dict[str, Any] = {}

        try:
            import copilot  # noqa: F401

            self._sdk_available = True
        except ImportError:
            pass

        if self._cli_available and self._sdk_available:
            logger.info("Copilot SDK ready (CLI + SDK both available)")
        elif not self._cli_available:
            logger.warning(
                "Copilot CLI not found — install from: https://github.com/github/copilot-sdk"
            )
        elif not self._sdk_available:
            logger.warning("Copilot SDK not found — install with: pip install github-copilot-sdk")

    @staticmethod
    def _inject_history(instruction: str, history: list[dict]) -> str:
        """Append conversation history to instruction as text."""
        lines = ["# Recent Conversation"]
        for msg in history:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"**{role}**: {content}")
        return instruction + "\n\n" + "\n".join(lines)

    async def _ensure_client(self) -> Any:
        """Lazily start and return the CopilotClient singleton."""
        if self._client is not None:
            return self._client

        from copilot import CopilotClient

        self._client = CopilotClient()
        await self._client.start()
        return self._client

    def _get_provider_config(self) -> dict[str, Any] | None:
        """Build BYOK provider configuration from settings.

        Returns None for default Copilot provider (GitHub OAuth).
        Returns a provider dict for openai/azure/anthropic BYOK.
        """
        provider = self.settings.copilot_sdk_provider

        if provider == "openai":
            cfg: dict[str, Any] = {"type": "openai"}
            if self.settings.openai_compatible_base_url:
                cfg["base_url"] = self.settings.openai_compatible_base_url
            if self.settings.openai_api_key:
                cfg["api_key"] = self.settings.openai_api_key
            elif self.settings.openai_compatible_api_key:
                cfg["api_key"] = self.settings.openai_compatible_api_key
            return cfg

        if provider == "azure":
            cfg = {"type": "azure"}
            if self.settings.openai_compatible_base_url:
                cfg["base_url"] = self.settings.openai_compatible_base_url
            if self.settings.openai_api_key:
                cfg["api_key"] = self.settings.openai_api_key
            return cfg

        if provider == "anthropic":
            cfg = {"type": "anthropic"}
            if self.settings.anthropic_api_key:
                cfg["api_key"] = self.settings.anthropic_api_key
            return cfg

        # Default: use GitHub Copilot provider (no BYOK config needed)
        return None

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not self._cli_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Copilot CLI not found on PATH.\n\n"
                    "Install from: https://github.com/github/copilot-sdk"
                ),
            )
            return

        if not self._sdk_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Copilot SDK not installed.\n\nInstall with: pip install github-copilot-sdk"
                ),
            )
            return

        self._stop_flag = False

        try:
            client = await self._ensure_client()

            # Build the prompt
            prompt_parts = []
            if system_prompt:
                prompt_parts.append(f"[System Instructions]\n{system_prompt}\n")
            if history:
                prompt_parts.append(self._inject_history("", history).strip())
            prompt_parts.append(message)

            # Inject compact tool instructions so the agent can use PocketPaw tools via shell
            try:
                from pocketpaw.agents.tool_bridge import get_tool_instructions_compact

                tool_section = get_tool_instructions_compact(self.settings)
                if tool_section:
                    prompt_parts.insert(-1, tool_section)
            except ImportError:
                pass

            full_prompt = "\n\n".join(prompt_parts)

            model = self.settings.copilot_sdk_model or "gpt-5.2"
            provider_config = self._get_provider_config()

            # Create or reuse session
            session = None
            if session_key and session_key in self._sessions:
                session = self._sessions[session_key]
            else:
                session_opts: dict[str, Any] = {
                    "model": model,
                    "streaming": True,
                }
                if system_prompt:
                    session_opts["system_message"] = system_prompt
                if provider_config:
                    session_opts["provider"] = provider_config

                session = await client.create_session(session_opts)
                if session_key:
                    self._sessions[session_key] = session

            # Collect events via queue
            queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
            _streamed_via_deltas = False  # Track if we got streaming deltas

            def on_event(event: Any) -> None:
                """Map Copilot SDK events to AgentEvents and enqueue."""
                nonlocal _streamed_via_deltas
                # event.type is an enum; use .value for string comparison
                event_type = _get_event_type(event)
                data = getattr(event, "data", event)

                if event_type == "assistant.message_delta":
                    delta = getattr(data, "delta_content", "") or ""
                    if delta:
                        _streamed_via_deltas = True
                        queue.put_nowait(AgentEvent(type="message", content=delta))

                elif event_type == "assistant.reasoning_delta":
                    delta = getattr(data, "delta_content", "") or ""
                    if delta:
                        queue.put_nowait(AgentEvent(type="thinking", content=delta))

                elif event_type == "assistant.message":
                    # Final complete message — only use if no deltas were streamed
                    if not _streamed_via_deltas:
                        content = getattr(data, "content", "") or ""
                        if content:
                            queue.put_nowait(AgentEvent(type="message", content=content))
                    _streamed_via_deltas = False

                elif event_type == "tool.call":
                    name = getattr(data, "name", "tool")
                    args = getattr(data, "arguments", {})
                    queue.put_nowait(
                        AgentEvent(
                            type="tool_use",
                            content=f"Using: {name}",
                            metadata={"name": name, "input": args},
                        )
                    )

                elif event_type == "tool.result":
                    name = getattr(data, "name", "tool")
                    output = getattr(data, "output", "")
                    queue.put_nowait(
                        AgentEvent(
                            type="tool_result",
                            content=str(output)[:200],
                            metadata={"name": name},
                        )
                    )

                elif event_type == "session.idle":
                    queue.put_nowait(None)  # sentinel for done

                elif event_type == "error":
                    error_msg = getattr(data, "message", "Unknown Copilot SDK error")
                    queue.put_nowait(AgentEvent(type="error", content=error_msg))

            session.on(on_event)

            # Send the message
            await session.send({"prompt": full_prompt})

            # Drain events from queue
            max_turns = self.settings.copilot_sdk_max_turns
            turn_count = 0
            while not self._stop_flag:
                event = await queue.get()

                if event is None:
                    break

                yield event

                if event.type == "tool_result":
                    turn_count += 1
                    if max_turns and turn_count >= max_turns:
                        yield AgentEvent(
                            type="error",
                            content=f"Reached max turns ({max_turns})",
                        )
                        break

            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error("Copilot SDK error: %s", e)
            yield AgentEvent(type="error", content=f"Copilot SDK error: {e}")

    async def stop(self) -> None:
        self._stop_flag = True
        # Destroy all active sessions
        for session in self._sessions.values():
            try:
                await session.destroy()
            except Exception:
                pass
        self._sessions.clear()

        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                pass
            self._client = None

    async def get_status(self) -> dict[str, Any]:
        return {
            "backend": "copilot_sdk",
            "cli_available": self._cli_available,
            "sdk_available": self._sdk_available,
            "running": self._client is not None,
            "model": self.settings.copilot_sdk_model or "gpt-5.2",
            "provider": self.settings.copilot_sdk_provider,
            "active_sessions": len(self._sessions),
        }


def _get_event_type(event: Any) -> str:
    """Extract event type string, handling both enum and plain str."""
    raw = getattr(event, "type", "")
    return raw.value if hasattr(raw, "value") else str(raw)
