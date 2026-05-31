# Pocket Specialist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `pocket_specialist__create` tool (MCP + CLI surfaces) that any agent backend can call to create a pocket end-to-end (list → decide extend-vs-create → draft → validate → persist) using a configurable in-process backend (default `deep_agents`), with status events streamed back to the user.

**Architecture:** The specialist is an orchestration utility under `backend/ee/agent/pocket_specialist/` that spins up a fresh, isolated `AgentBackend` instance via `AgentRouter.create_isolated_backend()`, attaches three pocket-specific LangChain `StructuredTool` wrappers (list / validate / persist), runs the LLM loop with the canonical `POCKET_CREATION_PROMPT_MCP` from `ee/ripple/_pockets.py`, and emits status events through the existing realtime bus. The calling agent's prompt is rewritten in `_pockets.py` so its only path is to delegate.

**Tech Stack:** Python 3.11+, Pydantic 2, FastAPI/Beanie (cloud), LangChain `StructuredTool`, deepagents 0.5.8 (default backend), pytest + pytest-asyncio.

**Stacks on:** PR #1083 (deepagents floor `>=0.5.8`), PR #1084 (deep_agents.py Responses-API fix + skills/memory plumbing). This plan's branch is `feat/pocket-specialist`, which already has the spec doc committed.

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `backend/ee/agent/__init__.py` | Empty package marker. |
| `backend/ee/agent/pocket_specialist/__init__.py` | Re-exports `run_specialist`, `PocketSpecialistCreateInput`, `PocketSpecialistCreateOutput`. |
| `backend/ee/agent/pocket_specialist/settings.py` | `resolve_specialist_model()` helper (settings → backend-aware model id). Pure logic, no I/O. |
| `backend/ee/agent/pocket_specialist/events.py` | Status event names as a frozen enum + `emit_specialist_event()` helper that writes to `ee/cloud/_core/realtime/bus`. |
| `backend/ee/agent/pocket_specialist/tools.py` | Three `StructuredTool` factories (`make_list_pockets_tool`, `make_validate_spec_tool`, `make_persist_pocket_tool`) — each closes over `workspace_id`/`user_id` so the LLM can't pass them. |
| `backend/ee/agent/pocket_specialist/runtime.py` | `run_specialist(brief, hints, *, workspace_id, user_id, settings)` — the only public entry point. Orchestrates backend selection, tool wiring, event emission, and the persist-once safety net. |
| `backend/ee/agent/pocket_specialist/mcp_tool.py` | `build_pocket_specialist_server()` and `POCKET_SPECIALIST_TOOL_IDS` — follows the `sdk_mcp_pocket.py` pattern. Hands off to `runtime.run_specialist`. |
| `backend/ee/agent/pocket_specialist/cli_tool.py` | `cloud_pocket_specialist_create` shell command for `codex_cli` / `opencode` / `gemini_cli` backends. Hands off to `runtime.run_specialist`. |
| `backend/tests/ee/__init__.py` | Empty package marker. |
| `backend/tests/ee/agent/__init__.py` | Empty package marker. |
| `backend/tests/ee/agent/test_pocket_specialist/__init__.py` | Empty package marker. |
| `backend/tests/ee/agent/test_pocket_specialist/test_settings.py` | Settings resolution tests. |
| `backend/tests/ee/agent/test_pocket_specialist/test_events.py` | Event emission tests with a mock bus. |
| `backend/tests/ee/agent/test_pocket_specialist/test_tools.py` | Tool wrapper tests in isolation. |
| `backend/tests/ee/agent/test_pocket_specialist/test_runtime.py` | `run_specialist` end-to-end with a mocked `DeepAgentsBackend`. |
| `backend/tests/ee/agent/test_pocket_specialist/test_mcp_tool.py` | MCP server registration + handler tests. |
| `backend/tests/ee/agent/test_pocket_specialist/test_cli_tool.py` | CLI command registration + handler tests. |

### Modified files

