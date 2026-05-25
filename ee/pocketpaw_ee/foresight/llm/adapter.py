# ee/pocketpaw_ee/foresight/llm/adapter.py
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — PR 2 adds:
#   - ClaudeCodeBackend.run(messages, response_format, tools) — the
#     CAMEL BaseModelBackend-shaped surface PR 3 will pass to OASIS's
#     SocialAgent constructor (it accepts BaseModelBackend instances).
#     The v0.1 complete() method stays as a convenience entrypoint for
#     SoulSeededPersona.decide()'s prompt-only call site.
#   - LiteLLMFallbackBackend — a stub that PR 3 wires up. Defined now
#     so the adapter module exposes the surface RFC §6.4 promises;
#     calling its complete/run today raises NotImplementedError with a
#     clear PR 3 pointer.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Two backend implementations for v0.1:
#
#   1. ClaudeCodeBackend — the thin Claude Code SDK ↔ CAMEL BaseModelBackend
#      adapter described in RFC 08 §6.4. Sits *under* the SDK's loop and
#      presents a minimal ``await backend.complete(prompt: str) -> str``
#      surface plus a CAMEL-shaped ``run(messages, response_format, tools)``
#      surface that PR 3 hooks into OASIS's SocialAgent. v0.1 keeps the
#      body small (~120 LOC budget per RFC) and lazy-imports
#      ``claude_agent_sdk`` so the foresight module imports cleanly
#      even without the SDK installed (the OSS install path).
#
#   2. DeterministicFakeBackend — used by tests + the smoke runner so
#      the v0.1 PR's CI doesn't depend on ANTHROPIC_API_KEY. Produces
#      deterministic responses that the persona parser handles cleanly.
#
#   3. LiteLLMFallbackBackend — stub per RFC §6.4. PR 3 wires it to
#      proxy Anthropic API directly when the Claude Code SDK's
#      abstraction leaks at scale.

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    # CAMEL types — only imported for type-checking. At runtime the
    # adapter accepts any object that quacks like a BaseMessage so we
    # stay importable on machines without camel-ai installed.
    from camel.messages import BaseMessage  # type: ignore[import-not-found]


class BackendProtocol(Protocol):
    """The minimal backend surface ``SoulSeededPersona`` requires.

    Anything exposing ``async def complete(prompt: str) -> str`` is a
    valid backend. This is the v0.1 surface; the CAMEL-shaped
    ``run(messages, response_format, tools)`` surface is the v1.0
    target for OASIS-style SocialAgent integration, and is now
    implemented on ClaudeCodeBackend (PR 2) as the second surface.
    """

    async def complete(self, prompt: str) -> str:  # pragma: no cover — protocol
        ...


