# Pockets Builder — Design Document

Created: 2026-05-01
Status: Draft — awaiting captain approval before implementation

---

## 1. Problem Statement

Pocket creation today is exclusively gated behind in-process MCP servers registered by the `claude_agent_sdk` backend (`src/pocketpaw/agents/sdk_mcp_pocket.py`). Every other backend — `codex_cli`, `openai_agents`, `ollama`, `copilot_sdk`, `deep_agents`, `opencode` — has no path to create or update a pocket from within a chat stream because those backends cannot consume Claude SDK MCP registrations. Beyond the backend-lock problem, the creation logic is scattered across three separate layers: the OSS bus loop (`agents/loop.py` `_publish_pocket_event` / `_create_pocket_and_session`), the cloud SSE router (`chat/agent_service.py` `build_context_block` + `_CLOUD_POCKET_TOOL_PREAMBLE`), and the pocket entity itself (`pockets/agent_context.py` `create_pocket_for_agent`).

The captain's principle is direct: "we don't need skill or mcp — we are natively integrating it. We are not exposing it to any other agent apps." PocketPaw owns the user session, the runtime, and the intent. Pocket creation should be a native platform capability that uses an LLM provider for exactly one job — turning the user's natural-language request into a validated `PocketSpec` via structured output — and then calls the existing `pockets.service` layer to persist it. The MCP abstraction adds friction, limits backend compatibility, and makes the creation path invisible to the rest of the system.

---

## 2. Goals and Non-Goals

### Goals

- Pocket creation and update work on every agent backend, not just `claude_agent_sdk`.
- All creation logic lives in one module: `ee/cloud/pockets/builder/`.
- The module is unit-testable with a fake provider stub, no Mongo required for spec-generation tests.
- The SSE stream surfaces creation progress incrementally: `intent.detected` → `spec.building` → `pocket.created` (or `error`).
- The builder uses whatever provider keys the workspace already has configured in `Settings`; it introduces no new env vars.
- The OSS bus path (`loop.py`) is kept alive for one release to avoid breaking non-cloud consumers.
- The module follows the 4-file shape rule (`domain.py`, `dto.py`, `service.py`, `prompts.py`/`providers.py`) per `ee/cloud/CLAUDE.md`.

### Non-Goals

- The builder does not replace the REST `POST /pockets` endpoint. That path is for direct programmatic creation and stays unchanged.
- The builder does not implement a general-purpose "agent native tool" framework. It is specific to pocket intent and not extensible by third parties. The captain explicitly rejected the tool/MCP abstraction.
- The builder does not handle widget-level incremental edits during an existing pocket session (that remains the territory of `pockets/agent_context.py` read/write helpers which stay in place for the interaction path).
- No new pip extras. The builder uses provider clients already present in the cloud install.

---

## 3. Module Shape

```
ee/cloud/pockets/builder/
    __init__.py       # public re-exports: run_intent_from_message, BuilderEvent,
                      # IntentKind, BuilderResult
    domain.py         # PocketSpec, WidgetSpec, PocketUpdatePatch, IntentKind,
                      # BuilderResult, BuilderEvent — frozen dataclasses/Pydantic
    dto.py            # BuildRequest, BuildResponse, IntentDetectionResult —
                      # internal Pydantic schemas used between detect/build steps
    prompts.py        # INTENT_CLASSIFIER_SYSTEM, SPEC_BUILDER_SYSTEM,
                      # UPDATE_BUILDER_SYSTEM — prompt constant strings
    providers.py      # structured_call() — provider-agnostic structured-output
                      # adapter for Anthropic / OpenAI-compatible / Ollama / Codex
    service.py        # detect_intent(), build_pocket_spec(), build_update_patch(),
                      # run_intent_from_message() — the public API surface
```

The existing `ee/cloud/pockets/` files (`domain.py`, `dto.py`, `service.py`, `router.py`, `agent_context.py`) are unchanged by this PR. The builder sits alongside them as a sub-package, not a replacement.

---

## 4. File-by-File API Surface

### 4.1 `builder/domain.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class IntentKind(StrEnum):
    CREATE   = "pocket_create"
    UPDATE   = "pocket_update"
    NONE     = "none"       # not a pocket intent; fall through to normal run


@dataclass(frozen=True)
class WidgetSpec:
    name: str
    type: str                            # metric | chart | table | feed | etc.
    icon: str = ""
    color: str = ""
    span: str = "col-span-1"
    data_source_type: str = "static"
    config: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    props: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    data: Any = None
    assigned_agent: str | None = None