| Path | Change |
|---|---|
| `backend/src/pocketpaw/config.py` | Add three `pocket_specialist_*` Settings fields after `deep_agents_max_turns`. |
| `backend/src/pocketpaw/agents/backend.py` | Extend `AgentBackend` Protocol with `attach_specialist_tools(tools: list)`. Default no-op via separate `BaseAgentBackend` mixin (Protocol can't have defaults). |
| `backend/src/pocketpaw/agents/router.py` | Add `AgentRouter.create_isolated_backend(backend_name, settings_override)` classmethod. |
| `backend/src/pocketpaw/agents/deep_agents.py` | Implement `attach_specialist_tools()` — extends the `_custom_tools` cache. |
| `backend/src/pocketpaw/agents/claude_sdk.py` | Register the new MCP server in `_get_mcp_servers()`. Add specialist tool ID to allowlist. |
| `backend/ee/ripple/_pockets.py` | Replace inline `STEP 1..N` pocket-creation blocks with single `STEP 0` delegation block in both `POCKET_CREATION_PROMPT_MCP` and `POCKET_CREATION_PROMPT_CLI`. |
| `backend/tests/cloud/test_pocket_prompts_single_source.py` | Extend regression test to assert (a) delegation block present, (b) legacy `STEP 1..N` blocks absent. |

---

## Task 1: Add `pocket_specialist_*` Settings fields

**Files:**
- Modify: `backend/src/pocketpaw/config.py` (after line 285, the `deep_agents_max_turns` field)
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_settings.py`

- [ ] **Step 1: Create empty package markers**

```bash
mkdir -p backend/ee/agent/pocket_specialist
mkdir -p backend/tests/ee/agent/test_pocket_specialist
```

Create `backend/ee/agent/__init__.py`, `backend/ee/agent/pocket_specialist/__init__.py`, `backend/tests/ee/__init__.py`, `backend/tests/ee/agent/__init__.py`, `backend/tests/ee/agent/test_pocket_specialist/__init__.py` — all empty files.

- [ ] **Step 2: Write the failing settings test**

Create `backend/tests/ee/agent/test_pocket_specialist/test_settings.py`:

```python
"""Pocket-specialist settings — defaults, env var resolution, model fallback."""
from pocketpaw.config import Settings


class TestPocketSpecialistSettings:
    def test_defaults(self):
        s = Settings()
        assert s.pocket_specialist_backend == "deep_agents"
        assert s.pocket_specialist_model == ""
        assert s.pocket_specialist_max_validation_retries == 3

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_BACKEND", "claude_agent_sdk")
        monkeypatch.setenv(
            "POCKETPAW_POCKET_SPECIALIST_MODEL", "openai_compatible:deepseek-v4-pro"
        )
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_MAX_VALIDATION_RETRIES", "5")
        s = Settings()
        assert s.pocket_specialist_backend == "claude_agent_sdk"
        assert s.pocket_specialist_model == "openai_compatible:deepseek-v4-pro"
        assert s.pocket_specialist_max_validation_retries == 5
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_settings.py -v
```
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'pocket_specialist_backend'`.

- [ ] **Step 4: Add the three settings fields to `config.py`**

In `backend/src/pocketpaw/config.py`, immediately after the `deep_agents_max_turns` field (the closing `)` on the line after the description), insert:

```python
    # Pocket Specialist Settings — see docs/superpowers/specs/2026-05-09-pocket-specialist-design.md
    pocket_specialist_backend: str = Field(
        default="deep_agents",
        description=(
            "Which agent backend runs the pocket specialist's LLM work. Must be a "
            "registered backend name (deep_agents, claude_agent_sdk, openai_agents, "
            "google_adk, codex_cli, opencode, copilot_sdk). Default deep_agents "
            "avoids subprocess cold-start."
        ),
    )
    pocket_specialist_model: str = Field(
        default="",
        description=(
            "Model override for the specialist run (empty = use the chosen backend's "
            "default *_model setting). provider:model format, e.g. "
            "'openai_compatible:deepseek-v4-pro' for cheap fast specs."
        ),
    )
    pocket_specialist_max_validation_retries: int = Field(
        default=3,
        description=(
            "Max draft -> validate -> revise iterations before persisting with "
            "remaining warnings. Specialist always persists; this only bounds revision."
        ),
    )
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_settings.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
cd backend && git add src/pocketpaw/config.py \
    ee/agent/__init__.py ee/agent/pocket_specialist/__init__.py \
    tests/ee/__init__.py tests/ee/agent/__init__.py \
    tests/ee/agent/test_pocket_specialist/__init__.py \
    tests/ee/agent/test_pocket_specialist/test_settings.py
git commit -m "feat(pocket-specialist): add settings fields"
```

---

## Task 2: Resolve specialist model helper

**Files:**
- Create: `backend/ee/agent/pocket_specialist/settings.py`
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_settings.py` (extend)

`resolve_specialist_model()` returns the model id for the specialist run: explicit override if set, else the chosen backend's default `*_model` setting.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/ee/agent/test_pocket_specialist/test_settings.py`:

```python
from ee.agent.pocket_specialist.settings import resolve_specialist_model


class TestResolveSpecialistModel:
    def test_explicit_override_wins(self):
        s = Settings(
            pocket_specialist_backend="deep_agents",
            pocket_specialist_model="openai_compatible:deepseek-v4-pro",
            deep_agents_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "openai_compatible:deepseek-v4-pro"

    def test_falls_back_to_backend_default_when_unset(self):
        s = Settings(
            pocket_specialist_backend="deep_agents",
            deep_agents_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "anthropic:claude-sonnet-4-6"

    def test_returns_empty_when_backend_has_no_model_setting(self):
        # opencode has opencode_model; copilot_sdk has copilot_sdk_model;
        # if a backend has none, resolver returns "" — caller must handle.
        s = Settings(pocket_specialist_backend="not_a_real_backend")
        assert resolve_specialist_model(s) == ""
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_settings.py::TestResolveSpecialistModel -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ee.agent.pocket_specialist.settings'`.

- [ ] **Step 3: Implement the resolver**

Create `backend/ee/agent/pocket_specialist/settings.py`:

```python
"""Settings resolution for the pocket specialist runtime.

Pure logic — no I/O, no side effects. Lets us test model fallback without
spinning up an actual backend.
"""

from __future__ import annotations

from pocketpaw.config import Settings


def resolve_specialist_model(settings: Settings) -> str:
    """Pick the model id for a specialist run.

    Order:
      1. ``settings.pocket_specialist_model`` if non-empty (explicit override).
      2. ``settings.<backend>_model`` for the chosen backend (e.g. ``deep_agents_model``).
      3. Empty string when the backend has no ``*_model`` field — caller must
         fall back to the backend's own internal default.
    """
    explicit = settings.pocket_specialist_model
    if explicit:
        return explicit
    field_name = f"{settings.pocket_specialist_backend}_model"
    return getattr(settings, field_name, "") or ""
```

- [ ] **Step 4: Run test to verify pass**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_settings.py -v
```
Expected: 5 passed (2 from Task 1 + 3 new).

- [ ] **Step 5: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/settings.py \
    tests/ee/agent/test_pocket_specialist/test_settings.py
git commit -m "feat(pocket-specialist): add model resolver"
```

---

## Task 3: Status events module

**Files:**
- Create: `backend/ee/agent/pocket_specialist/events.py`
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_events.py`

Status events are emitted through the existing realtime bus
(`ee/cloud/_core/realtime/bus.py`). The events module defines the names
as a frozen enum and provides a thin emit helper.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/ee/agent/test_pocket_specialist/test_events.py`:

```python
"""Pocket-specialist status events — names and emit helper."""
from unittest.mock import AsyncMock, patch

import pytest

from ee.agent.pocket_specialist.events import (
    SpecialistEvent,
    emit_specialist_event,
)


class TestSpecialistEventNames:
    def test_known_events(self):
        # Mirrors the design doc Status events table.
        assert SpecialistEvent.START.value == "specialist:start"
        assert SpecialistEvent.LISTING.value == "specialist:listing"
        assert SpecialistEvent.DECIDED.value == "specialist:decided"
        assert SpecialistEvent.DRAFTING.value == "specialist:drafting"
        assert SpecialistEvent.VALIDATING.value == "specialist:validating"
        assert SpecialistEvent.REVISING.value == "specialist:revising"
        assert SpecialistEvent.PERSISTING.value == "specialist:persisting"
        assert SpecialistEvent.DONE.value == "specialist:done"


class TestEmitSpecialistEvent:
    @pytest.mark.asyncio
    async def test_emit_writes_to_bus(self):
        with patch(
            "ee.agent.pocket_specialist.events.event_bus",
        ) as mock_bus:
            mock_bus.publish = AsyncMock()
            await emit_specialist_event(SpecialistEvent.LISTING, {})
            mock_bus.publish.assert_awaited_once()
            event = mock_bus.publish.await_args.args[0]
            assert event["type"] == "specialist:listing"
            assert event["data"] == {}

    @pytest.mark.asyncio
    async def test_emit_includes_payload(self):
        with patch(
            "ee.agent.pocket_specialist.events.event_bus",
        ) as mock_bus:
            mock_bus.publish = AsyncMock()
            await emit_specialist_event(
                SpecialistEvent.DECIDED, {"action": "create"}
            )
            event = mock_bus.publish.await_args.args[0]
            assert event["type"] == "specialist:decided"
            assert event["data"] == {"action": "create"}

    @pytest.mark.asyncio
    async def test_emit_swallows_bus_failure(self, caplog):
        with patch(
            "ee.agent.pocket_specialist.events.event_bus",
        ) as mock_bus:
            mock_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
            # Must not raise — events are best-effort.
            await emit_specialist_event(SpecialistEvent.START, {"brief": "x"})
            assert "specialist event emit failed" in caplog.text.lower()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_events.py -v
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the events module**

Create `backend/ee/agent/pocket_specialist/events.py`:

```python
"""Status events emitted during a pocket specialist run.

Best-effort fire-and-forget emission to the realtime bus. Bus failures
NEVER propagate — the specialist's work continues even when no client
is subscribed.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from ee.cloud.shared.events import event_bus

log = logging.getLogger(__name__)


class SpecialistEvent(str, Enum):
    """Status event names. Frontend consumes these as progress indicators."""

    START = "specialist:start"
    LISTING = "specialist:listing"
    DECIDED = "specialist:decided"
    DRAFTING = "specialist:drafting"
    VALIDATING = "specialist:validating"
    REVISING = "specialist:revising"
    PERSISTING = "specialist:persisting"
    DONE = "specialist:done"


async def emit_specialist_event(
    event: SpecialistEvent,
    data: dict[str, Any],
) -> None:
    """Emit a specialist status event. Best-effort — never raises."""
    payload = {"type": event.value, "data": data}
    try:
        await event_bus.publish(payload)
    except Exception as exc:  # noqa: BLE001
        log.debug("specialist event emit failed (non-fatal): %s", exc)
```

- [ ] **Step 4: Run test to verify pass**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_events.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/events.py \
    tests/ee/agent/test_pocket_specialist/test_events.py
git commit -m "feat(pocket-specialist): add status events"
```

---

## Task 4: Internal tool wrappers

**Files:**
- Create: `backend/ee/agent/pocket_specialist/tools.py`
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_tools.py`

Three `StructuredTool` factories. Each closes over `workspace_id` and
`user_id` so the LLM can never pass them as arguments — keeps multi-tenancy
enforced even if the LLM hallucinates.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/ee/agent/test_pocket_specialist/test_tools.py`:

```python
"""Specialist-internal tool wrappers — workspace closure, schema, return shape."""
from unittest.mock import AsyncMock, patch

import pytest

from ee.agent.pocket_specialist.tools import (
    make_list_pockets_tool,
    make_persist_pocket_tool,
    make_validate_spec_tool,
)


class TestListPocketsTool:
    @pytest.mark.asyncio
    async def test_closes_over_workspace_and_user(self):
        with patch(
            "ee.agent.pocket_specialist.tools._agent_list_pockets",
            new=AsyncMock(return_value=[{"id": "p1", "name": "X"}]),
        ) as mocked:
            tool = make_list_pockets_tool(
                workspace_id="ws-1", user_id="user-A"
            )
            result = await tool.ainvoke({})
            mocked.assert_awaited_once_with("ws-1", "user-A")
            assert result == [{"id": "p1", "name": "X"}]


class TestValidateSpecTool:
    def test_returns_warnings_list(self):
        tool = make_validate_spec_tool()
        # Spec with a known-bad arrow-fn expression triggers a warning.
        bad_spec = {
            "version": "1.0",
            "ui": {
                "type": "table",
                "props": {"rows": "{state.repos.map(r => r.name)}"},
            },
        }
        result = tool.invoke({"spec": bad_spec})
        assert result["ok"] is False
        assert len(result["warnings"]) > 0

    def test_clean_spec_returns_ok(self):
        tool = make_validate_spec_tool()
        good_spec = {
            "version": "1.0",
            "state": {"name": ""},
            "ui": {"type": "input", "props": {"value": "{state.name}"}},
        }
        result = tool.invoke({"spec": good_spec})
        assert result["ok"] is True
        assert result["warnings"] == []


class TestPersistPocketTool:
    @pytest.mark.asyncio
    async def test_create_path(self):
        with patch(
            "ee.agent.pocket_specialist.tools._agent_create",
            new=AsyncMock(
                return_value=({"id": "new-1", "name": "Created"}, None, None)
            ),
        ) as mocked:
            tool = make_persist_pocket_tool(
                workspace_id="ws-1", user_id="user-A"
            )
            result = await tool.ainvoke({
                "name": "Created",
                "ripple_spec": {"version": "1.0", "ui": {"type": "text"}},
            })
            mocked.assert_awaited_once()
            assert result["id"] == "new-1"

    @pytest.mark.asyncio
    async def test_update_path(self):
        with patch(
            "ee.agent.pocket_specialist.tools._agent_update",
            new=AsyncMock(
                return_value={"id": "p1", "name": "Updated"}
            ),
        ) as mocked:
            tool = make_persist_pocket_tool(
                workspace_id="ws-1", user_id="user-A"
            )
            result = await tool.ainvoke({
                "target_pocket_id": "p1",
                "ripple_spec": {"version": "1.0", "ui": {"type": "text"}},
            })
            mocked.assert_awaited_once()
            assert result["id"] == "p1"
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_tools.py -v
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the tool factories**

Create `backend/ee/agent/pocket_specialist/tools.py`:

```python
"""LangChain StructuredTool factories for the specialist's internal use.

Each factory closes over ``workspace_id`` and ``user_id``, so those are
NEVER tool arguments visible to the LLM. The LLM cannot accidentally
cross workspaces.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# Indirections so tests can patch on this module rather than reaching
# into ee.cloud directly.
from ee.cloud.pockets.agent_context import (
    create_pocket_for_agent as _agent_create,
)
from ee.cloud.pockets.agent_context import (
    update_pocket_for_agent as _agent_update,
)
from ee.cloud.pockets.service import agent_list as _agent_list_pockets
from ee.cloud.ripple_validator import validate_ripple_spec


# ------------------------------------------------------------------
# list_pockets
# ------------------------------------------------------------------

class _ListPocketsArgs(BaseModel):
    """No arguments — workspace is closed over by the factory."""


def make_list_pockets_tool(*, workspace_id: str, user_id: str) -> StructuredTool:
    async def _run() -> list[dict[str, Any]]:
        return await _agent_list_pockets(workspace_id, user_id)

    return StructuredTool.from_function(
        coroutine=_run,
        name="list_pockets",
        description=(
            "List existing pockets in the current workspace. Call this BEFORE "
            "drafting a new spec to decide whether to extend an existing pocket "
            "or create a new one. Returns a compact list of "
            "{id, name, description, type, icon, color}."
        ),
        args_schema=_ListPocketsArgs,
    )


# ------------------------------------------------------------------
# validate_spec
# ------------------------------------------------------------------

class _ValidateSpecArgs(BaseModel):
    spec: dict[str, Any] = Field(..., description="The rippleSpec to validate.")


def make_validate_spec_tool() -> StructuredTool:
    def _run(spec: dict[str, Any]) -> dict[str, Any]:
        warnings = validate_ripple_spec(spec)
        return {
            "ok": len(warnings) == 0,
            "warnings": [w.message for w in warnings],
        }

    return StructuredTool.from_function(
        func=_run,
        name="validate_spec",
        description=(
            "Validate a draft rippleSpec against the renderer's expression "
            "grammar. Returns {ok, warnings}. Re-draft and re-validate if "
            "warnings is non-empty. After max retries (default 3), persist "
            "anyway — never block the user."
        ),
        args_schema=_ValidateSpecArgs,
    )


# ------------------------------------------------------------------
# persist_pocket
# ------------------------------------------------------------------

class _PersistPocketArgs(BaseModel):
    name: str | None = Field(
        default=None,
        description="Required when creating; ignored when target_pocket_id is set.",
    )
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    ripple_spec: dict[str, Any] = Field(..., description="The validated rippleSpec.")
    target_pocket_id: str | None = Field(
        default=None,
        description="When set, updates the existing pocket. When None, creates a new one.",
    )


def make_persist_pocket_tool(*, workspace_id: str, user_id: str) -> StructuredTool:
    async def _run(
        ripple_spec: dict[str, Any],
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        target_pocket_id: str | None = None,
    ) -> dict[str, Any]:
        if target_pocket_id:
            pocket = await _agent_update(
                pocket_id=target_pocket_id,
                ripple_spec=ripple_spec,
                name=name,
                description=description,
                icon=icon,
                color=color,
            )
            return pocket
        # create path
        pocket, _id, err = await _agent_create(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name or "Untitled pocket",
            description=description or "",
            icon=icon,
            color=color,
            ripple_spec=ripple_spec,
        )
        if err:
            raise RuntimeError(f"persist failed: {err}")
        return pocket

    return StructuredTool.from_function(
        coroutine=_run,
        name="persist_pocket",
        description=(
            "Persist the rippleSpec as a new pocket OR update an existing one. "
            "Pass target_pocket_id to update; omit to create. You MUST call this "
            "exactly once before returning. Returns {id, name, description, "
            "type, icon, color}."
        ),
        args_schema=_PersistPocketArgs,
    )
```

> **NOTE:** Verify the actual signatures of `agent_list`,
> `create_pocket_for_agent`, and `update_pocket_for_agent` in
> `ee/cloud/pockets/service.py` and `ee/cloud/pockets/agent_context.py`
> before pasting this code. Adjust the return-tuple unpacking if the
> upstream signatures differ.

- [ ] **Step 4: Run test to verify pass**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_tools.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/tools.py \
    tests/ee/agent/test_pocket_specialist/test_tools.py
git commit -m "feat(pocket-specialist): add list/validate/persist tool wrappers"
```

---

## Task 5: Define `attach_specialist_tools` on the AgentBackend Protocol

**Files:**
- Modify: `backend/src/pocketpaw/agents/backend.py`
- Modify: `backend/src/pocketpaw/agents/deep_agents.py` (add the implementation)
- Test: `backend/tests/test_deep_agents_backend.py` (extend)

Protocol gets a new method. `DeepAgentsBackend` implements it by extending its tool cache. Other backends inherit a no-op default that logs a warning — they're not in the v1 valid-backend list for the specialist.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_deep_agents_backend.py`:

```python
class TestDeepAgentsAttachSpecialistTools:
    """attach_specialist_tools() merges into _custom_tools and invalidates the
    compiled-graph cache so the next run picks up the new tool surface."""

    def test_appends_tools_and_invalidates_cache(self):
        from langchain_core.tools import StructuredTool
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        backend._custom_tools = [MagicMock(name="existing")]
        backend._cached_agent = MagicMock(name="prev")
        backend._cached_model_key = ("anthropic:x", (), ())

        new_tool = StructuredTool.from_function(
            func=lambda: "hi", name="extra", description="x"
        )
        backend.attach_specialist_tools([new_tool])

        assert backend._custom_tools[-1] is new_tool
        assert backend._cached_agent is None  # cache invalidated
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd backend && uv run pytest tests/test_deep_agents_backend.py::TestDeepAgentsAttachSpecialistTools -v
```
Expected: FAIL — `AttributeError: 'DeepAgentsBackend' object has no attribute 'attach_specialist_tools'`.

- [ ] **Step 3: Add the Protocol method**

In `backend/src/pocketpaw/agents/backend.py`, inside `class AgentBackend(Protocol):` after the `get_status` method (around line 72):

```python
    def attach_specialist_tools(self, tools: list[Any]) -> None:
        """Attach pocket-specialist-internal tools to this backend instance.

        Called by the specialist runtime to wire list_pockets / validate_spec /
        persist_pocket into the LLM's tool surface for the duration of an
        isolated specialist run.

        Backends that cannot accept dynamic tools at runtime should raise
        NotImplementedError and will be excluded from the valid
        ``pocket_specialist_backend`` set.
        """
        ...
```

- [ ] **Step 4: Implement on `DeepAgentsBackend`**

In `backend/src/pocketpaw/agents/deep_agents.py`, add a method to `DeepAgentsBackend` after `_get_or_create_agent`:

```python
    def attach_specialist_tools(self, tools: list[Any]) -> None:
        """Merge specialist tools into the custom-tool cache.

        Invalidates the compiled-graph cache so the next ``run()`` rebuilds
        the agent with the new tool surface.
        """
        if self._custom_tools is None:
            self._custom_tools = []
        self._custom_tools.extend(tools)
        self._cached_agent = None  # force recompile next run
        self._cached_model_key = None
```

- [ ] **Step 5: Run test to verify pass**

```bash
cd backend && uv run pytest tests/test_deep_agents_backend.py::TestDeepAgentsAttachSpecialistTools -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd backend && git add src/pocketpaw/agents/backend.py \
    src/pocketpaw/agents/deep_agents.py \
    tests/test_deep_agents_backend.py
git commit -m "feat(agents): attach_specialist_tools on AgentBackend protocol + deep_agents impl"
```

---

## Task 6: `AgentRouter.create_isolated_backend()`

**Files:**
- Modify: `backend/src/pocketpaw/agents/router.py`
- Test: `backend/tests/test_router.py` (or wherever AgentRouter tests live — find with `find tests -name "*router*"`).

Returns a fresh, non-cached backend instance with optional settings overrides — the specialist's chat-loop must not share state with the user's chat backend.

- [ ] **Step 1: Find and inspect existing router tests**

```bash
cd backend && find tests -iname "*router*" -name "*.py"
```

Use the located test file (likely `tests/test_router.py` or similar). If none exists, create `backend/tests/test_router_isolated_backend.py`.

- [ ] **Step 2: Write the failing test**

Add to the chosen test file:

```python
class TestCreateIsolatedBackend:
    """create_isolated_backend builds a fresh backend instance with optional
    settings overrides — used for short-lived specialist runs."""

    def test_returns_fresh_instance(self):
        from pocketpaw.agents.router import AgentRouter
        from pocketpaw.config import Settings

        a = AgentRouter.create_isolated_backend(
            "deep_agents", Settings(), settings_override=None
        )
        b = AgentRouter.create_isolated_backend(
            "deep_agents", Settings(), settings_override=None
        )
        assert a is not b

    def test_applies_model_override(self):
        from pocketpaw.agents.router import AgentRouter
        from pocketpaw.config import Settings

        backend = AgentRouter.create_isolated_backend(
            "deep_agents",
            Settings(deep_agents_model="anthropic:claude-sonnet-4-6"),
            settings_override={"deep_agents_model": "openai_compatible:deepseek-v4-pro"},
        )
        assert backend.settings.deep_agents_model == "openai_compatible:deepseek-v4-pro"

    def test_unknown_backend_raises(self):
        from pocketpaw.agents.router import AgentRouter
        from pocketpaw.config import Settings

        with pytest.raises(ValueError, match="not registered"):
            AgentRouter.create_isolated_backend(
                "nonexistent_backend", Settings()
            )
```

- [ ] **Step 3: Run test to verify failure**

```bash
cd backend && uv run pytest tests/test_router_isolated_backend.py -v
```
Expected: FAIL — `AttributeError: type object 'AgentRouter' has no attribute 'create_isolated_backend'`.

- [ ] **Step 4: Implement the classmethod**

In `backend/src/pocketpaw/agents/router.py`, after the `_get_fallback_backend` method (around line 89), add:

```python
    @classmethod
    def create_isolated_backend(
        cls,
        backend_name: str,
        settings: Settings,
        *,
        settings_override: dict[str, Any] | None = None,
    ) -> Any:
        """Build a fresh, non-cached AgentBackend with optional settings overrides.

        Used for short-lived specialist runs that should not share state with
        the main chat backend. Each call returns a new instance; nothing is
        cached on the router.
        """
        backend_cls = get_backend_class(backend_name)
        if backend_cls is None:
            raise ValueError(
                f"Backend '{backend_name}' is not registered or its dependencies "
                "are not installed."
            )

        if settings_override:
            # Settings is a Pydantic BaseSettings; copy with overrides.
            effective = settings.model_copy(update=settings_override)
        else:
            effective = settings

        return backend_cls(effective)
```

- [ ] **Step 5: Run test to verify pass**

```bash
cd backend && uv run pytest tests/test_router_isolated_backend.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
cd backend && git add src/pocketpaw/agents/router.py \
    tests/test_router_isolated_backend.py
git commit -m "feat(agents): AgentRouter.create_isolated_backend for fresh non-cached instances"
```

---

## Task 7: `run_specialist()` runtime — happy path

**Files:**
- Create: `backend/ee/agent/pocket_specialist/runtime.py`
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_runtime.py`

Orchestrates: backend selection → tool wiring → run → event emission →
result assembly. Persist-once safety net is added in Task 8.

- [ ] **Step 1: Write the failing test (happy path)**

Create `backend/tests/ee/agent/test_pocket_specialist/test_runtime.py`:

```python
"""run_specialist end-to-end with a mocked backend."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistHints,
    run_specialist,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings


def _stream(events: list[AgentEvent]):
    """Build an async generator that yields the given events."""
    async def gen(*args, **kwargs):
        for e in events:
            yield e
    return gen


class TestRunSpecialistHappyPath:
    @pytest.mark.asyncio
    async def test_returns_persisted_pocket_via_tool_capture(self):
        # Backend yields a tool_result for persist_pocket; runtime captures
        # it and returns it as the final pocket.
        captured_pocket = {"id": "p-new", "name": "Repos", "color": "#0ea5e9"}
        events = [
            AgentEvent(type="tool_use", content="", metadata={"name": "list_pockets"}),
            AgentEvent(type="tool_result", content="[]", metadata={"name": "list_pockets"}),
            AgentEvent(type="tool_use", content="", metadata={"name": "persist_pocket"}),
            AgentEvent(
                type="tool_result",
                content=str(captured_pocket),
                metadata={"name": "persist_pocket", "result": captured_pocket},
            ),
            AgentEvent(type="done", content=""),
        ]
        fake_backend = MagicMock()
        fake_backend.run = _stream(events)
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with (
            patch(
                "ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
                return_value=fake_backend,
            ),
            patch(
                "ee.agent.pocket_specialist.runtime.emit_specialist_event",
                new=AsyncMock(),
            ) as mock_emit,
        ):
            out = await run_specialist(
                PocketSpecialistCreateInput(brief="Track my repos"),
                workspace_id="ws-1",
                user_id="user-A",
                settings=Settings(),
            )

        assert out.ok is True
        assert out.action in ("created", "extended")
        assert out.pocket["id"] == "p-new"
        # Status events emitted in order
        emitted = [c.args[0].value for c in mock_emit.await_args_list]
        assert emitted[0] == "specialist:start"
        assert emitted[-1] == "specialist:done"

    @pytest.mark.asyncio
    async def test_hints_target_pocket_id_locks_update_path(self):
        captured_pocket = {"id": "p-1", "name": "Updated"}
        events = [
            AgentEvent(type="tool_use", content="", metadata={"name": "persist_pocket"}),
            AgentEvent(
                type="tool_result",
                content="",
                metadata={"name": "persist_pocket", "result": captured_pocket},
            ),
            AgentEvent(type="done", content=""),
        ]
        fake_backend = MagicMock()
        fake_backend.run = _stream(events)
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with (
            patch(
                "ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
                return_value=fake_backend,
            ),
            patch(
                "ee.agent.pocket_specialist.runtime.emit_specialist_event",
                new=AsyncMock(),
            ),
        ):
            out = await run_specialist(
                PocketSpecialistCreateInput(
                    brief="Update repos pocket",
                    hints=PocketSpecialistHints(target_pocket_id="p-1"),
                ),
                workspace_id="ws-1",
                user_id="user-A",
                settings=Settings(),
            )

        assert out.action == "extended"
        assert out.pocket["id"] == "p-1"
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_runtime.py -v
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement runtime.py (happy path only)**

Create `backend/ee/agent/pocket_specialist/runtime.py`:

```python
"""Pocket-specialist runtime — the only public entry point for the tool surfaces.

Orchestrates backend selection, tool wiring, event emission, and result
assembly. Always persists a pocket — see feedback_pocket_always_ships.md.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ee.agent.pocket_specialist.events import (
    SpecialistEvent,
    emit_specialist_event,
)
from ee.agent.pocket_specialist.settings import resolve_specialist_model
from ee.agent.pocket_specialist.tools import (
    make_list_pockets_tool,
    make_persist_pocket_tool,
    make_validate_spec_tool,
)
from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP, POCKET_ID_TOKEN
from pocketpaw.agents.router import AgentRouter
from pocketpaw.config import Settings

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Tool I/O schemas
# ------------------------------------------------------------------

class PocketSpecialistHints(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    icon: str | None = None
    target_pocket_id: str | None = None


class PocketSpecialistCreateInput(BaseModel):
    brief: str = Field(..., min_length=10, max_length=4000)
    hints: PocketSpecialistHints | None = None


class PocketSpecialistCreateOutput(BaseModel):
    ok: bool
    action: Literal["created", "extended"]
    pocket: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int
    backend_used: str


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------

async def run_specialist(
    input: PocketSpecialistCreateInput,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
) -> PocketSpecialistCreateOutput:
    started = time.monotonic()
    backend_name = settings.pocket_specialist_backend
    model_id = resolve_specialist_model(settings)

    await emit_specialist_event(
        SpecialistEvent.START,
        {
            "brief": input.brief[:200],
            "hints": input.hints.model_dump() if input.hints else None,
            "backend": backend_name,
        },
    )

    override: dict[str, Any] = {}
    if model_id:
        override[f"{backend_name}_model"] = model_id

    backend = AgentRouter.create_isolated_backend(
        backend_name, settings, settings_override=override or None,
    )
    backend.attach_specialist_tools([
        make_list_pockets_tool(workspace_id=workspace_id, user_id=user_id),
        make_validate_spec_tool(),
        make_persist_pocket_tool(workspace_id=workspace_id, user_id=user_id),
    ])

    system_prompt = _build_system_prompt(input.hints)
    user_message = _build_user_message(input)

    captured_pocket: dict[str, Any] | None = None
    captured_warnings: list[str] = []
    persist_called = False

    try:
        async for event in backend.run(user_message, system_prompt=system_prompt):
            if event.type == "tool_use":
                tool_name = (event.metadata or {}).get("name", "")
                if tool_name == "list_pockets":
                    await emit_specialist_event(SpecialistEvent.LISTING, {})
                elif tool_name == "validate_spec":
                    await emit_specialist_event(
                        SpecialistEvent.VALIDATING, {}
                    )
                elif tool_name == "persist_pocket":
                    await emit_specialist_event(
                        SpecialistEvent.PERSISTING, {}
                    )
            elif event.type == "tool_result":
                meta = event.metadata or {}
                if meta.get("name") == "persist_pocket":
                    persist_called = True
                    result = meta.get("result")
                    if isinstance(result, dict):
                        captured_pocket = result
                elif meta.get("name") == "validate_spec":
                    result = meta.get("result")
                    if isinstance(result, dict):
                        captured_warnings = result.get("warnings", [])
    finally:
        await backend.stop()

    if not persist_called or captured_pocket is None:
        # Safety net (Task 8 expands this).
        log.warning(
            "specialist run finished without persist_pocket; using fallback"
        )
        captured_pocket = await _force_persist_fallback(
            workspace_id=workspace_id,
            user_id=user_id,
            input=input,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    action: Literal["created", "extended"] = (
        "extended"
        if (input.hints and input.hints.target_pocket_id)
        else "created"
    )

    await emit_specialist_event(
        SpecialistEvent.DONE,
        {
            "pocket_id": captured_pocket.get("id", ""),
            "action": action,
            "duration_ms": duration_ms,
            "warning_count": len(captured_warnings),
        },
    )

    return PocketSpecialistCreateOutput(
        ok=True,
        action=action,
        pocket=captured_pocket,
        warnings=captured_warnings,
        duration_ms=duration_ms,
        backend_used=backend_name,
    )


def _build_system_prompt(hints: PocketSpecialistHints | None) -> str:
    """Compose the specialist system prompt from the canonical creation
    prompt + any hints from the caller."""
    base = POCKET_CREATION_PROMPT_MCP.replace(POCKET_ID_TOKEN, "")
    if not hints:
        return base
    hint_block = ["", "CALLER HINTS (respect when set, otherwise decide yourself):"]
    for field in ("name", "description", "color", "icon", "target_pocket_id"):
        v = getattr(hints, field)
        if v:
            hint_block.append(f"  {field}: {v}")
    return base + "\n".join(hint_block)


def _build_user_message(input: PocketSpecialistCreateInput) -> str:
    """The user-message envelope for the specialist run."""
    return (
        "Create a pocket per the brief below. Follow the workflow in your "
        "system prompt: list existing pockets, decide extend-vs-create, "
        "draft, validate, persist. You MUST end by calling persist_pocket "
        "exactly once.\n\nBRIEF:\n"
        + input.brief
    )


async def _force_persist_fallback(
    *,
    workspace_id: str,
    user_id: str,
    input: PocketSpecialistCreateInput,
) -> dict[str, Any]:
    """Stub — Task 8 fills this in."""
    raise NotImplementedError("force-persist fallback added in Task 8")
```

- [ ] **Step 4: Run test to verify pass**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_runtime.py::TestRunSpecialistHappyPath -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/runtime.py \
    tests/ee/agent/test_pocket_specialist/test_runtime.py
git commit -m "feat(pocket-specialist): runtime happy path with backend orchestration"
```

---

## Task 8: Persist-once safety net

**Files:**
- Modify: `backend/ee/agent/pocket_specialist/runtime.py`
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_runtime.py` (extend)

If the LLM finishes without calling `persist_pocket`, the runtime
force-persists a minimal-but-valid pocket using whatever name/description
hints we have. Per `feedback_pocket_always_ships.md` — never block.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/ee/agent/test_pocket_specialist/test_runtime.py`:

```python
class TestRunSpecialistSafetyNet:
    @pytest.mark.asyncio
    async def test_force_persists_when_llm_skips_persist(self):
        # Backend yields events that never include persist_pocket — runtime
        # must force-create a pocket anyway.
        events = [
            AgentEvent(type="message", content="I'm done."),
            AgentEvent(type="done", content=""),
        ]
        fake_backend = MagicMock()
        fake_backend.run = _stream(events)
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        force_persisted = {"id": "p-fallback", "name": "Untitled (auto)"}
        with (
            patch(
                "ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
                return_value=fake_backend,
            ),
            patch(
                "ee.agent.pocket_specialist.runtime.emit_specialist_event",
                new=AsyncMock(),
            ),
            patch(
                "ee.agent.pocket_specialist.runtime._agent_create_for_fallback",
                new=AsyncMock(
                    return_value=(force_persisted, None, None)
                ),
            ) as mock_create,
        ):
            out = await run_specialist(
                PocketSpecialistCreateInput(brief="A vague brief"),
                workspace_id="ws-1",
                user_id="user-A",
                settings=Settings(),
            )

        mock_create.assert_awaited_once()
        assert out.ok is True
        assert out.pocket["id"] == "p-fallback"
        assert any("force" in w.lower() or "fallback" in w.lower() for w in out.warnings)
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_runtime.py::TestRunSpecialistSafetyNet -v
```
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement the fallback**

In `backend/ee/agent/pocket_specialist/runtime.py`, replace the stub `_force_persist_fallback` (and add the `_agent_create_for_fallback` indirection so tests can patch it):

```python
from ee.cloud.pockets.agent_context import (
    create_pocket_for_agent as _agent_create_for_fallback,
)


async def _force_persist_fallback(
    *,
    workspace_id: str,
    user_id: str,
    input: PocketSpecialistCreateInput,
) -> dict[str, Any]:
    """Persist a minimal pocket when the LLM finished without calling
    persist_pocket. Always ships output.
    """
    name = (input.hints and input.hints.name) or _derive_name_from_brief(input.brief)
    description = (
        input.hints and input.hints.description
    ) or input.brief[:200]
    minimal_spec = {
        "version": "1.0",
        "state": {},
        "ui": {
            "type": "text",
            "props": {
                "value": (
                    "This pocket was auto-created from a brief. "
                    "Ask me to refine it and I'll fill it out."
                )
            },
        },
    }
    pocket, _id, err = await _agent_create_for_fallback(
        workspace_id=workspace_id,
        user_id=user_id,
        name=name,
        description=description,
        icon=(input.hints and input.hints.icon) or "Sparkles",
        color=(input.hints and input.hints.color) or "#a78bfa",
        ripple_spec=minimal_spec,
    )
    if err:
        raise RuntimeError(f"force-persist fallback failed: {err}")
    return pocket


def _derive_name_from_brief(brief: str) -> str:
    """Best-effort short title from the brief — first 6 words, capped at 40 chars."""
    words = brief.strip().split()[:6]
    name = " ".join(words).rstrip(".,!?:;")[:40]
    return name or "Untitled pocket"
```

Also update the call site in `run_specialist` to surface the fallback in warnings:

```python
    if not persist_called or captured_pocket is None:
        log.warning(
            "specialist run finished without persist_pocket; using fallback"
        )
        captured_pocket = await _force_persist_fallback(
            workspace_id=workspace_id,
            user_id=user_id,
            input=input,
        )
        captured_warnings.append(
            "Specialist did not call persist_pocket; force-persisted a "
            "minimal pocket. Ask the user to refine."
        )
```

- [ ] **Step 4: Run test to verify pass**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_runtime.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/runtime.py \
    tests/ee/agent/test_pocket_specialist/test_runtime.py
git commit -m "feat(pocket-specialist): persist-once safety net"
```

---

## Task 9: MCP tool registration

**Files:**
- Create: `backend/ee/agent/pocket_specialist/mcp_tool.py`
- Modify: `backend/src/pocketpaw/agents/claude_sdk.py` (register the new server in `_get_mcp_servers`, add tool ID to allowlist)
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_mcp_tool.py`

Follows the pattern in `pocketpaw/agents/sdk_mcp_pocket.py`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/ee/agent/test_pocket_specialist/test_mcp_tool.py`:

```python
"""MCP server registration + handler tests."""
from unittest.mock import AsyncMock, patch

import pytest


class TestPocketSpecialistMcpServer:
    def test_server_name_and_tool_id(self):
        from ee.agent.pocket_specialist.mcp_tool import (
            CREATE_TOOL_ID,
            POCKET_SPECIALIST_TOOL_IDS,
            SERVER_NAME,
        )
        assert SERVER_NAME == "pocketpaw_pocket_specialist"
        assert CREATE_TOOL_ID == "mcp__pocketpaw_pocket_specialist__create"
        assert CREATE_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS

    def test_build_server_returns_sdk_mcp_server(self):
        from ee.agent.pocket_specialist.mcp_tool import (
            build_pocket_specialist_server,
        )
        server = build_pocket_specialist_server()
        # Just check it's a non-None object — exact type depends on the
        # claude-agent-sdk version. Server has a name attribute.
        assert server is not None


class TestCreateHandler:
    @pytest.mark.asyncio
    async def test_handler_calls_run_specialist_and_returns_text_payload(self):
        from ee.agent.pocket_specialist.mcp_tool import _create_handler
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        fake_out = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p-1", "name": "X"},
            warnings=[],
            duration_ms=42,
            backend_used="deep_agents",
        )
        with patch(
            "ee.agent.pocket_specialist.mcp_tool.run_specialist",
            new=AsyncMock(return_value=fake_out),
        ):
            payload = await _create_handler({"brief": "Track my repos"})
        # MCP shape: {content: [{type: "text", text: "<json>"}]}
        assert "content" in payload
        assert payload["content"][0]["type"] == "text"
        # JSON-encoded body should contain the pocket id
        assert "p-1" in payload["content"][0]["text"]
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_mcp_tool.py -v
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the MCP module**

Read `backend/src/pocketpaw/agents/sdk_mcp_pocket.py` to copy the exact server-construction pattern (it imports `claude_agent_sdk.create_sdk_mcp_server` or similar).

Create `backend/ee/agent/pocket_specialist/mcp_tool.py`:

```python
"""In-process SDK MCP binding that exposes the pocket specialist to
MCP-capable agent backends (claude_agent_sdk, deep_agents, openai_agents,
google_adk).

Mirrors the structure of ``pocketpaw/agents/sdk_mcp_pocket.py``. The
single tool ``create`` accepts {brief, hints?} and hands off to
``runtime.run_specialist``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistHints,
    run_specialist,
)
from ee.cloud.pockets.agent_context import (
    user_id_var,
    workspace_id_var,
)

log = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_pocket_specialist"
CREATE_TOOL_ID = f"mcp__{SERVER_NAME}__create"

POCKET_SPECIALIST_TOOL_IDS = (CREATE_TOOL_ID,)


async def _create_handler(args: dict[str, Any]) -> dict[str, Any]:
    """MCP handler for pocket_specialist__create."""
    from pocketpaw.config import Settings

    workspace_id = workspace_id_var.get(None)
    user_id = user_id_var.get(None)
    if not workspace_id or not user_id:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Error: pocket_specialist__create requires workspace "
                        "and user context (call from a cloud chat session)."
                    ),
                }
            ],
            "is_error": True,
        }

    raw_hints = args.get("hints")
    hints = PocketSpecialistHints(**raw_hints) if raw_hints else None
    payload = PocketSpecialistCreateInput(
        brief=args.get("brief", ""), hints=hints
    )

    try:
        out = await run_specialist(
            payload,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=Settings(),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pocket specialist run failed")
        return {
            "content": [
                {"type": "text", "text": f"Error: {exc}"}
            ],
            "is_error": True,
        }

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(out.model_dump(), separators=(",", ":")),
            }
        ]
    }


def build_pocket_specialist_server():
    """Build the in-process SDK MCP server that exposes the specialist tool."""
    # Match sdk_mcp_pocket.py's import path EXACTLY.
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "create",
        "Create a pocket end-to-end from a natural-language brief. The "
        "specialist lists existing pockets, decides extend-vs-create, drafts "
        "and validates the rippleSpec, and persists. Returns "
        "{ok, action, pocket, warnings, duration_ms, backend_used}. Always "
        "produces a pocket — never noop.",
        {
            "brief": {
                "type": "string",
                "description": (
                    "Natural-language description of what the user wants. "
                    "Include any research/context already gathered."
                ),
            },
            "hints": {
                "type": "object",
                "description": (
                    "Optional caller-supplied overrides "
                    "{name?, description?, color?, icon?, target_pocket_id?}."
                ),
                "additionalProperties": True,
            },
        },
    )
    async def create_pocket_specialist(args: dict[str, Any]) -> dict[str, Any]:
        return await _create_handler(args)

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[create_pocket_specialist],
    )
```

- [ ] **Step 4: Wire the new server into `claude_sdk.py`**

In `backend/src/pocketpaw/agents/claude_sdk.py`, find `_get_mcp_servers` (around line 532) and the existing block that registers `sdk_mcp_pocket` (around line 588 — `from pocketpaw.agents.sdk_mcp_pocket import build_pocket_context_server`).

Add a sibling block immediately after, gated to register only when the operator hasn't disabled the specialist (we do not have an `enabled` setting per the spec — gate is "if backend can run"):

```python
            # Pocket specialist server — exposes pocket_specialist__create.
            try:
                from ee.agent.pocket_specialist.mcp_tool import (
                    SERVER_NAME as _PS_SERVER_NAME,
                    build_pocket_specialist_server,
                )
                if self._policy.is_mcp_server_allowed(_PS_SERVER_NAME):
                    servers[_PS_SERVER_NAME] = build_pocket_specialist_server()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pocket_specialist MCP server skipped: %s", exc
                )
```

Then find the existing `POCKET_TOOL_IDS` import (around line 894) and extend the allowlist:

```python
            from ee.agent.pocket_specialist.mcp_tool import (
                POCKET_SPECIALIST_TOOL_IDS,
            )
            allowed_tools.extend(POCKET_SPECIALIST_TOOL_IDS)
```

(Splice into the existing pocket-tool allowlist block — read the surrounding 20 lines first to match the pattern in this file exactly.)

- [ ] **Step 5: Run test to verify pass**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_mcp_tool.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/mcp_tool.py \
    src/pocketpaw/agents/claude_sdk.py \
    tests/ee/agent/test_pocket_specialist/test_mcp_tool.py
git commit -m "feat(pocket-specialist): MCP tool registration + claude_sdk wiring"
```

---

## Task 10: CLI shell command registration

**Files:**
- Create: `backend/ee/agent/pocket_specialist/cli_tool.py`
- Modify: `backend/src/pocketpaw/tools/cli.py` (or wherever `cloud_list_pockets` is registered — find with `grep -rn 'cloud_list_pockets'`)
- Test: `backend/tests/ee/agent/test_pocket_specialist/test_cli_tool.py`

For backends that don't speak MCP (codex_cli, opencode, gemini_cli), the
specialist is exposed as a shell command `cloud_pocket_specialist_create`.

- [ ] **Step 1: Locate the existing CLI tool registration pattern**

```bash
cd backend && grep -rn "cloud_list_pockets\|_cloud_list_pockets" src/pocketpaw/tools/ 2>&1
```

Read the file the matches point to. Note the function signature pattern
(probably takes argv-style args, returns a JSON string).

- [ ] **Step 2: Write the failing test**

Create `backend/tests/ee/agent/test_pocket_specialist/test_cli_tool.py`:

```python
"""CLI shell command tests for cloud_pocket_specialist_create."""
import json
from unittest.mock import AsyncMock, patch

import pytest


class TestCloudPocketSpecialistCreate:
    @pytest.mark.asyncio
    async def test_parses_brief_and_returns_json(self):
        from ee.agent.pocket_specialist.cli_tool import (
            _cloud_pocket_specialist_create,
        )
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        fake_out = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p-1", "name": "X"},
            warnings=[],
            duration_ms=10,
            backend_used="deep_agents",
        )
        with patch(
            "ee.agent.pocket_specialist.cli_tool.run_specialist",
            new=AsyncMock(return_value=fake_out),
        ):
            result = await _cloud_pocket_specialist_create(
                ["Track my GitHub PRs across foo/bar/baz"]
            )
        parsed = json.loads(result)
        assert parsed["ok"] is True
        assert parsed["pocket"]["id"] == "p-1"

    @pytest.mark.asyncio
    async def test_parses_hints_flag(self):
        from ee.agent.pocket_specialist.cli_tool import (
            _cloud_pocket_specialist_create,
        )
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        fake_out = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p-1", "name": "PR Tracker"},
            warnings=[],
            duration_ms=10,
            backend_used="deep_agents",
        )
        with patch(
            "ee.agent.pocket_specialist.cli_tool.run_specialist",
            new=AsyncMock(return_value=fake_out),
        ) as mock_run:
            await _cloud_pocket_specialist_create([
                "Track my GitHub PRs",
                "--hints",
                '{"name": "PR Tracker", "color": "#0ea5e9"}',
            ])
        # Verify hints were parsed and passed
        called_input = mock_run.await_args.args[0]
        assert called_input.hints is not None
        assert called_input.hints.name == "PR Tracker"
        assert called_input.hints.color == "#0ea5e9"
