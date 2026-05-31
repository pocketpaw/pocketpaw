# backend_adapter.py — Adapter that makes PocketPaw's agent backends
# usable as a knowledge_base CompilerBackend.
#
# Created: 2026-04-06
# This bridges the standalone knowledge-base package with PocketPaw's
# agent registry, so KB compilation uses whatever LLM backend is active.

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PocketPawCompilerBackend:
    """CompilerBackend adapter that delegates to PocketPaw's active agent backend.

    Implements the knowledge_base.compiler.CompilerBackend protocol:
        async def complete(prompt: str, system_prompt: str = "") -> str

    Uses the agent registry to get the current backend (Claude SDK, OpenAI, etc.)
    and streams a response, concatenating all message chunks.
    """

    def __init__(self, backend_name: str = "", model: str = "") -> None:
        self._backend_name = backend_name
        self._model = model

    async def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Send a prompt to the active PocketPaw backend and return full response."""
        from pocketpaw.agents.registry import get_backend_class
        from pocketpaw.config import Settings

        settings = Settings.load()
        backend_name = self._backend_name or settings.agent_backend

        if self._model:
            if "claude" in backend_name:
                settings.claude_sdk_model = self._model
            elif "openai" in backend_name:
                settings.openai_model = self._model

        backend_cls = get_backend_class(backend_name)
        if not backend_cls:
            logger.warning("KB compiler backend '%s' not available", backend_name)
            return ""

        agent = backend_cls(settings)
        chunks: list[str] = []

        try:
            sys_prompt = system_prompt or "You are a knowledge compiler. Output only valid JSON."
            async for event in agent.run(prompt, system_prompt=sys_prompt):
                if getattr(event, "type", "") == "message":
                    content = getattr(event, "content", "")
                    if content:
                        chunks.append(str(content))
                elif getattr(event, "type", "") == "done":
                    break
        finally:
            await agent.stop()

        return "".join(chunks).strip()