@dataclass(frozen=True)
class PocketSpec:
    """Validated pocket spec produced by the builder.

    Maps 1:1 to ``pockets.dto.CreatePocketRequest``.  The builder produces
    this; ``service.py:run_intent_from_message`` converts it to a
    ``CreatePocketRequest`` and calls ``pockets.service.create``.
    """
    name: str
    description: str = ""
    type: str = "custom"                 # UISpec category / pocket type
    icon: str = ""
    color: str = ""
    visibility: str = "workspace"
    ripple_spec: dict[str, Any] | None = None
    widgets: tuple[WidgetSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PocketUpdatePatch:
    """Partial patch spec produced when intent is ``pocket_update``."""
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    ripple_spec: dict[str, Any] | None = None


@dataclass(frozen=True)
class BuilderResult:
    """Terminal value from ``run_intent_from_message``."""
    intent: IntentKind
    pocket_id: str | None = None        # set on successful create/update
    pocket_view: dict[str, Any] | None = None   # agent-view dict, set on success
    spec: PocketSpec | None = None      # the validated spec that was applied
    error: str | None = None            # human-readable on failure


# ---------------------------------------------------------------------------
# SSE event objects yielded by run_intent_from_message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuilderEvent:
    """Typed value the SSE handler yields as an SSE frame.

    ``name`` maps to the SSE ``event:`` field. ``data`` maps to ``data:``
    (JSON-serialised by the router). The router converts each ``BuilderEvent``
    to ``_sse(ev.name, ev.data)`` identically to all other SSE events.

    Defined event names:
      intent.detected  — intent classification finished; data: {intent, confidence}
      spec.building    — LLM call for spec in flight; data: {}
      pocket.created   — Mongo write succeeded; data: {pocket_id, pocket}
      pocket.updated   — Mongo write succeeded (update path); data: {pocket_id}
      error            — builder failure; data: {code, message}
    """
    name: str
    data: dict[str, Any]
```

### 4.2 `builder/dto.py`

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BuildRequest(BaseModel):
    """Input to ``detect_intent`` and ``build_pocket_spec``.

    Carries everything the builder needs from the SSE request context without
    importing ``ScopeContext`` (which would create a cycle with ``chat``).
    """
    user_message: str
    workspace_id: str
    user_id: str
    session_mongo_id: str | None = None
    pocket_id: str | None = None        # set when editing an existing pocket
    provider: str                       # "anthropic" | "openai" | "ollama" | ...
    model: str | None = None            # override; None = provider default


class IntentDetectionResult(BaseModel):
    """Schema that the classifier LLM call must return (structured output)."""
    intent: str = Field(
        description="One of: pocket_create, pocket_update, none",
        pattern="^(pocket_create|pocket_update|none)$",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    # Optional hint the classifier can surface to speed up spec building
    pocket_name_hint: str | None = None
    pocket_type_hint: str | None = None


class BuildResponse(BaseModel):
    """Final result shape returned by service-layer callers that want a
    plain dict instead of iterating the async generator."""
    intent: str
    pocket_id: str | None = None
    pocket_view: dict[str, Any] | None = None
    error: str | None = None
```

### 4.3 `builder/prompts.py`

This file holds three module-level string constants. No logic.

`INTENT_CLASSIFIER_SYSTEM` — a short system prompt (< 200 words) that instructs the classifier to identify whether the user wants to create a new pocket, update an existing one, or neither. It lists the signals from `_POCKET_CREATION_CONTEXT` and `_POCKET_INTERACTION_CONTEXT` in condensed form. The schema it must emit is `IntentDetectionResult`.

`SPEC_BUILDER_SYSTEM` — the consolidated system prompt for new pocket creation. This replaces `_POCKET_CREATION_CONTEXT` (from `pocketpaw/api/v1/pockets.py`) as the authoritative prompt for structured PocketSpec generation. It instructs the LLM to return a JSON object matching `PocketSpec` (name, description, type, icon, color, ripple_spec). It includes the UISpec v1.0 format guide inline (stripped from the existing `_POCKET_CREATION_CONTEXT` constant) and hard rules: no Bash bridge, no MCP, no chat-bubble inline spec.

`UPDATE_BUILDER_SYSTEM` — the system prompt for updating an existing pocket. Instructs the LLM to return a `PocketUpdatePatch` object containing only the fields to change. References the existing pocket's ripple_spec shape. Replaces the update-path guidance currently embedded in `_POCKET_INTERACTION_CONTEXT`.

The existing constants in `pocketpaw/api/v1/pockets.py` (`_POCKET_CREATION_CONTEXT`, `_POCKET_INTERACTION_CONTEXT`) and in `agent_service.py` (`_CLOUD_POCKET_TOOL_PREAMBLE`) are candidates for deletion in follow-up PRs (see section 10).

### 4.4 `builder/providers.py`

Single public function:

```python
async def structured_call(
    provider: str,
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    settings: Settings | None = None,
) -> BaseModel:
    """Call an LLM provider with structured-output constraints.

    Returns a validated ``schema`` instance. Raises ``ProviderError`` on:
    - HTTP/API failure (non-retried)
    - Two consecutive Pydantic validation failures

    Never raises on the first parse failure — retries once with a correction
    message appended: "Your previous response was not valid JSON matching the
    schema. Please return only the JSON object."

    Args:
        provider: One of "anthropic", "openai", "openai_compatible", "ollama",
                  "litellm", "openrouter", "codex_cli", or "other".
        schema:   A Pydantic BaseModel subclass whose JSON schema is used as
                  the structured output constraint.
        messages: Standard OpenAI-format message list [{"role", "content"}, ...].
        model:    Provider-specific model override; falls back to Settings defaults
                  when None.
        settings: Injected Settings instance; loads via Settings.load() when None.
    """
    ...
```

Private helpers (not exported):

```python
async def _anthropic_call(
    schema: type[BaseModel],
    messages: list[dict],
    model: str,
    api_key: str,
) -> BaseModel: ...
# Uses tool_use with strict input_schema derived from schema.model_json_schema().
# The tool name is "emit_result". Returns tool_use.input validated by schema.


async def _openai_call(
    schema: type[BaseModel],
    messages: list[dict],
    model: str,
    base_url: str | None,
    api_key: str | None,
) -> BaseModel: ...
# Uses response_format={"type": "json_schema", "json_schema": {...}}.
# Covers: openai, openai_compatible, openrouter, litellm proxy.


async def _ollama_native_call(
    schema: type[BaseModel],
    messages: list[dict],
    model: str,
    host: str,
) -> BaseModel: ...
# Uses Ollama /api/chat with format=schema.model_json_schema() (Ollama >=0.5).


async def _plain_text_call(
    schema: type[BaseModel],
    messages: list[dict],
    provider: str,
    settings: Settings,
    model: str | None,
) -> BaseModel: ...
# Used for codex_cli, copilot_sdk, deep_agents, opencode when no native
# structured-output API is available. Appends to the user message:
# "Respond with a single JSON object matching this schema: <schema_json>".
# Parses the response with json.loads() + schema.model_validate().
# On first failure, appends correction and retries once.
```

Provider dispatch table (inside `structured_call`):

| `provider` value | Dispatch |
|-----------------|----------|
| `anthropic` | `_anthropic_call` with `settings.anthropic_api_key` |
| `openai` | `_openai_call` with `settings.openai_api_key`, no base_url |
| `openai_compatible` | `_openai_call` with `settings.openai_compatible_api_key` + `settings.openai_compatible_base_url` |
| `openrouter` | `_openai_call` with `settings.openrouter_api_key` + `https://openrouter.ai/api/v1` |
| `litellm` | `_openai_call` with `settings.litellm_api_key` + `settings.litellm_api_base` |
| `ollama` | `_ollama_native_call` with `settings.ollama_host` |
| `codex_cli` | `_plain_text_call` |
| any other | `_plain_text_call` |

Model defaults when caller passes `model=None`:

| `provider` | Default model |
|-----------|--------------|
| `anthropic` | `settings.anthropic_model` (default `claude-haiku-4-5-20251001`) — use Haiku not Sonnet; spec generation is fast and cheap |
| `openai` | `settings.openai_model` |
| `openai_compatible` | `settings.openai_compatible_model` |
| `openrouter` | `settings.openrouter_model` |
| `litellm` | `settings.litellm_model` |
| `ollama` | `settings.ollama_model` |
| `codex_cli` | `settings.codex_cli_model` |

**Key implementation note on Anthropic**: The classifier call and spec-builder call both go through `_anthropic_call` using `tool_use` with `tool_choice={"type": "tool", "name": "emit_result"}` to force exactly one structured response. This is more reliable than `response_format` (which Anthropic does not support) and more reliable than plain-text parsing.

**Key implementation note on Ollama**: `_ollama_native_call` uses `httpx` (already a core dep) to call `POST /api/chat` directly rather than importing the `ollama` Python package, avoiding a new dependency.

**`ProviderError` class** (defined in `providers.py`):

```python
class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None: ...
    code: str       # e.g. "no_key", "api_error", "parse_failed_twice", "timeout"
    message: str
    retryable: bool
```

### 4.5 `builder/service.py`

```python
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from pocketpaw.config import Settings

from ee.cloud.pockets.builder.domain import (
    BuilderEvent,
    BuilderResult,
    IntentKind,
    PocketSpec,
    PocketUpdatePatch,
)
from ee.cloud.pockets.builder.dto import BuildRequest, IntentDetectionResult


async def detect_intent(req: BuildRequest) -> IntentDetectionResult:
    """Classify whether ``req.user_message`` is a pocket create, update, or
    unrelated request.

    Makes a single structured-output LLM call using the INTENT_CLASSIFIER_SYSTEM
    prompt and ``IntentDetectionResult`` schema. Always completes; never yields
    SSE events. Callers use the result to decide whether to proceed to
    ``build_pocket_spec`` / ``build_update_patch`` or fall through to a normal
    agent run.

    Raises ``ProviderError`` on provider failure (caller handles as error event).
    """
    ...


async def build_pocket_spec(req: BuildRequest) -> PocketSpec:
    """Turn the user's natural-language request into a validated ``PocketSpec``.

    Makes a single structured-output LLM call using the SPEC_BUILDER_SYSTEM
    prompt and ``PocketSpec`` schema. Validates the returned spec via Pydantic
    (one retry on parse failure as per ``providers.structured_call`` contract).

    Raises ``ProviderError`` on provider or validation failure.
    """
    ...


async def build_update_patch(req: BuildRequest) -> PocketUpdatePatch:
    """Turn the user's natural-language edit request into a ``PocketUpdatePatch``.

    Same contract as ``build_pocket_spec`` but uses ``UPDATE_BUILDER_SYSTEM``
    and ``PocketUpdatePatch`` schema. ``req.pocket_id`` must be set; this
    function does NOT load the current pocket — that is the caller's
    responsibility so the service layer can inject the current spec into the
    system prompt if desired (pass via ``req.user_message`` prefix).
    """
    ...


async def run_intent_from_message(
    req: BuildRequest,
    *,
    settings: Settings | None = None,
) -> AsyncGenerator[BuilderEvent, None]:
    """Top-level entry point. Async generator yielding ``BuilderEvent`` objects.

    Sequence:
      1. Call ``detect_intent(req)``.
      2. Yield ``BuilderEvent("intent.detected", {intent, confidence})``.
      3. If intent is ``none``: yield nothing further — caller falls through.
      4. If intent is ``pocket_create``:
         a. Yield ``BuilderEvent("spec.building", {})``.
         b. Call ``build_pocket_spec(req)``.
         c. Call ``pockets.service.agent_create(...)`` with the spec fields.
         d. Yield ``BuilderEvent("pocket.created", {pocket_id, pocket})``.
         e. Link active session to the new pocket (same logic as
            ``create_pocket_for_agent`` today).
      5. If intent is ``pocket_update``:
         a. Yield ``BuilderEvent("spec.building", {})``.
         b. Call ``build_update_patch(req)``.
         c. Call ``pockets.service.agent_update(...)`` with patch fields.
         d. Yield ``BuilderEvent("pocket.updated", {pocket_id})``.
      6. Any ``ProviderError``:
         Yield ``BuilderEvent("error", {code, message})``.

    The SSE router maps each ``BuilderEvent`` to an SSE frame and exits the
    stream after the ``pocket.created`` / ``pocket.updated`` / ``error`` event.

    Session linking (step 4e):
      Uses the same approach as ``agent_context.create_pocket_for_agent`` today:
      ``sessions.service.attach_pocket_to_session_doc(session_mongo_id, ...)``
      followed by a ``SessionUpdated`` realtime emit. Both calls are best-effort
      (log + continue on failure).

    SSE push for sidebar refresh:
      Calls ``push_sse_event("pocket_created", {...})`` via the existing
      ``agent_service.push_sse_event`` mechanism. This is the one import from
      ``ee.cloud.chat`` allowed to the service layer — it is a lazy import
      inside the function body, not a module-level dependency.

    Returns:
      AsyncGenerator yielding ``BuilderEvent`` objects. Callers iterate with
      ``async for event in run_intent_from_message(req): ...``
    """
    ...
```

### 4.6 `builder/__init__.py`

```python
from ee.cloud.pockets.builder.domain import (
    BuilderEvent,
    BuilderResult,
    IntentKind,
    PocketSpec,
    PocketUpdatePatch,
)
from ee.cloud.pockets.builder.dto import BuildRequest
from ee.cloud.pockets.builder.service import (
    build_pocket_spec,
    build_update_patch,
    detect_intent,
    run_intent_from_message,
)

__all__ = [
    "BuilderEvent",
    "BuilderResult",
    "BuildRequest",
    "IntentKind",
    "PocketSpec",
    "PocketUpdatePatch",
    "build_pocket_spec",
    "build_update_patch",
    "detect_intent",
    "run_intent_from_message",
]
```

---

## 5. Provider Abstraction Detail

The central function is `providers.structured_call(provider, schema, messages, *, model, settings)`. Its dispatch relies entirely on the `provider` string argument, which the `service.py` layer derives from the agent's backend at call time (see section 7).

### Anthropic dispatch

Uses the Messages API with `tool_use`. The schema is converted with `schema.model_json_schema()` and passed as `tools=[{"name": "emit_result", "description": "...", "input_schema": <json_schema>}]` with `tool_choice={"type": "tool", "name": "emit_result"}`. This forces the model to produce exactly one structured JSON object with no surrounding prose. The response is validated via `schema.model_validate(response.content[0].input)`. If validation fails, one correction turn is appended: `{"role": "user", "content": "Invalid response. Return only the JSON matching the schema."}` and the call is repeated. Second failure raises `ProviderError(code="parse_failed_twice")`.

Why tool_use and not system prompt + JSON extraction for Anthropic: tool_use with forced tool choice is the only Anthropic mechanism that guarantees structured output without relying on the model voluntarily emitting clean JSON. Prompt-only approaches have a ~5-10% parse failure rate on Haiku for nested schemas.

### OpenAI / OpenAI-compatible dispatch

Uses the Chat Completions API with `response_format={"type": "json_schema", "json_schema": {"name": "emit_result", "strict": True, "schema": <json_schema>}}`. This is supported by OpenAI gpt-4o+, most OpenAI-compatible endpoints (vLLM, LiteLLM proxy, OpenRouter for supported models), and Azure OpenAI. For endpoints that do not support `json_schema` response_format (they return a 400 with `unsupported_parameter`), the code catches the error and falls back to `_plain_text_call` with a logged warning.

### Ollama native dispatch

Uses `httpx.AsyncClient` to call `POST {settings.ollama_host}/api/chat` with `{"model": model, "messages": messages, "format": <json_schema>, "stream": false}`. Ollama >=0.5 supports the `format` field with a JSON Schema object. Validation follows the same try-once-retry pattern.

### Plain-text dispatch (codex_cli, copilot_sdk, deep_agents, opencode, other)

Appends to the last user message: `"\n\nRespond with a single JSON object (no markdown, no explanation) matching this schema:\n{json.dumps(json_schema, indent=2)}"`. Makes the provider's normal completion call (each provider's existing python SDK / subprocess bridge). Extracts the first `{...}` JSON block from the response with a simple bracket-depth scanner (same approach as `loop._extract_pocket_json` already in the codebase). Validates with `schema.model_validate(parsed)`. One retry on failure.