```

- [ ] **Step 3: Run test to verify failure**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_cli_tool.py -v
```
Expected: FAIL — module not found.

- [ ] **Step 4: Implement the CLI tool**

Create `backend/ee/agent/pocket_specialist/cli_tool.py`:

```python
"""CLI shell command for backends that don't speak MCP (codex_cli,
opencode, gemini_cli, copilot_sdk).

Command signature:
    cloud_pocket_specialist_create <brief> [--hints '<json>']

Returns the JSON-encoded PocketSpecialistCreateOutput.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistHints,
    run_specialist,
)
from ee.cloud.pockets.agent_context import user_id_var, workspace_id_var
from pocketpaw.config import Settings

log = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> tuple[str, dict[str, Any] | None]:
    p = argparse.ArgumentParser(
        prog="cloud_pocket_specialist_create",
        add_help=False,
    )
    p.add_argument("brief", type=str)
    p.add_argument("--hints", type=str, default=None)
    ns = p.parse_args(argv)
    hints = json.loads(ns.hints) if ns.hints else None
    return ns.brief, hints


async def _cloud_pocket_specialist_create(argv: list[str]) -> str:
    brief, raw_hints = _parse_args(argv)
    hints = PocketSpecialistHints(**raw_hints) if raw_hints else None

    workspace_id = workspace_id_var.get(None)
    user_id = user_id_var.get(None)
    if not workspace_id or not user_id:
        return json.dumps({
            "ok": False,
            "error": "missing workspace/user context",
        })

    out = await run_specialist(
        PocketSpecialistCreateInput(brief=brief, hints=hints),
        workspace_id=workspace_id,
        user_id=user_id,
        settings=Settings(),
    )
    return json.dumps(out.model_dump(), separators=(",", ":"))
```