class ClaudeCodeBackend:
    """Adapt Claude Code SDK to the v0.1 backend protocol AND the CAMEL
    BaseModelBackend-shaped ``run`` surface.

    Two surfaces:

      - ``complete(prompt: str) -> str`` — the v0.1 surface
        ``SoulSeededPersona.decide`` uses. Drives a single SDK turn
        and returns the assistant's final text.
      - ``run(messages, response_format=None, tools=None)`` — the
        CAMEL BaseModelBackend-shaped surface PR 3 will pass to OASIS's
        ``SocialAgent.__init__(model=...)``. Flattens the message list
        into a single prompt (the SDK doesn't carry conversation
        history across queries) and returns a CAMEL chat-completion
        dict so downstream parsing is unchanged.

    The SDK still owns the loop (tool calls, memory hydration, sub-agent
    spawns happen inside that turn), preserving the persona's actual
    runtime behavior (RFC §6.4 fidelity-floor requirement).

    The semaphore guards against burst concurrency when
    ``ForesightWorld.tick()`` fans out to N personas; v0.1 default is
    128 (the Sonnet-tier value from RFC §6.4). The tier-pool builder
    that constructs the per-tier semaphores (Sonnet 128 / Haiku 256 /
    vLLM unbounded) lands in v1.0.

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

    # --- v0.1 convenience surface (used by SoulSeededPersona) ----------

    async def complete(self, prompt: str) -> str:
        """One SDK turn → assistant's final text.

        The SDK leak surface to watch (RFC §15.3): the SDK is agent-loop-
        shaped, not chat-completion-shaped. If we hit unexpected event
        streams or non-text terminal messages here, swap to the LiteLLM
        fallback (one-line config change at v1.0).
        """
        async with self._sem:
            client = await self._build_client()
            # Lazy-imported SDK exposes ``query(prompt) -> AsyncIterator[events]``.
            # We drain to the terminal event and return its text payload.
            async with client:
                response = await client.query(prompt=prompt)
                return await self._await_terminal(response)

    # --- CAMEL BaseModelBackend-shaped surface (used by PR 3 OASIS wiring) -

    async def run(
        self,
        messages: list[BaseMessage] | list[Any],
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """One turn against the CAMEL surface. Hands the message tail
        to the SDK; awaits the SDK's terminal response; returns a CAMEL
        chat-completion-style dict so downstream parsers (SocialAgent's
        ``perform_action_by_llm``) consume it unchanged.

        v0.2 behavior:
          - ``response_format`` and ``tools`` are NOT enforced. The SDK
            owns tool routing inside its own loop, so passing CAMEL
            FunctionTool specs here is a no-op at the adapter layer.
            PR 3 will close this gap by translating CAMEL tool specs
            into SDK ``Permissions`` overrides where appropriate.
          - The message list is flattened: system messages become the
            SDK's system prompt, the final user message becomes the
            turn input, intermediate turns become prior-context prose.
        """
        prompt = self._compose_prompt(messages)
        final = await self.complete(prompt)
        return self._to_camel_response(final)

    def _compose_prompt(self, messages: list[Any]) -> str:
        """Flatten a CAMEL ``BaseMessage`` list into a single prompt
        string. v0.2 keeps the contract minimal: walk messages in
        order, extract ``content`` (or ``str(msg)`` as a fallback),
        and join with double newlines. The SDK doesn't carry a
        conversation history across queries, so a single text blob is
        the cleanest contract.

        Heuristic for role detection: messages with a ``role_name`` or
        ``role_type`` attribute starting with ``system`` are emitted
        first as ``[SYSTEM] ...``; everything else falls under
        ``[USER] ...`` or ``[ASSISTANT] ...``. PR 3 will swap this for
        CAMEL's own ``BaseMessage.to_dict()`` shape once we depend on
        CAMEL at runtime.
        """
        if not messages:
            return ""
        parts: list[str] = []
        for msg in messages:
            content = getattr(msg, "content", None) or str(msg)
            role = self._role_tag(msg)
            parts.append(f"[{role}] {content}".strip())
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _role_tag(msg: Any) -> str:
        """Map a CAMEL ``BaseMessage`` (or a ``BaseMessage``-shaped stub)
        to a SYSTEM / USER / ASSISTANT tag for the flattened prompt.

        Two channels of evidence are consulted, ``role_name`` first then
        ``role_type``. CAMEL's ``BaseMessage.role_name`` is a free-form
        string (commonly "User" / "Assistant" / "System"); ``role_type``
        is the ``RoleType`` enum (``RoleType.USER`` etc.). We're tolerant
        across both because OASIS constructs BaseMessage via
        ``make_user_message`` / ``make_assistant_message`` factories that
        set role_name but not always role_type.
        """
        role_name = (getattr(msg, "role_name", "") or "").lower()
        if "system" in role_name:
            return "SYSTEM"
        if "assistant" in role_name:
            return "ASSISTANT"
        if "user" in role_name:
            return "USER"
        # No role_name signal; fall back to role_type enum stringification.
        # CAMEL's RoleType enum stringifies to e.g. 'RoleType.USER'.
        rt = str(getattr(msg, "role_type", None) or "").upper()
        if "ASSISTANT" in rt:
            return "ASSISTANT"
        if "SYSTEM" in rt:
            return "SYSTEM"
        return "USER"

    @staticmethod
    def _to_camel_response(final: str) -> dict[str, Any]:
        """Format final text as a CAMEL chat-completion-style dict.

        CAMEL's ``BaseModelBackend.run`` callers expect an OpenAI-shaped
        ``ChatCompletion``-like dict with ``choices[0].message.content``.
        We mirror the shape closely enough that
        ``SocialAgent.perform_action_by_llm``'s downstream parsing
        works. The ``tool_calls`` field is empty at v0.2 — tool routing
        happens inside the SDK's own loop, not as a chat-completion
        artefact the way CAMEL native backends emit it. PR 3 will close
        this gap with a translator pass.
        """
        return {
            "id": "claude-code-sdk-turn",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final,
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

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

    PR 2 addition: ``run(messages, ...)`` returns a CAMEL chat-completion
    dict wrapping the same deterministic text — lets PR 3 swap the
    deterministic fake into OASIS's SocialAgent for substrate-level
    integration tests.
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

    async def run(
        self,
        messages: list[Any],  # noqa: ARG002 — fake doesn't read messages
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """CAMEL-shaped surface — wraps the deterministic text from
        ``complete`` in a chat-completion dict. The fake doesn't care
        about the message list or tool specs; they're accepted for
        signature parity with ``ClaudeCodeBackend.run``.
        """
        text = await self.complete("ignored")
        return ClaudeCodeBackend._to_camel_response(text)

    @property
    def call_count(self) -> int:
        return self._call_count


class LiteLLMFallbackBackend:
    """Stub for the LiteLLM fallback path RFC §6.4 calls out.

    If the Claude Code SDK's abstraction leaks at scale (unexpected
    event streams, non-text terminal messages, SDK-side throttling
    that doesn't honor our semaphore), the runtime swap is to replace
    ``ClaudeCodeBackend`` with this class, which proxies Anthropic's
    chat-completion API directly via ``litellm.acompletion``.

    PR 3 wires the actual proxy. v0.2 keeps the interface alive so:
      - the tier-pool builder (RFC §7.3) can iterate over fallback
        slots without an ImportError, and
      - downstream callers can introspect ``BACKEND_AVAILABLE`` and
        wire conditional fallback behavior before PR 3 lands.
    """

    BACKEND_AVAILABLE = False

    def __init__(self, *, model: str | None = None, **_: Any) -> None:
        self._model = model

    async def complete(self, prompt: str) -> str:  # noqa: ARG002
        raise NotImplementedError(
            "LiteLLMFallbackBackend is a stub at v0.2 of RFC 08. PR 3 wires "
            "the actual litellm.acompletion proxy. Use ClaudeCodeBackend or "
            "DeterministicFakeBackend until then."
        )

    async def run(
        self,
        messages: list[Any],  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "LiteLLMFallbackBackend is a stub at v0.2 of RFC 08. PR 3 wires "
            "the actual litellm.acompletion proxy. Use ClaudeCodeBackend or "
            "DeterministicFakeBackend until then."
        )
