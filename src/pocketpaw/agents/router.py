"""Agent Router - routes to the selected agent backend.

Changes:
  - 2026-02-14: Graceful ImportError handling when a backend dep is missing.
  - 2026-02-02: Added claude_agent_sdk_full for 2-layer architecture.
  - 2026-02-02: Simplified - removed 2-layer mode (SDK has built-in execution).
  - 2026-02-02: Added pocketpaw_native - custom orchestrator with OI executor.
  - 2026-02-02: RE-ENABLED claude_agent_sdk - now uses official SDK properly!
                claude_code still disabled (homebrew pyautogui approach).
"""

import logging
from collections.abc import AsyncIterator

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)

# Backends that are currently DISABLED (kept for future integration)
DISABLED_BACKENDS = {"claude_code"}  # claude_agent_sdk is now ENABLED!


class AgentRouter:
    """Routes agent requests to the selected backend.

    ACTIVE backends:
    - claude_agent_sdk: Official Claude Agent SDK with all built-in tools (RECOMMENDED)
    - pocketpaw_native: PocketPaw's own brain + Open Interpreter hands
    - open_interpreter: Standalone Open Interpreter (local/cloud LLMs)

    DISABLED backends (for future use):
    - claude_code: Homebrew Claude + pyautogui (needs work)
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._agent = None
        self._initialize_agent()

    def _initialize_agent(self) -> None:
        """Initialize the selected agent backend."""
        from pocketpaw.llm.client import resolve_llm_client

        backend = self.settings.agent_backend

        # Check if backend is disabled
        if backend in DISABLED_BACKENDS:
            logger.warning(f"⚠️ Backend '{backend}' disabled → using claude_agent_sdk")
            backend = "claude_agent_sdk"

        # Log Ollama usage
        llm = resolve_llm_client(self.settings)
        if llm.is_ollama:
            logger.info(
                "🦙 Ollama provider detected (%s) — using %s backend with local model",
                llm.ollama_host,
                backend,
            )
        if llm.is_openai_compatible:
            logger.info(
                "🔗 OpenAI-compatible provider detected (%s) — using %s backend",
                llm.openai_compatible_base_url,
                backend,
            )

        try:
            if backend == "claude_agent_sdk":
                from pocketpaw.agents.claude_sdk import ClaudeAgentSDKWrapper

                self._agent = ClaudeAgentSDKWrapper(self.settings)
                logger.info(
                    "🚀 [bold green]Claude Agent SDK[/] ─ Bash, WebSearch, WebFetch, Read, Write"
                )

            elif backend == "pocketpaw_native":
                from pocketpaw.agents.pocketpaw_native import PocketPawOrchestrator

                self._agent = PocketPawOrchestrator(self.settings)
                logger.info("🧠 [bold blue]PocketPaw Native[/] ─ Anthropic + Open Interpreter")

            elif backend == "open_interpreter":
                from pocketpaw.agents.open_interpreter import OpenInterpreterAgent

                self._agent = OpenInterpreterAgent(self.settings)
                logger.info(
                    "🤖 [bold yellow]Open Interpreter[/] ─ Local/Cloud LLMs [dim](experimental)[/]"
                )

            else:
                logger.warning(f"Unknown backend: {backend} → using claude_agent_sdk")
                from pocketpaw.agents.claude_sdk import ClaudeAgentSDKWrapper

                self._agent = ClaudeAgentSDKWrapper(self.settings)
        except ImportError as exc:
            install_hints = {
                "claude_agent_sdk": "pip install claude-agent-sdk",
                "pocketpaw_native": "pip install 'pocketpaw[native]'",
                "open_interpreter": "pip install 'pocketpaw[native]'",
            }
            hint = install_hints.get(backend, "pip install pocketpaw")
            logger.error(
                f"Could not load '{backend}' backend — missing dependency: {exc}. "
                f"Install it with: {hint}"
            )

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Run the agent with the given message.

        Args:
            message: User message to process.
            system_prompt: Dynamic system prompt from AgentContextBuilder.
            history: Recent session history as list of {"role": ..., "content": ...} dicts.

        Yields dicts with:
          - type: "message", "tool_use", "tool_result", "error", "done"
          - content: string content
          - metadata: optional dict with tool info (name, input)
        """
        if not self._agent:
            yield {"type": "error", "content": "❌ No agent initialized"}
            yield {"type": "done", "content": ""}
            return

        async for chunk in self._agent.run(message, system_prompt=system_prompt, history=history):
            yield chunk

    async def stop(self) -> None:
        """Stop the agent."""
        if self._agent:
            await self._agent.stop()
