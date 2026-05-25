# ee/pocketpaw_ee/foresight/llm/adapter.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Two backend implementations for v0.1:
#
#   1. ClaudeCodeBackend — the thin Claude Code SDK ↔ CAMEL BaseModelBackend
#      adapter described in RFC 08 §6.4. Sits *under* the SDK's loop and
#      presents a minimal ``await backend.complete(prompt: str) -> str``
#      surface. v0.1 keeps the body small (~120 LOC budget per RFC) and
#      lazy-imports ``claude_agent_sdk`` so the foresight module imports
#      cleanly even without the SDK installed (the OSS install path).
#
#   2. DeterministicFakeBackend — used by tests + the smoke runner so
#      the v0.1 PR's CI doesn't depend on ANTHROPIC_API_KEY. Produces
#      deterministic responses that the persona parser handles cleanly.
#
# The LiteLLM fallback (RFC §6.4) lives in a follow-up PR — its file
# placeholder is documented in the module __init__.py.

from __future__ import annotations

import asyncio
from typing import Any, Protocol


class BackendProtocol(Protocol):
    """The minimal backend surface ``SoulSeededPersona`` requires.

    Anything exposing ``async def complete(prompt: str) -> str`` is a
    valid backend. This is the v0.1 surface; v1.0 broadens to CAMEL's
    full ``BaseModelBackend.run(messages, response_format, tools)``
    shape, with this `complete` becoming a convenience wrapper.
    """

    async def complete(self, prompt: str) -> str:  # pragma: no cover — protocol
        ...


class ClaudeCodeBackend:
    """Adapt Claude Code SDK to the v0.1 backend protocol.

    v0.1 keeps the surface deliberately narrow — ``complete(prompt)``
    drives a single SDK turn and returns the assistant's final text.
    The SDK still owns the loop (tool calls, memory hydration, sub-agent
    spawns happen inside that turn), preserving the persona's actual
    runtime behavior (RFC §6.4 fidelity-floor requirement).

    The semaphore guards against burst concurrency when ``ForesightWorld.tick()``
    fans out to N personas; v0.1 default is 128 (the Sonnet-tier value
    from RFC §6.4). The tier-pool builder that constructs the per-tier
    semaphores (Sonnet 128 / Haiku 256 / vLLM unbounded) lands in v1.0.

    The ``client_factory`` is injected so tests can hand in a stub
    factory without monkey-patching ``claude_agent_sdk``. Production
    callers can leave it ``None`` to get the default factory which
    lazy-imports the SDK on first call.
    """

    def __init__(
        self,
        *,
        client_factory: Any | None = None,
        max_concurrent: int = 128,
        model: str | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self._client_factory = client_factory
        self._sem = asyncio.Semaphore(max_concurrent)
        self._model = model  # reserved for v1.0 tier-pool tagging

    async def complete(self, prompt: str) -> str:
        """One SDK turn → assistant's final text.

        The SDK leak surface to watch (RFC §15.3): the SDK is agent-loop-
        shaped, not chat-completion-shaped. If we hit unexpected event
        streams or non-text terminal messages here, swap to the LiteLLM
        fallback (one-line config change at v1.0).
        """
        async with self._sem:
            client = await self._build_client()
            try:
                # Lazy-imported SDK exposes ``query(prompt) -> AsyncIterator[events]``.
                # We drain to the terminal event and return its text payload.
                async with client:
                    response = await client.query(prompt=prompt)
                    return await self._await_terminal(response)
            except Exception:
                raise

    async def _build_client(self) -> Any:
        """Resolve the SDK client. Factory wins; otherwise lazy-import.

        v0.1 imports ``claude_agent_sdk.ClaudeSDKClient`` only on
        first call — the foresight module must remain import-safe
        on machines that don't have the SDK (the OSS install path).
        """
        if self._client_factory is not None:
            client = self._client_factory()
            if asyncio.iscoroutine(client):
                client = await client
            return client
        try:
            from claude_agent_sdk import ClaudeSDKClient  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — depends on install
            raise RuntimeError(
                "ClaudeCodeBackend requires the claude_agent_sdk package. "
                "Install with `uv sync --dev --group ee` or pass a "
                "client_factory at construction time."
            ) from exc
        return ClaudeSDKClient()

    @staticmethod
    async def _await_terminal(response: Any) -> str:
        """Drain the SDK's event stream until the terminal event;
        return the assistant's final text.

        v0.1 handles two shapes:
          - response is an async iterator of events with ``.text`` payloads
          - response is already the final string (some SDK versions)
        """
        if isinstance(response, str):
            return response
        if hasattr(response, "__aiter__"):
            final = ""
            async for event in response:
                text = getattr(event, "text", None) or getattr(event, "content", None) or ""
                if isinstance(text, str) and text:
                    final = text  # keep the last; SDK emits incremental + final
            return final
        # Some SDK versions return a dict-shaped final response.
        if isinstance(response, dict):
            return str(response.get("text") or response.get("content") or "")
        return str(response)


class DeterministicFakeBackend:
    """A backend that produces deterministic, parser-friendly responses.

    Used by tests + the smoke runner so v0.1 CI doesn't depend on
    network or API keys. Each ``complete`` call returns a single line:

        ``action=<verb>; rationale=<short phrase>; put=<key>:<value>``

    The default behavior cycles through a small action vocabulary so
    multi-tick smoke runs produce varied state mutations the world
    can apply. Callers can override ``responses`` to script specific
    behavior in tests.
    """

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        default_action: str = "observe",
    ) -> None:
        self._responses = list(responses or [])
        self._default_action = default_action
        self._call_count = 0

    async def complete(self, prompt: str) -> str:  # noqa: ARG002 — prompt unused in fake
        idx = self._call_count
        self._call_count += 1
        if self._responses:
            return self._responses[idx % len(self._responses)]
        # Default rotation: observe → propose → confirm. Keys collide
        # by design so the world's last-writer-wins logic gets exercised.
        verbs = ["observe", "propose", "confirm", "amend", "approve"]
        verb = verbs[idx % len(verbs)]
        return f"action={verb}; rationale=tick-{idx}; put=last_action:{verb}"

    @property
    def call_count(self) -> int:
        return self._call_count