- [ ] **Step 5: Register the command in the CLI tools registry**

In the file located in Step 1 (likely `backend/src/pocketpaw/tools/cli.py`), follow the EXACT same pattern as `cloud_list_pockets` to register `cloud_pocket_specialist_create`. Mirror the existing dispatcher entry; do not invent a new pattern.

If the existing pattern is, for example, a dict mapping command name → coroutine, add:

```python
    "cloud_pocket_specialist_create": _cloud_pocket_specialist_create,
```

with the import at the top of the file:

```python
from ee.agent.pocket_specialist.cli_tool import _cloud_pocket_specialist_create
```

- [ ] **Step 6: Run test to verify pass**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/test_cli_tool.py -v
```
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/cli_tool.py \
    src/pocketpaw/tools/cli.py \
    tests/ee/agent/test_pocket_specialist/test_cli_tool.py
git commit -m "feat(pocket-specialist): CLI shell command for non-MCP backends"
```

---

## Task 11: Replace inline pocket-creation block in `_pockets.py`

**Files:**
- Modify: `backend/ee/ripple/_pockets.py` (both `POCKET_CREATION_PROMPT_MCP` and `POCKET_CREATION_PROMPT_CLI`)
- Test: `backend/tests/cloud/test_pocket_prompts_single_source.py` (extend)

