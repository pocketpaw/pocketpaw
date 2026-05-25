# ee/pocketpaw_ee/foresight/llm/adapter.py
# Updated: 2026-05-25 (feat/foresight-v03-calibration) — PR 3:
#   - Wired LiteLLMFallbackBackend to the real ``litellm.acompletion``
#     proxy. PR 2 shipped this as a stub; PR 3 makes it operational
#     so the tier-pool builder can route the long-tail (Llama-3.1-8B
#     via vLLM/Modal) and the per-scenario fallback path the SDK
#     leakage mitigation calls out (RFC §6.4 + §15.3).
#   - Added ``translate_camel_tools_to_sdk_overrides(tools)`` — the
#     CAMEL FunctionTool → Claude Code SDK Permissions translator
#     PR 2 flagged as open follow-up. The translator filters CAMEL
#     tool specs into a Permissions dict the SDK consumes; tools
#     without a known shape are dropped with a debug log rather than
#     crashing the run.
#   - Both ClaudeCodeBackend.run + LiteLLMFallbackBackend.run now
#     honor ``tools`` by translating them into the SDK Permissions
#     overrides (no-op when the tool list is empty, preserving the
#     PR 2 contract).
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
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    # CAMEL types — only imported for type-checking. At runtime the
    # adapter accepts any object that quacks like a BaseMessage so we
    # stay importable on machines without camel-ai installed.
    from camel.messages import BaseMessage  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


# --- CAMEL FunctionTool → Claude Code SDK Permissions translator -----
#
# RFC §6.4 promises a translator pass so CAMEL tool specs (which
# OASIS's ``SocialAgent`` constructs via ``get_openai_function_list``)
# get mapped to the SDK's ``Permissions`` overrides — which is how the
# SDK gates which built-in tools the model is allowed to call.
#
# CAMEL FunctionTool shape (camel.toolkits.function_tool.FunctionTool):
#   - ``.func`` — the underlying callable; ``func.__name__`` is the
#     OpenAI tool name (also `func.__name__`).
#   - ``.openai_tool_schema`` (or equiv) — the OpenAI tool-call schema.
#
# Claude Code SDK Permissions shape (claude_agent_sdk.Permissions):
#   - A dict like ``{"allow": ["Read", "Bash"], "deny": []}``.
#
# Translation strategy: pass-through tool names that match SDK built-in
# tool names (Bash, Read, Write, Glob, Grep, etc.); drop unknown
# CAMEL-side tool names with a debug log. The translator is intentionally
# conservative — a tool name we don't recognize is safer to drop than
# to forward and have the SDK reject mid-run.


# The set of SDK built-in tool names PR 3 knows how to whitelist.
# Source: claude_agent_sdk's built-in tool registry (Bash / Read /
# Write / Edit / Glob / Grep / NotebookEdit / WebFetch / WebSearch /
# Skill / Task). Subset can be added per-scenario via the
# ``allowed_sdk_tools`` argument below.
_SDK_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "NotebookEdit",
        "Read",
        "Skill",
        "Task",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)