### API key acquisition

`providers.py` never reads env vars directly. It receives a `Settings` instance (passed in or loaded via `Settings.load()`) and reads the relevant fields: `settings.anthropic_api_key`, `settings.openai_api_key`, `settings.openai_compatible_api_key`, `settings.openai_compatible_base_url`, `settings.litellm_api_key`, `settings.litellm_api_base`, `settings.openrouter_api_key`, `settings.ollama_host`, `settings.ollama_model`. No new env vars are introduced.

---

## 6. Intent Detection Approach

### Recommendation: Two Calls

The builder uses two sequential LLM calls:

**Call 1 — Intent classifier** (`detect_intent`): A minimal call using the `IntentDetectionResult` schema (three fields: `intent`, `confidence`, `pocket_name_hint`). System prompt is under 200 words. Input is just the user's message. Output token budget is ~50 tokens. Always uses Haiku (or the equivalent cheapest model for the configured provider) regardless of what model the main agent backend uses. This call is fast (~300ms) and cheap (~$0.0001).

**Call 2 — Spec builder** (`build_pocket_spec` or `build_update_patch`): Only executed if Call 1 returns `intent != "none"`. Uses the full `SPEC_BUILDER_SYSTEM` prompt plus the user message. Output token budget is ~800 tokens (enough for a full UISpec with 5-8 widgets). Uses the configured model for the provider.