Per the spec, both variants get an unconditional STEP 0 delegation block
that REPLACES the existing inline `STEP 1..N` blocks.

- [ ] **Step 1: Read the current prompts**

```bash
cd backend && cat ee/ripple/_pockets.py | head -300
```

Identify the exact span of the inline STEP 1..N block in each prompt
variant. Note the exact section boundaries (likely demarcated by markdown
headers like `## STEP 1` or `### Pocket creation steps`).

- [ ] **Step 2: Write the failing extended regression test**

In `backend/tests/cloud/test_pocket_prompts_single_source.py`, append:

```python
class TestSpecialistDelegationBlock:
    """The new STEP 0 delegation block must replace the legacy STEP 1..N
    inline-creation block in BOTH MCP and CLI prompt variants."""

    def test_mcp_prompt_has_delegation_block(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert "pocket_specialist__create" in POCKET_CREATION_PROMPT_MCP
        assert "DELEGATE TO SPECIALIST" in POCKET_CREATION_PROMPT_MCP

    def test_cli_prompt_has_delegation_block(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_CLI

        assert "cloud_pocket_specialist_create" in POCKET_CREATION_PROMPT_CLI
        assert "DELEGATE TO SPECIALIST" in POCKET_CREATION_PROMPT_CLI

    def test_legacy_inline_steps_removed_mcp(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        # The legacy inline block enumerated mcp__pocketpaw_pocket__create_pocket
        # directly. After this PR, the calling agent should NEVER call
        # create_pocket directly.
        assert "mcp__pocketpaw_pocket__create_pocket" not in POCKET_CREATION_PROMPT_MCP
        assert "mcp__pocketpaw_pocket__update_pocket" not in POCKET_CREATION_PROMPT_MCP

    def test_legacy_inline_steps_removed_cli(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_CLI

        assert "cloud_create_pocket" not in POCKET_CREATION_PROMPT_CLI
        assert "cloud_update_pocket" not in POCKET_CREATION_PROMPT_CLI
```

