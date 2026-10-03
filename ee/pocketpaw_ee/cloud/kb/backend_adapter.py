# backend_adapter.py — Adapter that makes PocketPaw's agent backends
# usable as a knowledge_base CompilerBackend.
#
# Bridges the standalone knowledge-base package (and other one-shot callers,
# such as the lens run overview) with PocketPaw's agent registry, so the call
# uses whatever LLM backend is active. An unavailable backend raises
# RuntimeError rather than returning "", so callers that treat any failure as
# fatal report what actually went wrong. ``tools_enabled=False`` runs the turn
# with no tools and no MCP servers; a backend whose ``run`` cannot take that
# switch is refused (RuntimeError) rather than silently run WITH tools.

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

    async def complete(
        self, prompt: str, system_prompt: str = "", *, tools_enabled: bool = True
    ) -> str:
        """Send a prompt to the active PocketPaw backend and return full response.

        ``tools_enabled=False`` is for prompts carrying untrusted text: the
        backend gets no tools and no MCP servers, so injected instructions have
        nothing to act with."""
        from pocketpaw.agents.backend import _accepts_tools_enabled_kwarg
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
            raise RuntimeError(
                f"agent backend {backend_name!r} is not available for KB compilation "
                "(not registered in the agent registry)"
            )

        run_kwargs: dict[str, bool] = {}
        if not tools_enabled:
            if not _accepts_tools_enabled_kwarg(backend_cls.run):
                raise RuntimeError(f"agent backend {backend_name!r} cannot run with tools disabled")
            run_kwargs["tools_enabled"] = False

        agent = backend_cls(settings)
        chunks: list[str] = []

        try:
            sys_prompt = system_prompt or "You are a knowledge compiler. Output only valid JSON."
            async for event in agent.run(prompt, system_prompt=sys_prompt, **run_kwargs):
                if getattr(event, "type", "") == "message":
                    content = getattr(event, "content", "")
                    if content:
                        chunks.append(str(content))
                elif getattr(event, "type", "") == "done":
                    break
        finally:
            await agent.stop()

        return "".join(chunks).strip()