**Why not one call**: A single discriminated-union schema (`{intent: "...", spec?: {...}, patch?: {...}}`) is harder to validate, forces the LLM to produce all output in one pass, and bloats the schema sent to providers that need it in the request (Anthropic tool_use, OpenAI json_schema). The two-call approach is only ~200-300ms slower on the wall clock because both calls are I/O-bound. The classifier result also surfaces to the SSE stream immediately as `intent.detected`, giving the UI something to show before the spec call completes.

**Why not pre-classify on the frontend**: The `CloudAgentChatRequest.intent` field already allows the frontend to hint `pocket_create`. This hint is respected — if `ctx.intent == "pocket_create"` is already set, `run_intent_from_message` can skip Call 1 and go directly to the spec builder (saving one LLM round-trip). The builder must still run Call 2 regardless, because the frontend cannot produce a `PocketSpec`. When `ctx.intent` is `None` or missing, the builder runs Call 1 first. This means the existing frontend behavior is preserved (the sidebar sends `intent=pocket_create` when the user is in pocket-creation mode), and the builder provides a safe fallback for backends that don't carry intent hints.

---

## 7. Provider Selection

### Recommendation: Same provider as the active agent, with Haiku-grade model override

The builder uses the same provider that the active agent backend is configured to use. The `ScopeContext.target_agent_id` field is already resolved by `resolve_scope_context`; the agent doc carries the backend type (from `AgentPool.get` via the `Agent` Mongo document). The `agent_router._run_agent_stream` already has access to the resolved agent `instance`. From the instance, the backend string (`claude_agent_sdk`, `openai_agents`, etc.) maps to a provider via a simple lookup:

