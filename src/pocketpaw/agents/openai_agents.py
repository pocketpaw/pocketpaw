"""OpenAI Agents SDK backend for PocketPaw.

Uses the official OpenAI Agents SDK (pip install openai-agents) which provides:
- Agent/Runner abstraction with streaming
- Built-in tools: code_interpreter, file_search, computer_use
- Multi-turn conversations via SQLiteSession
- Custom model support via OpenAIChatCompletionsModel (Ollama, local LLMs)

Requires: pip install openai-agents
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)

# Session DB path — shared across all OpenAI Agents sessions
_SESSION_DB = Path.home() / ".pocketpaw" / "openai_agents_sessions.db"


class OpenAIAgentsBackend:
    """OpenAI Agents SDK backend — supports GPT models and Ollama/local via OpenAI-compat."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="openai_agents",
            display_name="OpenAI Agents SDK",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["code_interpreter", "file_search", "computer_use"],
            tool_policy_map={
                "code_interpreter": "shell",
                "file_search": "read_file",
                "computer_use": "shell",
            },
            required_keys=["openai_api_key"],
            supported_providers=["openai", "ollama", "openai_compatible"],
            install_hint={
                "pip_package": "openai-agents",
                "pip_spec": "pocketpaw[openai-agents]",
                "verify_import": "agents",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._sqlite_session_available = False
        self._sessions: dict[str, Any] = {}  # session_key -> SQLiteSession
        self._custom_tools: list | None = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            import agents  # noqa: F401

            self._sdk_available = True
            logger.info("OpenAI Agents SDK ready")
        except ImportError:
            logger.warning(
                "OpenAI Agents SDK not installed — pip install 'pocketpaw[openai-agents]'"
            )
            return

        # Check for SQLiteSession support (requires openai-agents >= 0.9.0)
        try:
            from agents.extensions.persistence import SQLiteSession  # noqa: F401

            self._sqlite_session_available = True
            logger.info("SQLiteSession available — native session management enabled")
        except ImportError:
            logger.info("SQLiteSession not available — falling back to history injection")

    def _get_or_create_session(self, session_key: str) -> Any:
        """Get or create a SQLiteSession for the given key."""
        if session_key in self._sessions:
            return self._sessions[session_key]

        from agents.extensions.persistence import SQLiteSession

        _SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
        session = SQLiteSession(str(_SESSION_DB))
        self._sessions[session_key] = session
        logger.info("Created SQLiteSession for key %s", session_key)
        return session

    @staticmethod
    def _inject_history(instructions: str, history: list[dict]) -> str:
        """Append conversation history to instructions as text."""
        lines = ["# Recent Conversation"]
        for msg in history:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"**{role}**: {content}")
        return instructions + "\n\n" + "\n".join(lines)

    @staticmethod
    def _extract_tool_name(item: Any) -> str:
        """Extract tool name from a ToolCallItem.

        ToolCallItem has a raw_item attribute containing the actual tool call.
        Different tool types have different structures for accessing the name.
        """
        try:
            # Try to access raw_item.function.name (for ResponseFunctionToolCall)
            if hasattr(item, "raw_item") and hasattr(item.raw_item, "function"):
                return item.raw_item.function.name

            # Try to access raw_item.type for other tool types (computer_use, file_search, etc.)
            if hasattr(item, "raw_item") and hasattr(item.raw_item, "type"):
                tool_type = item.raw_item.type
                # Return a human-readable name based on type
                type_names = {
                    "computer_use": "Computer",
                    "file_search": "File Search",
                    "code_interpreter": "Code Interpreter",
                }
                return type_names.get(tool_type, tool_type.replace("_", " ").title())

            # Fallback: try direct name attribute (shouldn't exist but check anyway)
            if hasattr(item, "name"):
                return item.name

            # Last resort: use description if available
            if hasattr(item, "description") and item.description:
                return item.description

        except (AttributeError, TypeError) as e:
            logger.debug("Could not extract tool name from item: %s", e)

        # Final fallback
        return "Tool"

    def _build_custom_tools(self) -> list:
        """Lazily build and cache PocketPaw custom tools as FunctionTool wrappers."""
        if self._custom_tools is not None:
            return self._custom_tools
        try:
            from pocketpaw.agents.tool_bridge import build_openai_function_tools

            self._custom_tools = build_openai_function_tools(self.settings)
        except Exception as exc:
            logger.debug("Could not build custom tools: %s", exc)
            self._custom_tools = []
        return self._custom_tools

    def _build_model(self) -> Any:
        """Build the model instance, supporting Ollama via OpenAI-compat."""
        model_name = self.settings.openai_agents_model or self.settings.openai_model or "gpt-5.2"

        # Per-backend provider setting, with fallback to global llm_provider
        provider = (
            getattr(self.settings, "openai_agents_provider", "") or self.settings.llm_provider
        )

        # If Ollama or OpenAI-compatible endpoint is configured, use OpenAIChatCompletionsModel
        if provider in ("ollama", "openai_compatible") or (
            provider == "auto" and self.settings.openai_compatible_base_url
        ):
            from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
            from openai import AsyncOpenAI

            if provider == "ollama":
                base_url = self.settings.ollama_host.rstrip("/") + "/v1"
                model_name = self.settings.ollama_model
                client = AsyncOpenAI(base_url=base_url, api_key="ollama")
            else:
                base_url = self.settings.openai_compatible_base_url
                api_key = self.settings.openai_compatible_api_key or "none"
                model_name = self.settings.openai_compatible_model or model_name
                client = AsyncOpenAI(base_url=base_url, api_key=api_key)

            return OpenAIChatCompletionsModel(model=model_name, openai_client=client)

        return model_name

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
                    "OpenAI Agents SDK not installed.\n\n"
                    "Install with: pip install 'pocketpaw[openai-agents]'"
                ),
            )
            return

        self._stop_flag = False

        try:
            from agents import Agent, Runner
            from openai.types.responses import ResponseTextDeltaEvent

            model = self._build_model()
            instructions = system_prompt or "You are PocketPaw, a helpful AI assistant."

            # Native session management via SQLiteSession:
            # - When session_key is provided and SQLiteSession is available,
            #   use the SDK's native session to manage conversation history.
            # - On the FIRST call for a given session_key (new native session),
            #   also inject history as a seed — this provides cross-backend
            #   portability when the user switches backends mid-session.
            # - On subsequent calls, the native session has accumulated turns
            #   so history injection is skipped.
            # - Without session_key, fall back to history injection.
            session = None
            is_new_session = session_key is not None and session_key not in self._sessions
            if session_key and self._sqlite_session_available:
                session = self._get_or_create_session(session_key)
                if is_new_session and history:
                    # Seed: carry over context from PocketPaw memory / prior backend
                    instructions = self._inject_history(instructions, history)
            elif history:
                # No native session — always inject
                instructions = self._inject_history(instructions, history)

            custom_tools = self._build_custom_tools()
            agent = Agent(
                name="PocketPaw",
                instructions=instructions,
                model=model,
                tools=custom_tools if custom_tools else [],
            )

            max_turns = self.settings.openai_agents_max_turns
            run_kwargs: dict[str, Any] = {
                "input": message,
            }
            if max_turns:
                run_kwargs["max_turns"] = max_turns
            if session is not None:
                run_kwargs["session"] = session
            result = Runner.run_streamed(agent, **run_kwargs)

            async for event in result.stream_events():
                if self._stop_flag:
                    break

                if event.type == "raw_response_event":
                    if isinstance(event.data, ResponseTextDeltaEvent):
                        yield AgentEvent(type="message", content=event.data.delta)

                elif event.type == "run_item_stream_event":
                    item = event.item
                    if item.type == "tool_call_item":
                        tool_name = self._extract_tool_name(item)
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Using {tool_name}...",
                            metadata={"name": tool_name, "input": {}},
                        )
                    elif item.type == "tool_call_output_item":
                        yield AgentEvent(
                            type="tool_result",
                            content=str(item.output)[:200],
                            metadata={"name": "tool"},
                        )

            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error("OpenAI Agents SDK error: %s", e)
            yield AgentEvent(type="error", content=f"OpenAI Agents error: {e}")

    async def stop(self) -> None:
        self._stop_flag = True

    async def get_status(self) -> dict[str, Any]:
        return {
            "backend": "openai_agents",
            "available": self._sdk_available,
            "running": not self._stop_flag,
            "native_sessions": self._sqlite_session_available,
            "active_sessions": len(self._sessions),
        }