- [ ] **Step 3: Run test to verify failure**

```bash
cd backend && uv run pytest tests/cloud/test_pocket_prompts_single_source.py::TestSpecialistDelegationBlock -v
```
Expected: 4 FAIL — delegation block not yet added; legacy strings still present.

- [ ] **Step 4: Rewrite the prompts**

In `backend/ee/ripple/_pockets.py`:

For `POCKET_CREATION_PROMPT_MCP`, replace the STEP 1..N inline block with:

```
## STEP 0 — DELEGATE TO SPECIALIST

When the user wants a pocket and you have the brief, IMMEDIATELY call:

    pocket_specialist__create({
        "brief": "<natural-language description of what the user wants>",
        "hints": {  // optional, only when user named these explicitly
            "name": "<user-said-name>",
            "color": "<user-said-color-hex>",
            "icon": "<user-said-icon>"
        }
    })

The specialist will list existing pockets, decide extend-vs-create,
draft, validate, and persist. You receive:

    {ok, action: "created"|"extended", pocket, warnings, duration_ms, backend_used}

Do NOT call list_pockets, create_pocket, or update_pocket directly.
The specialist owns the whole flow in one tool call.

After the specialist returns, surface any warnings to the user as
"I shipped it; want me to clean up X?" — do NOT block on warnings.
The pocket already exists.
```