| Backend | Provider string for builder |
|---------|---------------------------|
| `claude_agent_sdk` with `claude_sdk_provider=anthropic` | `anthropic` |
| `claude_agent_sdk` with `claude_sdk_provider=ollama` | `ollama` |
| `openai_agents` with `openai_agents_provider=openai` | `openai` |
| `openai_agents` with `openai_agents_provider=ollama` | `ollama` |
| `openai_agents` with `openai_agents_provider=openai_compatible` | `openai_compatible` |
| `codex_cli` | `codex_cli` |
| `copilot_sdk` | `copilot_sdk` |
| `deep_agents` | derived from `settings.deep_agents_model` prefix |
| `opencode` | `opencode` |
| `ollama` (direct) | `ollama` |

**Why same provider**: The workspace already has a key for it (the main agent is working). Using a different provider would require a second key to be configured and would introduce surprise ("why is my Ollama-backend pocket builder calling Anthropic?"). The SSE stream is already bound to the correct workspace identity via `attach_agent_identity`; the builder inherits that context.

**Model override**: For the intent classifier (Call 1), the builder always requests the provider's cheapest/fastest model variant rather than the full model the agent uses. For Anthropic, that means `claude-haiku-4-5-20251001` (`settings.anthropic_model` already defaults to Haiku for non-agent workloads). For other providers, the builder uses a `_FAST_MODEL_HINTS` dict in `providers.py` that maps provider to a fast model slug; if no hint is available, it uses the same model as the configured backend. Call 2 (spec building) uses the full configured model since spec quality matters.

**Not configurable per-workspace in this version**: Workspace-level builder provider override is a nice future feature but adds settings complexity now. Adding it later is non-breaking (just add a `pocket_builder_provider` Settings field). Mark as open question in section 14.

---

## 8. Error Handling Matrix

| Failure scenario | ProviderError code | User-visible SSE event | Notes |
|-----------------|-------------------|----------------------|-------|
| No API key configured for provider | `no_key` | `error` event: "Pocket creation requires an API key for {provider}. Add one in Settings." | Check before making any call. |
| Provider returns HTTP 4xx (auth, quota, rate limit) | `api_error` | `error` event: "The AI provider returned an error ({status_code}). Check your API key or quota." | Do not expose raw error body; log it. |
| Provider returns HTTP 5xx or timeout | `api_error` | `error` event: "Could not reach the AI provider. Try again in a moment." | Retryable=True but no automatic retry in this version — let the user retry. |
| Pydantic parse fails twice (malformed JSON from provider) | `parse_failed_twice` | `error` event: "Could not generate a pocket spec from your request. Try rephrasing it." | Log the raw responses for debugging. |
| Pydantic parse fails twice on update patch | `parse_failed_twice` | `error` event: "Could not parse an update spec from your request. Try being more specific." | Same as above. |
| `pockets.service.agent_create` fails (Mongo error) | N/A (service exception) | `error` event: "Pocket was designed but could not be saved. Try again." | Log full traceback. |
| Intent is `none` (not a pocket request) | N/A | No builder events. Caller falls through to normal agent run. | This is the happy non-pocket path. |
| `req.workspace_id` or `req.user_id` is missing | `bad_request` | `error` event: "Missing session context. Refresh and try again." | Should not happen in normal flow. |

The SSE `error` event payload shape matches the existing pattern: `{"code": str, "message": str}`. The `code` is the `ProviderError.code`, prefixed `builder.` so the frontend can distinguish builder errors from agent run errors: e.g., `builder.no_key`, `builder.api_error`, `builder.parse_failed_twice`.

After any `error` event, the `_run_agent_stream` generator breaks (does not fall through to the normal agent run). The reasoning: if the user said "build me a pocket" and the builder failed, sending a normal text response from the agent is confusing. The frontend shows the error inline and lets the user retry.

**One exception**: if `detect_intent` call itself fails (provider error during classification), and `ctx.intent` is also `None`, the builder silently falls through to the normal agent run rather than erroring. The reasoning: intent classification is a best-effort pre-step; if it fails, the request is not necessarily a pocket intent, and a normal reply is better than an error for an ambiguous message.

---

## 9. Conversational Reply Policy

### Recommendation: Always emit a brief conversational chunk after successful creation

After a successful `pocket.created` event, `run_intent_from_message` yields one final `BuilderEvent` with `name="chunk"` and a brief, deterministic confirmation string assembled from the spec — no additional LLM call:

```
"Built {spec.name} — a {spec.type} pocket with {widget_count} widgets."
```

If the spec has a `ripple_spec`, append: " The canvas is ready."

**Why**: Complete silence after `pocket.created` is jarring in a chat interface. The frontend mounts the new pocket on the canvas (via `pocket_created` SSE event) but the chat thread has no message confirming what happened. A short deterministic string is fast (zero latency), costs nothing, and makes the conversation feel complete.

**Why not a full LLM-generated description**: It would require a third LLM call, adding ~500ms latency after the pocket is already created and mounted. The spec itself contains the relevant details (name, type, widget count). A template string is good enough.

