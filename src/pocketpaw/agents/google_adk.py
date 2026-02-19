"""Google ADK backend for PocketPaw.

Uses the official Google Agent Development Kit (pip install google-adk) which provides:
- LlmAgent with native Gemini model support
- InMemoryRunner with session management
- Built-in tools: google_search, code_execution
- MCP toolset integration (stdio/SSE)
- Custom Python function tools via FunctionTool

Requires: pip install google-adk, GOOGLE_API_KEY env var.
"""

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)

# App name constant for ADK session management
_APP_NAME = "pocketpaw"


class GoogleADKBackend:
    """Google ADK backend — native Python SDK for Gemini-powered agents."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="google_adk",
            display_name="Google ADK",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["google_search", "code_execution"],
            tool_policy_map={
                "google_search": "browser",
                "code_execution": "shell",
            },
            required_keys=["google_api_key"],
            supported_providers=["google"],
            install_hint={
                "pip_package": "google-adk",
                "pip_spec": "pocketpaw[google-adk]",
                "verify_import": "google.adk",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._runner: Any = None
        self._sessions: dict[str, str] = {}  # session_key -> session_id
        self._custom_tools: list | None = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            import google.adk  # noqa: F401

            self._sdk_available = True
            logger.info("Google ADK SDK ready")
        except ImportError:
            logger.warning("Google ADK not installed — pip install 'pocketpaw[google-adk]'")
            return

        # Set API key env var for ADK
        api_key = self.settings.google_api_key
        if api_key:
            os.environ["GOOGLE_API_KEY"] = api_key
        # Disable Vertex AI — use direct API key auth
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

    def _build_custom_tools(self) -> list:
        """Lazily build and cache PocketPaw custom tools as ADK FunctionTool wrappers."""
        if self._custom_tools is not None:
            return self._custom_tools
        try:
            from pocketpaw.agents.tool_bridge import build_adk_function_tools

            self._custom_tools = build_adk_function_tools(self.settings)
        except Exception as exc:
            logger.debug("Could not build custom tools: %s", exc)
            self._custom_tools = []
        return self._custom_tools

    def _build_mcp_toolsets(self) -> list:
        """Build ADK McpToolset instances from PocketPaw MCP config."""
        try:
            from google.adk.tools.mcp_tool import McpToolset
            from google.adk.tools.mcp_tool.mcp_session_manager import (
                SseConnectionParams,
                StdioConnectionParams,
            )
            from mcp import StdioServerParameters
        except ImportError:
            logger.debug("MCP dependencies not available for ADK")
            return []

        try:
            from pocketpaw.mcp.config import load_mcp_config
        except ImportError:
            return []

        configs = load_mcp_config()
        if not configs:
            return []

        policy = ToolPolicy(
            profile=self.settings.tool_profile,
            allow=self.settings.tools_allow,
            deny=self.settings.tools_deny,
        )

        toolsets: list = []
        for cfg in configs:
            if not policy.is_mcp_server_allowed(cfg.name):
                logger.info("MCP server '%s' blocked by tool policy", cfg.name)
                continue
            try:
                if cfg.transport == "stdio":
                    toolset = McpToolset(
                        connection_params=StdioConnectionParams(
                            server_params=StdioServerParameters(
                                command=cfg.command,
                                args=cfg.args or [],
                                env=cfg.env,
                            ),
                        ),
                    )
                    toolsets.append(toolset)
                elif cfg.transport in ("sse", "http"):
                    if cfg.url:
                        toolset = McpToolset(
                            connection_params=SseConnectionParams(
                                url=cfg.url,
                                headers=cfg.headers or {},
                            ),
                        )
                        toolsets.append(toolset)
            except Exception as exc:
                logger.debug("Skipping MCP server %s: %s", cfg.name, exc)

        logger.info("Built %d MCP toolsets for ADK", len(toolsets))
        return toolsets

    def _get_runner(self, instruction: str, tools: list):
        """Create or reuse the InMemoryRunner."""
        from google.adk.agents import LlmAgent
        from google.adk.runners import InMemoryRunner

        model = self.settings.google_adk_model or "gemini-3-pro-preview"

        agent = LlmAgent(
            name="PocketPaw",
            model=model,
            instruction=instruction,
            tools=tools,
        )

        runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)
        return runner

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

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not self._sdk_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Google ADK not installed.\n\nInstall with: pip install 'pocketpaw[google-adk]'"
                ),
            )
            return

        self._stop_flag = False

        try:
            from google.genai import types

            instruction = system_prompt or "You are PocketPaw, a helpful AI assistant."

            # Build tools: custom PocketPaw tools + MCP toolsets
            tools = self._build_custom_tools() + self._build_mcp_toolsets()

            # Session management: reuse sessions for multi-turn, seed history on first call
            user_id = "pocketpaw_user"
            is_new_session = session_key is not None and session_key not in self._sessions

            if is_new_session and history:
                instruction = self._inject_history(instruction, history)
            elif not session_key and history:
                instruction = self._inject_history(instruction, history)

            runner = self._get_runner(instruction, tools)

            # Create or reuse session
            if session_key and session_key in self._sessions:
                session_id = self._sessions[session_key]
            else:
                import uuid

                session_id = str(uuid.uuid4())
                if session_key:
                    self._sessions[session_key] = session_id

            # Ensure session exists
            await runner.session_service.create_session(
                app_name=_APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )

            # Build user message
            user_message = types.Content(
                role="user",
                parts=[types.Part(text=message)],
            )

            turn_count = 0
            max_turns = self.settings.google_adk_max_turns
            saw_partial = False  # track whether we received streaming chunks

            # Enable SSE streaming so the LLM streams token-by-token
            try:
                from google.adk.agents.run_config import RunConfig, StreamingMode

                run_config = RunConfig(streaming_mode=StreamingMode.SSE)
            except ImportError:
                run_config = None

            run_kwargs: dict[str, Any] = {
                "user_id": user_id,
                "session_id": session_id,
                "new_message": user_message,
            }
            if run_config is not None:
                run_kwargs["run_config"] = run_config

            async for event in runner.run_async(**run_kwargs):
                if self._stop_flag:
                    break

                if max_turns and turn_count >= max_turns:
                    yield AgentEvent(
                        type="error",
                        content=f"Max turns ({max_turns}) reached — stopping.",
                    )
                    break

                if not event.content or not event.content.parts:
                    continue

                # Dedup streaming: with SSE, ADK yields partial=True chunks
                # followed by a final partial=False/None event with the
                # complete text.  Emit partials for real-time streaming and
                # skip the final duplicate.
                is_partial = getattr(event, "partial", None) is True

                for part in event.content.parts:
                    if self._stop_flag:
                        break

                    if part.text:
                        if is_partial:
                            # Streaming chunk — emit immediately
                            saw_partial = True
                            yield AgentEvent(type="message", content=part.text)
                        elif saw_partial:
                            # Final event after partials — skip (duplicate)
                            # Reset for next LLM turn (e.g. after tool use)
                            saw_partial = False
                        else:
                            # Non-streaming mode — no partials seen, emit text
                            yield AgentEvent(type="message", content=part.text)

                    elif part.function_call:
                        turn_count += 1
                        fc = part.function_call
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Using {fc.name}...",
                            metadata={
                                "name": fc.name,
                                "input": dict(fc.args) if fc.args else {},
                            },
                        )

                    elif part.function_response:
                        fr = part.function_response
                        output = str(fr.response) if fr.response else ""
                        yield AgentEvent(
                            type="tool_result",
                            content=output[:200],
                            metadata={"name": fr.name or "tool"},
                        )

            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error("Google ADK error: %s", e)
            yield AgentEvent(type="error", content=f"Google ADK error: {e}")

    async def stop(self) -> None:
        self._stop_flag = True

    async def get_status(self) -> dict[str, Any]:
        return {
            "backend": "google_adk",
            "available": self._sdk_available,
            "running": not self._stop_flag,
            "active_sessions": len(self._sessions),
            "model": self.settings.google_adk_model or "gemini-3-pro-preview",
        }


# Backward compatibility alias
GeminiCLIBackend = GoogleADKBackend