For `POCKET_CREATION_PROMPT_CLI`, mirror with the shell-command form:

```
## STEP 0 — DELEGATE TO SPECIALIST

When the user wants a pocket and you have the brief, IMMEDIATELY run:

    cloud_pocket_specialist_create "<brief>" --hints '<json>'

(The --hints flag is optional. Pass JSON like
{"name": "PR Tracker", "color": "#0ea5e9"} only when the user named
those fields explicitly.)

The specialist will list existing pockets, decide extend-vs-create,
draft, validate, and persist. The command prints a JSON object:

    {ok, action: "created"|"extended", pocket, warnings, duration_ms, backend_used}

Do NOT call cloud_list_pockets, cloud_create_pocket, or
cloud_update_pocket directly. The specialist owns the whole flow.

After the specialist returns, surface any warnings to the user as
"I shipped it; want me to clean up X?" — do NOT block on warnings.
The pocket already exists.
```

DELETE everything that was the legacy STEP 1..N section in each variant.

- [ ] **Step 5: Run the regression tests + audit unrelated tests**

```bash
cd backend && uv run pytest tests/cloud/test_pocket_prompts_single_source.py -v
```
Expected: all in this file pass.

Now audit:

```bash
cd backend && uv run pytest tests/ -k "pocket and prompt" --tb=short 2>&1 | tail -30
```