The `chunk` event after `pocket.created` is accumulated into `full_text` by the existing `_run_agent_stream` loop (since the loop iterates the builder's generator the same way it would iterate pool events), so the confirmation text is persisted as the assistant message at `stream_end`. This means the conversation history carries a record of what was created, which benefits future history rehydration.

---

## 10. Migration Plan

### Phase 1 — Same PR as the new builder module

The following are safe to land in the same PR:

1. Create `ee/cloud/pockets/builder/` with all six files.
2. Rewire `agent_router._run_agent_stream` to call `builder.run_intent_from_message` when `ctx.intent == "pocket_create"` OR when builder's `detect_intent` returns `pocket_create` (see section 13 for pseudocode).
3. Add the `builder` import-linter contract block (section 11).
4. Remove `_CLOUD_POCKET_TOOL_PREAMBLE` from `agent_service.py` and update `build_context_block` to not inject MCP tool guidance when `intent == "pocket_create"` (the builder handles it; no MCP guidance needed).
5. Remove `build_context_block`'s reference to `_POCKET_CREATION_CONTEXT` for the `pocket_create` intent branch — the builder's `SPEC_BUILDER_SYSTEM` prompt replaces it.

### Phase 2 — Follow-up PR (after Phase 1 ships and is verified in staging)

These are deferred because they remove code that the OSS bus path still depends on or that is consumed by import-linter contracts that would require coordinated multi-file changes:

1. **Delete `sdk_mcp_pocket.py` `create_pocket` and `update_pocket` handlers** (keep `get_pocket`, `add_widget`, `update_widget`, `remove_widget` — those serve the pocket interaction path which is NOT being replaced in this design). The `build_pocket_context_server()` function loses `create_pocket` and `update_pocket` from its tool list. This is safe once Phase 1 lands because the builder handles creation for all backends, but the SDK still needs the read/widget-edit tools for the interaction path.

2. **Delete `loop.py` `_publish_pocket_event` and `_create_pocket_and_session`**. These are the OSS bus path. They must survive Phase 1 because non-cloud users (OSS, local dashboard) still use them. One release cycle (one minor version) after Phase 1 ships, these can be removed under a deprecation notice in the changelog.

3. **Delete `_POCKET_CREATION_CONTEXT` from `pocketpaw/api/v1/pockets.py`**. The cloud path no longer consumes it after Phase 1. The OSS local path (`dashboard.py` or the OSS agent context builder) may still reference it. Audit before deleting.

4. **Delete `_POCKET_INTERACTION_CONTEXT` from `pocketpaw/api/v1/pockets.py`**. Same rule as above — audit first.

5. **Delete `create_pocket_for_agent` from `pockets/agent_context.py`**. After Phase 1, `run_intent_from_message` in `builder/service.py` owns creation. `agent_context.py` keeps `update_pocket_for_agent`, `add_widget_for_agent`, `update_widget_for_agent`, `remove_widget_for_agent`, `fetch_pocket_for_agent` for the interaction path.

**Why the OSS loop stays for one release**: `loop.py` is tested by OSS bus tests in `tests/test_agent_loop.py` (if they exist) and is consumed by non-cloud users who may not have upgraded their frontend. Removing it in the same PR as the builder landing is a risky coordination surface.

---

## 11. Import-Linter Contract Block

Add the following to `.importlinter` (or `pyproject.toml` `[tool.importlinter]` section) in the same PR:

```ini
[importlinter:contract:pockets-builder-isolation]
name = Pockets builder — import isolation
type = forbidden

# Builder internals are not importable outside the package except via __init__.py
[importlinter:contract:pockets-builder-no-internal-leak]
name = Pockets builder — no internal import from outside
type = forbidden
source_modules =
    ee.cloud.pockets.router
    ee.cloud.pockets.dto
    ee.cloud.pockets.domain
    ee.cloud.pockets.service
    ee.cloud.pockets.agent_context
    ee.cloud.pockets.layouts
    ee.cloud.chat.agent_router
    ee.cloud.chat.agent_service
forbidden_modules =
    ee.cloud.pockets.builder.service
    ee.cloud.pockets.builder.providers
    ee.cloud.pockets.builder.prompts
    ee.cloud.pockets.builder.dto

[importlinter:contract:pockets-builder-no-chat-cycle]
name = Pockets builder — no import from ee.cloud.chat (cycle prevention)
type = forbidden
source_modules =
    ee.cloud.pockets.builder
forbidden_modules =
    ee.cloud.chat.agent_router
    ee.cloud.chat.agent_service
    ee.cloud.chat.message_service
    ee.cloud.chat.ws

[importlinter:contract:pockets-builder-allowed-upstream]
name = Pockets builder — allowed upstream imports
type = forbidden
source_modules =
    ee.cloud.pockets.builder
forbidden_modules =
    # Builder must not import cloud models directly — only through pockets.service
    ee.cloud.models.pocket
    ee.cloud.models.session
    ee.cloud.models.user
    ee.cloud.models.workspace
    # Builder must not import the MCP binding it is replacing
    pocketpaw.agents.sdk_mcp_pocket
```

**What the builder MAY import** (not enforced by import-linter, but codified here for implementers):

- `ee.cloud.pockets.service` — to call `agent_create`, `agent_update`
- `ee.cloud.pockets.domain` — for the existing `Pocket` / `Widget` value objects
- `ee.cloud.pockets.agent_context` — for `_push_replace` style SSE mutation pushes
- `ee.cloud.sessions.service` — for `attach_pocket_to_session_doc`
- `ee.cloud.realtime.emit` + `ee.cloud.realtime.events` — for `SessionUpdated`
- `pocketpaw.config.Settings` — for provider key access
- Standard library + `pydantic` + `httpx` — always allowed

**The one permitted lazy import from `ee.cloud.chat`**: `agent_service.push_sse_event` is imported lazily inside `run_intent_from_message` (inside the function body, not at module level) to push `pocket_created` onto the SSE stream. The import-linter `forbidden_modules` block above targets module-level imports and does not block this pattern. This is the established approach used by `pockets/agent_context.py` for the same call today.

---

## 12. Test Plan

### File: `tests/ee/cloud/pockets/builder/test_providers.py`

```
test_structured_call_anthropic_returns_validated_schema
    — FakeAnthropic client returns a canned tool_use block; assert model_validate succeeds.

test_structured_call_anthropic_retries_on_parse_failure
    — First response is malformed JSON; second response is valid; assert one retry.

test_structured_call_anthropic_raises_on_two_failures
    — Both responses are malformed; assert ProviderError(code="parse_failed_twice").

test_structured_call_openai_returns_validated_schema
    — FakeOpenAI client returns canned JSON in response_format shape.

test_structured_call_ollama_returns_validated_schema
    — FakeOllamaClient (httpx mock) returns canned JSON.

test_structured_call_plain_text_extracts_json_from_prose
    — Plain-text response contains JSON block surrounded by prose; assert extracted.

test_structured_call_no_key_raises_provider_error
    — Settings has no anthropic_api_key; assert ProviderError(code="no_key").

test_structured_call_api_4xx_raises_provider_error
    — FakeClient raises HTTP 401; assert ProviderError(code="api_error").
```

### File: `tests/ee/cloud/pockets/builder/test_service.py`

```
test_detect_intent_returns_create
    — FakeStructuredCallProvider returns IntentDetectionResult(intent="pocket_create");
      assert detect_intent returns that result.

test_detect_intent_returns_none_for_unrelated_message
    — FakeProvider returns IntentDetectionResult(intent="none");
      assert detect_intent intent == IntentKind.NONE.

test_build_pocket_spec_returns_valid_pocketspec
    — FakeProvider returns a canned PocketSpec dict; assert model_validate succeeds.

test_build_pocket_spec_raises_on_provider_error
    — FakeProvider raises ProviderError; assert it propagates.

test_run_intent_from_message_create_full_flow
    — FakeProvider returns classify→create; fake pockets.service.agent_create returns
      (view_dict, pocket_id, None); collect all BuilderEvent objects; assert sequence:
      [intent.detected, spec.building, pocket.created, chunk].

test_run_intent_from_message_none_intent_yields_only_intent_event
    — FakeProvider returns none intent; assert generator yields exactly one event
      (intent.detected) and then stops.

test_run_intent_from_message_error_event_on_provider_failure
    — detect_intent call raises ProviderError; assert generator yields error event.

test_run_intent_from_message_error_event_on_mongo_failure
    — pockets.service.agent_create returns (None, None, "insert failed");
      assert generator yields error event with builder.mongo_error code.

test_run_intent_skips_classify_when_intent_hint_provided
    — BuildRequest has ctx.intent=="pocket_create" pre-set via req.intent_hint field;
      FakeProvider is called once (spec only, not twice); assert classify never called.
```

### File: `tests/ee/cloud/pockets/builder/test_sse_sequence.py`

```
test_agent_router_builder_event_sequence_matches_frontend_expectation
    — Mocks pool.run to never be called; mocks run_intent_from_message to yield
      [intent.detected, spec.building, pocket.created, chunk]; drives
      _run_agent_stream end-to-end; collects all (event_name, data) tuples;
      asserts sequence: stream_start, intent.detected, spec.building,
      pocket.created, chunk, stream_end.
      This is the critical contract test — the frontend event consumer depends
      on this exact sequence.

test_agent_router_none_intent_falls_through_to_pool_run
    — run_intent_from_message yields only intent.detected(intent="none");
      assert pool.run IS called and normal chunk/stream_end follows.

test_agent_router_builder_error_stops_stream
    — run_intent_from_message yields error event; assert stream stops (stream_end
      with assistant_message_id=None, no pool.run call).
```

### File: `tests/ee/cloud/pockets/builder/test_surface_filter_regression.py`

```
test_session_surface_filter_still_applies_after_builder_write
    — Create a pocket via the builder (fake provider, real Mongo fixture from
      conftest mongo_db). Verify the created Session document has surface == "pocket"
      (not "dm" or "session") so the PR #1031 Session.surface filter continues to
      work. This is a regression guard: the builder uses
      sessions.service.attach_pocket_to_session_doc which must preserve surface.
```

### Shared fixture: `FakeStructuredCallProvider`

A `pytest` fixture in `conftest.py` that patches `providers.structured_call` with a configurable async function. Callers set `fake_provider.returns = [result1, result2]` and the fake pops from the list on each call. Raises `ProviderError` if the list is exhausted unexpectedly.

---

## 13. Rewired `_run_agent_stream` Pseudocode

The change to `agent_router.py` is a single dispatch block inserted before the `pool.run(...)` call. The existing pool.run path is the fallthrough for non-pocket intents.

```python
async def _run_agent_stream(
    ctx: ScopeContext,
    user_message_id: str,
    body: CloudAgentChatRequest,
    cancel_event: asyncio.Event,
    *,
    history: list[dict[str, str]] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:

    run_id = _new_run_id()
    session_key = session_key_for(ctx)
    pool = get_agent_pool()

    # ... (load agent instance, build scope_block, broadcast typing=True,
    #      yield stream_start, attach side channel, attach identity) ...
    # unchanged setup

    # --- BUILDER DISPATCH ---
    # Determine whether this message is a pocket-create/update intent.
    # Short-circuit conditions that skip the builder entirely:
    #   - ctx.intent == "none" (explicit non-pocket hint from a future frontend)
    #   - pocket scope AND no create/update signals (handled below by builder's
    #     detect_intent returning "none")
    # The builder always runs detect_intent unless ctx.intent is already
    # "pocket_create" or "pocket_update" (frontend pre-classified).

    _should_try_builder = True  # default: let builder classify

    if ctx.intent == "pocket_create" or ctx.intent == "pocket_update":
        # Frontend pre-classified — skip Call 1, go directly to spec building.
        _intent_hint = ctx.intent
    else:
        _intent_hint = None  # builder will run classify call

    if _should_try_builder:
        from ee.cloud.pockets.builder import run_intent_from_message, BuildRequest

        # Derive provider from the resolved agent instance's backend.
        _provider = _derive_provider_from_instance(instance, settings)

        build_req = BuildRequest(
            user_message=body.content,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            session_mongo_id=(
                ctx.scope_id if ctx.kind is ScopeKind.SESSION else None
            ),
            pocket_id=ctx.pocket_id,
            provider=_provider,
            # intent_hint skips classify call when pre-set
            # (add intent_hint field to BuildRequest)
            intent_hint=_intent_hint,
        )

        pocket_intent_seen = False
        async for builder_event in run_intent_from_message(build_req):
            # Drain side channel between builder events (same as pool.run loop)
            for ev in _drain_side_channel():
                yield ev
            if cancel_event.is_set():
                cancelled = True
                break
            if builder_event.name == "intent.detected":
                if builder_event.data.get("intent") == "none":
                    # Not a pocket intent — fall through to normal agent run.
                    break
                pocket_intent_seen = True
                yield (builder_event.name, builder_event.data)
            elif builder_event.name == "chunk":
                # Confirmation text — accumulate into full_text for persistence.
                chunk_text = builder_event.data.get("content", "")
                full_text += chunk_text
                yield ("chunk", {"content": chunk_text, "type": "text"})
            else:
                yield (builder_event.name, builder_event.data)
                if builder_event.name in ("pocket.created", "pocket.updated",
                                          "error"):
                    # Builder finished (success or failure). Do NOT fall through
                    # to pool.run — the stream ends here.
                    if builder_event.name == "error":
                        yield ("stream_end", {
                            "assistant_message_id": None,
                            "usage": {},
                            "cancelled": False,
                        })
                    else:
                        # Persist the confirmation chunk as assistant message.
                        if full_text.strip():
                            assistant_msg = await _persist_assistant_message(
                                ctx, full_text, []
                            )
                            yield ("stream_end", {
                                "assistant_message_id": str(assistant_msg.id),
                                "usage": {},
                                "cancelled": False,
                            })
                        else:
                            yield ("stream_end", {
                                "assistant_message_id": None,
                                "usage": {},
                                "cancelled": False,
                            })
                    await _broadcast_agent_typing(ctx, active=False)
                    return

        if cancelled or pocket_intent_seen:
            # Either cancelled or pocket flow handled above.
            return
        # If we fell out of the builder loop via "intent.detected(none)" break,
        # continue to the normal pool.run path below.

    # --- NORMAL AGENT RUN (unchanged) ---
    async for event in pool.run(ctx.target_agent_id, body.content, session_key, ...):
        ...
```

**`_derive_provider_from_instance` helper** (new private function in `agent_router.py`):

```python
def _derive_provider_from_instance(instance: Any, settings: Settings) -> str:
    """Map an agent pool instance to the builder provider string."""
    backend = getattr(instance, "backend", "") or ""
    if "claude" in backend or "anthropic" in backend:
        return getattr(instance, "provider", None) or "anthropic"
    if "openai" in backend:
        return getattr(instance, "provider", None) or "openai"
    if "codex" in backend:
        return "codex_cli"
    if "copilot" in backend:
        return "copilot_sdk"
    if "deep" in backend:
        # deep_agents uses "anthropic:model" or "openai:model" format
        model_str = settings.deep_agents_model or ""
        return model_str.split(":")[0] if ":" in model_str else "anthropic"
    if "opencode" in backend:
        return "opencode"
    if "ollama" in backend:
        return "ollama"
    # Safe fallback: if Anthropic key present use it, else openai, else plain-text
    if settings.anthropic_api_key:
        return "anthropic"
    if settings.openai_api_key:
        return "openai"
    return "other"
```

---

## 14. Open Questions for Captain

**14.1 Intent detection threshold**: The `IntentDetectionResult.confidence` field is returned by the classifier but the current design does not define a threshold below which the builder should fall through instead of attempting spec generation. Should we define one (e.g., `confidence < 0.5` → fall through)? Or trust the binary `intent` field and ignore confidence for routing? Confidence is useful for logging but the routing logic is simpler without a threshold gate.

**14.2 Workspace-level builder provider override**: The design locks the builder to the same provider as the active agent backend. If a workspace has `codex_cli` as the main agent but also has an Anthropic key, the builder will use `codex_cli`'s plain-text path (lower quality). Should there be a `pocket_builder_provider` Settings field (e.g., `POCKETPAW_POCKET_BUILDER_PROVIDER=anthropic`) that lets operators override this? Recommendation is to add it only after a complaint is filed — it adds config surface area for a rare edge case.

**14.3 Update path pocket context injection**: `build_update_patch` receives `req.pocket_id` but does not fetch the current pocket's `ripple_spec` to inject into the system prompt. This means the LLM generates a patch blind (knowing only what the user said, not the current state). For simple field patches (name, icon) this is fine. For `ripple_spec` updates it may produce a partial spec that drops existing widgets. Should `build_update_patch` call `pockets.service.agent_view` first and inject the current spec? The cost is one extra Mongo read and a larger prompt. Recommendation: yes, inject it, but mark this as a follow-up to keep the Phase 1 PR scope tight.

**14.4 `pocket.updated` SSE push to sidebar**: The `pocket.created` path today calls `push_sse_event("pocket_created", {...})` so the frontend mounts the new pocket in the sidebar. The update path should call `push_sse_event("pocket_mutation", {...})` to refresh the canvas. Confirm the frontend's `pocket_mutation` handler can handle a builder-sourced mutation (same payload shape as `agent_context._push_replace`)?

**14.5 OSS bus deprecation timeline**: The `loop.py` `_publish_pocket_event` / `_create_pocket_and_session` functions are marked for deletion in Phase 2. Is one minor version cycle the right timeline, or does the captain prefer to keep them indefinitely for OSS consumers who haven't migrated to the cloud path?

**14.6 `PocketSpec.widgets` vs `PocketSpec.ripple_spec`**: The spec builder prompt will instruct the LLM to produce either a `ripple_spec` (UISpec v1.0 tree) or a flat `widgets` array, but not both (they are mutually exclusive in the current render path). Should `PocketSpec` enforce this constraint with a Pydantic `model_validator` that raises if both are non-empty? Or leave it to the prompt guidance? Recommendation: add the validator — silent data corruption is worse than a loud error.