def translate_camel_tools_to_sdk_overrides(
    tools: list[Any] | None,
    *,
    allowed_sdk_tools: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Translate a CAMEL FunctionTool list into Claude Code SDK
    Permissions overrides.

    Args:
        tools: list of CAMEL ``FunctionTool``-shaped objects, OR
            OpenAI-shaped tool-call dicts (the wire format that lands
            inside ``run()``'s ``tools`` arg). Heterogeneous lists are
            accepted; per-entry shape detection picks the right path.
            ``None`` or ``[]`` returns ``{}`` (no overrides).
        allowed_sdk_tools: optional whitelist of SDK built-in tool
            names to keep. Defaults to ``_SDK_BUILTIN_TOOLS`` (the
            full known set).

    Returns:
        A dict of the form ``{"allow": [<tool_name>, ...]}`` that the
        SDK consumes via ``ClaudeSDKClient(permissions=...)``. Returns
        ``{}`` when the input list is empty (no override).

    The translator is best-effort: tools whose name isn't a recognized
    SDK built-in are skipped with a debug log. Per-scenario callers
    that need to gate custom MCP tools should layer their own
    permissions on top of what this function returns.
    """
    if not tools:
        return {}
    # ``or`` would treat an empty set as falsy and fall through to the
    # default — but an explicit empty whitelist means "allow nothing".
    whitelist = allowed_sdk_tools if allowed_sdk_tools is not None else _SDK_BUILTIN_TOOLS
    allowed: list[str] = []
    skipped: list[str] = []
    for tool in tools:
        name = _extract_tool_name(tool)
        if name is None:
            skipped.append(repr(tool)[:40])
            continue
        if name in whitelist:
            if name not in allowed:
                allowed.append(name)
        else:
            skipped.append(name)
    if skipped:
        logger.debug(
            "translate_camel_tools_to_sdk_overrides: skipped %d tools not in SDK built-in set: %s",
            len(skipped),
            skipped,
        )
    if not allowed:
        return {}
    return {"allow": list(allowed)}


def _extract_tool_name(tool: Any) -> str | None:
    """Pull a tool name from either a CAMEL FunctionTool or an OpenAI-
    shaped tool-call dict.

    CAMEL FunctionTool: ``tool.func.__name__``.
    OpenAI dict: ``tool["function"]["name"]`` or ``tool["name"]``.
    """
    func = getattr(tool, "func", None)
    if func is not None:
        name = getattr(func, "__name__", None)
        if isinstance(name, str) and name:
            return name
    if isinstance(tool, dict):
        # OpenAI tool-call format: {"type": "function", "function": {"name": ...}}
        fn = tool.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name:
                return name
        name = tool.get("name")
        if isinstance(name, str) and name:
            return name
    return None


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

    async def complete(
        self,
        prompt: str,
        *,
        permissions_overrides: dict[str, Any] | None = None,
    ) -> str:
        """One SDK turn → assistant's final text.

        ``permissions_overrides`` (PR 3): if non-empty, passed to the
        SDK client factory so the client constructs with the override
        applied for THIS turn. The base client (no overrides) is reused
        when the override is empty / ``None`` — keeps the cheap path
        cheap.

        The SDK leak surface to watch (RFC §15.3): the SDK is agent-loop-
        shaped, not chat-completion-shaped. If we hit unexpected event
        streams or non-text terminal messages here, swap to the LiteLLM
        fallback (PR 3 wires that fallback to real
        ``litellm.acompletion``).
        """
        async with self._sem:
            client = await self._build_client(permissions_overrides=permissions_overrides)
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

        PR 3 behavior:
          - ``tools`` is now translated to Claude Code SDK Permissions
            overrides via ``translate_camel_tools_to_sdk_overrides``.
            The resulting dict is stashed on the backend so
            ``_build_client`` can pass it when constructing the SDK
            client. Empty / unrecognized tool lists become a no-op
            override (preserves the PR 2 contract).
          - ``response_format`` remains a no-op — the SDK doesn't
            expose a JSON-schema-coerce knob at the prompt boundary.
          - The message list is flattened: system messages become the
            SDK's system prompt, the final user message becomes the
            turn input, intermediate turns become prior-context prose.
        """
        # Translate the CAMEL tool list into SDK Permissions overrides
        # for THIS run (does not mutate persistent backend state).
        overrides = translate_camel_tools_to_sdk_overrides(tools)
        prompt = self._compose_prompt(messages)
        final = await self.complete(prompt, permissions_overrides=overrides)
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

    async def _build_client(self, *, permissions_overrides: dict[str, Any] | None = None) -> Any:
        """Resolve the SDK client. Factory wins; otherwise lazy-import.

        ``permissions_overrides`` (PR 3): when non-empty, attempted as
        a constructor kwarg on the SDK client. Some SDK versions don't
        accept ``permissions`` directly — we fall back to plain
        construction if so, and log at debug. The factory path always
        wins; tests inject their own factory and don't need to thread
        overrides through.

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
        if permissions_overrides:
            try:
                # The SDK's ClaudeSDKClient may or may not accept a
                # ``permissions=`` kwarg depending on the installed
                # version. We try-except + ignore the mypy call-arg
                # error because the runtime fallback covers older
                # SDKs cleanly.
                return ClaudeSDKClient(permissions=permissions_overrides)  # type: ignore[call-arg]
            except TypeError:
                logger.debug(
                    "ClaudeSDKClient does not accept `permissions` kwarg; "
                    "falling back to default construction. Tools: %s",
                    permissions_overrides.get("allow"),
                )
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
    """LiteLLM proxy — the RFC §6.4 fallback path.

    PR 3 wires the actual ``litellm.acompletion`` proxy. The fallback
    serves three roles in the engine:

      1. **SDK leak insurance.** If the Claude Code SDK's abstraction
         leaks at scale (unexpected event streams, non-text terminal
         messages, SDK-side throttling that doesn't honor our
         semaphore), the runtime swap is to replace
         ``ClaudeCodeBackend`` with this class — same surface,
         direct API access.
      2. **Tier-pool tail backend.** The captain-locked tier mix
         (5% Sonnet / 15% Haiku / 80% Llama-3.1-8B vLLM, RFC §10) uses
         LiteLLM to route the tail to vLLM / Modal / Together / any
         OpenAI-compatible endpoint. The ``model`` argument carries the
         LiteLLM provider tag (e.g. ``"hosted_vllm/meta-llama/Llama-3.1-8B"``).
      3. **Cross-provider eval.** Same scenario can run on Anthropic
         today and Bedrock-hosted Mistral tomorrow — Cognition
         Swappable (True IS Spec point 3 / RFC 08 §14.3).

    The implementation is intentionally thin: ``complete(prompt)``
    flattens to a one-message OpenAI-shape and proxies through
    ``litellm.acompletion``. ``run(messages, ...)`` honors the CAMEL
    surface that ``oasis.SocialAgent(model=...)`` calls into.

    Construction:
      - ``model``: LiteLLM model identifier. Required for ``run`` to
        make a real call; ``complete`` falls back to a sensible default
        for the Llama tail (``"ollama/llama3.1:8b"``) if not provided.
      - ``base_url`` (optional): override the API base — useful for
        self-hosted vLLM endpoints.
      - ``api_key`` (optional): provider key; falls back to env vars
        per LiteLLM's own resolution (``ANTHROPIC_API_KEY``,
        ``OPENAI_API_KEY``, etc.).
      - ``max_concurrent``: semaphore guard (mirrors
        ``ClaudeCodeBackend``).
      - ``extra_kwargs``: kwargs forwarded to ``litellm.acompletion``
        (e.g. ``temperature``, ``max_tokens``).
    """

    BACKEND_AVAILABLE = True
    """PR 3 wires the real proxy — the flag flips to True."""

    DEFAULT_TAIL_MODEL = "ollama/llama3.1:8b"
    """The dev-mode default tail model. Production tier-pool callers
    pass an explicit ``model=`` (e.g. the vLLM endpoint tag).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_concurrent: int = 256,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._sem = asyncio.Semaphore(max_concurrent)
        self._extra_kwargs = dict(extra_kwargs or {})

    async def complete(
        self,
        prompt: str,
        *,
        permissions_overrides: dict[str, Any] | None = None,  # noqa: ARG002 — surface parity with ClaudeCodeBackend
    ) -> str:
        """One LiteLLM call → assistant's text.

        Constructs a one-message OpenAI-shape call and pulls
        ``choices[0].message.content`` from the response. The
        ``permissions_overrides`` arg is accepted for surface parity
        with ``ClaudeCodeBackend.complete`` (LiteLLM doesn't gate
        tools the same way; tool routing happens at the response
        layer if the model emits a ``tool_calls`` block).
        """
        model = self._model or self.DEFAULT_TAIL_MODEL
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._api_key:
            kwargs["api_key"] = self._api_key
        kwargs.update(self._extra_kwargs)
        async with self._sem:
            try:
                from litellm import acompletion  # noqa: PLC0415 — lazy import
            except ImportError as exc:  # pragma: no cover — depends on install
                raise RuntimeError(
                    "LiteLLMFallbackBackend requires the litellm package. "
                    "Install with `uv sync --dev` (litellm is in the dev "
                    "deps)."
                ) from exc
            response = await acompletion(**kwargs)
        return self._extract_text(response)

    async def run(
        self,
        messages: list[Any],
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """CAMEL ``BaseModelBackend``-shaped surface. Translates the
        message list directly to LiteLLM's chat-completion call.

        Unlike ``ClaudeCodeBackend.run``, the LiteLLM path can honor
        ``tools`` natively — LiteLLM accepts the OpenAI tool-call
        schema and the response carries ``tool_calls`` when the model
        emits them. ``response_format`` is also forwarded (some
        providers honor ``{"type": "json_object"}``).
        """
        model = self._model or self.DEFAULT_TAIL_MODEL
        litellm_messages = self._messages_to_litellm(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": litellm_messages,
        }
        if response_format:
            kwargs["response_format"] = response_format
        if tools:
            # LiteLLM consumes the OpenAI tool-call schema directly.
            # CAMEL FunctionTool entries get unwrapped to their wire
            # form; OpenAI-shaped dicts pass through unchanged.
            kwargs["tools"] = [self._tool_to_litellm(t) for t in tools]
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._api_key:
            kwargs["api_key"] = self._api_key
        kwargs.update(self._extra_kwargs)
        async with self._sem:
            try:
                from litellm import acompletion  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("LiteLLMFallbackBackend requires the litellm package.") from exc
            response = await acompletion(**kwargs)
        return self._response_to_camel(response)

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Pull the assistant text out of a LiteLLM response.

        Handles two shapes:
          - object response with ``.choices[0].message.content``
          - dict response with ``["choices"][0]["message"]["content"]``
        """
        if hasattr(response, "choices"):
            choices = response.choices
            if choices:
                msg = getattr(choices[0], "message", None)
                if msg is not None:
                    return str(getattr(msg, "content", "") or "")
        if isinstance(response, dict):
            choices = response.get("choices") or []
            if choices:
                msg = choices[0].get("message", {})
                return str(msg.get("content", "") or "")
        return str(response)

    @staticmethod
    def _messages_to_litellm(messages: list[Any]) -> list[dict[str, str]]:
        """Map a CAMEL ``BaseMessage`` list to LiteLLM's chat-shape.

        - System messages → ``{"role": "system", ...}``
        - User messages → ``{"role": "user", ...}``
        - Assistant messages → ``{"role": "assistant", ...}``
        """
        out: list[dict[str, str]] = []
        for msg in messages:
            content = getattr(msg, "content", None) or str(msg)
            role = ClaudeCodeBackend._role_tag(msg).lower()
            if role == "system":
                out.append({"role": "system", "content": content})
            elif role == "assistant":
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "user", "content": content})
        return out

    @staticmethod
    def _tool_to_litellm(tool: Any) -> dict[str, Any]:
        """Translate a CAMEL ``FunctionTool`` (or OpenAI-shaped dict) to
        the wire format LiteLLM expects.

        For OpenAI-shaped dicts, pass through. For CAMEL FunctionTool,
        pull ``openai_tool_schema`` if available; fall back to a
        minimal stub.
        """
        if isinstance(tool, dict):
            return dict(tool)
        # CAMEL FunctionTool has ``get_openai_tool_schema()`` on recent
        # versions; older versions expose ``.openai_tool_schema``.
        schema = getattr(tool, "get_openai_tool_schema", None)
        if callable(schema):
            try:
                return schema()
            except Exception:  # noqa: BLE001 — fall back to attribute
                pass
        schema_attr = getattr(tool, "openai_tool_schema", None)
        if isinstance(schema_attr, dict):
            return dict(schema_attr)
        # Last-resort stub so LiteLLM doesn't crash.
        func = getattr(tool, "func", None)
        name = getattr(func, "__name__", None) or "unknown_tool"
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    @staticmethod
    def _response_to_camel(response: Any) -> dict[str, Any]:
        """Format a LiteLLM response as a CAMEL chat-completion dict.

        LiteLLM responses are already OpenAI-shaped; we normalize to a
        dict so callers don't need to introspect provider-specific
        attribute names.
        """
        if isinstance(response, dict):
            return response
        # Object-shape — convert via .__dict__ or model_dump.
        if hasattr(response, "model_dump"):
            try:
                return response.model_dump()  # pydantic-style
            except Exception:  # noqa: BLE001 — fall back to dict_ shape
                pass
        if hasattr(response, "dict") and callable(response.dict):
            try:
                return response.dict()
            except Exception:  # noqa: BLE001
                pass
        # Best effort: walk choices manually.
        choices_out: list[dict[str, Any]] = []
        for ch in getattr(response, "choices", []) or []:
            msg = getattr(ch, "message", None)
            choices_out.append(
                {
                    "index": getattr(ch, "index", 0),
                    "message": {
                        "role": getattr(msg, "role", "assistant"),
                        "content": getattr(msg, "content", "") or "",
                        "tool_calls": getattr(msg, "tool_calls", []) or [],
                    },
                    "finish_reason": getattr(ch, "finish_reason", "stop"),
                }
            )
        return {
            "id": getattr(response, "id", "litellm-response"),
            "object": "chat.completion",
            "choices": choices_out,
            "usage": getattr(response, "usage", {}),
        }