Any test that asserts the presence of the legacy inline steps will fail.
Update those tests to assert the delegation block instead, or remove
them if they were specifically testing legacy behavior. Read each failure
case-by-case.

- [ ] **Step 6: Commit**

```bash
cd backend && git add ee/ripple/_pockets.py \
    tests/cloud/test_pocket_prompts_single_source.py \
    tests/  # any other prompt tests you needed to update
git commit -m "feat(pocket-specialist): replace inline pocket creation with delegation block"
```

---

## Task 12: Public package exports

**Files:**
- Modify: `backend/ee/agent/pocket_specialist/__init__.py`

- [ ] **Step 1: Add the public re-exports**

Replace `backend/ee/agent/pocket_specialist/__init__.py` with:

```python
"""Pocket specialist — orchestrated pocket creation from a brief.

See docs/superpowers/specs/2026-05-09-pocket-specialist-design.md.
"""

from ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistCreateOutput,
    PocketSpecialistHints,
    run_specialist,
)

__all__ = [
    "PocketSpecialistCreateInput",
    "PocketSpecialistCreateOutput",
    "PocketSpecialistHints",
    "run_specialist",
]
```

- [ ] **Step 2: Run all specialist tests one more time**

```bash
cd backend && uv run pytest tests/ee/agent/test_pocket_specialist/ -v
```
Expected: all pass.

- [ ] **Step 3: Run the full backend test suite to catch regressions**

```bash
cd backend && uv run pytest --ignore=tests/e2e -q 2>&1 | tail -20
```
Expected: no new failures vs. main. If any test outside this PR's scope fails, investigate before commit.

- [ ] **Step 4: Commit**

```bash
cd backend && git add ee/agent/pocket_specialist/__init__.py
git commit -m "feat(pocket-specialist): public package exports"
```

---

## Task 13: Open the PR

- [ ] **Step 1: Push the branch**

```bash
cd backend && git push -u origin feat/pocket-specialist
```

- [ ] **Step 2: Open the PR**

```bash
cd backend && gh pr create --base chore/deep-agents-optimizations \
    --title "feat(pocket-specialist): orchestrated pocket creation tool" \
    --body "$(cat <<'EOF'
## Summary

Stacked on #1084. Implements the design at
`docs/superpowers/specs/2026-05-09-pocket-specialist-design.md`.

A new MCP/CLI tool `pocket_specialist__create` (and shell variant
`cloud_pocket_specialist_create`) that any agent backend can call to
create a pocket end-to-end (list -> decide extend-vs-create -> draft ->
validate -> persist) with status events streamed back to the user.

## What's in the PR

- New module `backend/ee/agent/pocket_specialist/` (runtime, tools,
  events, settings, MCP + CLI surfaces).
- Three new Settings fields: `pocket_specialist_backend`,
  `pocket_specialist_model`, `pocket_specialist_max_validation_retries`.
- `AgentBackend.attach_specialist_tools()` Protocol method, implemented
  on `DeepAgentsBackend`.
- `AgentRouter.create_isolated_backend()` for fresh non-cached backend
  instances per specialist run.
- Calling-agent prompts in `ee/ripple/_pockets.py` rewritten to
  delegate unconditionally to the specialist (legacy STEP 1..N inline
  blocks removed).
- Persist-once safety net per `feedback_pocket_always_ships.md`: if the
  LLM finishes without calling `persist_pocket`, the runtime force-
  persists a minimal pocket from the brief.

## Test plan

- [x] `uv run pytest tests/ee/agent/test_pocket_specialist/ -v`
- [ ] Full backend suite green: `uv run pytest --ignore=tests/e2e -q`
- [ ] Manual smoke (claude_agent_sdk caller): `mcp__pocketpaw_pocket_specialist__create({brief: "Track my GitHub PRs"})` via cloud chat -> creates pocket, status events visible in activity panel.
- [ ] Manual smoke (codex_cli caller): `cloud_pocket_specialist_create "Track my repos"` shell command -> same outcome.
- [ ] Misconfig smoke: set `POCKETPAW_POCKET_SPECIALIST_BACKEND=not_a_backend` -> startup warning, tool not registered, calling agent has no pocket tools (intentional, per spec).
EOF
)"
```

- [ ] **Step 3: Verify the PR was created**

```bash
cd backend && gh pr view --web
```

---

## Self-Review

1. **Spec coverage** — every spec section has a task:
   - Architecture diagram → Tasks 4-10 collectively
   - Module layout → Tasks 1-12 file creation
   - Tool surface → Tasks 9, 10
   - Internal workflow → Tasks 7-8
   - Runtime selection → Tasks 5, 6, 7
   - Settings → Task 1
   - Specialist-internal tools → Task 4
   - Status events → Tasks 3, 7
   - Calling-agent prompt changes → Task 11
   - Testing strategy → tests are integrated into each task
   - Operator misconfiguration mode → covered by `create_isolated_backend` raising `ValueError` (Task 6) + smoke test in PR description

2. **Placeholder scan** — no `TBD`, `TODO`, `<fill in>`, or "implement later" markers; every code step shows the actual code; every test step shows the actual test code; every command step has the exact command.

3. **Type consistency** — names checked across tasks:
   - `PocketSpecialistCreateInput` / `PocketSpecialistCreateOutput` / `PocketSpecialistHints` — same in runtime.py, mcp_tool.py, cli_tool.py, tests.
   - `run_specialist(input, *, workspace_id, user_id, settings)` — consistent signature.
   - `attach_specialist_tools(tools: list)` — same in Protocol (Task 5) and impl (Task 5) and runtime call (Task 7).
   - `create_isolated_backend(backend_name, settings, *, settings_override)` — same in classmethod (Task 6) and runtime call (Task 7).
   - `SpecialistEvent` enum values — used in events.py (Task 3) and runtime.py (Task 7) consistently.
   - `SERVER_NAME = "pocketpaw_pocket_specialist"` and `CREATE_TOOL_ID = "mcp__pocketpaw_pocket_specialist__create"` — consistent in mcp_tool.py and tests.

No issues found.
