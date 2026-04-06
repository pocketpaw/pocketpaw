# Deep Agents Backend Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add LangChain Deep Agents as a 7th agent backend, giving users access to the LangGraph ecosystem with built-in planning, subagent delegation, and multi-provider LLM support.

**Architecture:** The `DeepAgentsBackend` class implements the existing `AgentBackend` protocol. It wraps `create_deep_agent()` from the `deepagents` SDK, translates LangGraph streaming events into PocketPaw `AgentEvent` objects, and bridges PocketPaw's tool system via LangChain `StructuredTool`. Installed as optional dep `pocketpaw[deep-agents]`.

**Tech Stack:** `deepagents` (LangChain/LangGraph), `langchain` (for `init_chat_model`, `StructuredTool`)

---

## Task 1: Register the Backend in the Registry

**Files:**
- Modify: `src/pocketpaw/agents/registry.py`

**Step 1: Add registry entry**

In `_BACKEND_REGISTRY`, add after `copilot_sdk`:

```python
"deep_agents": ("pocketpaw.agents.deep_agents", "DeepAgentsBackend"),
```

**Step 2: Verify registry lists it**

Run: `uv run python -c "from pocketpaw.agents.registry import list_backends; print(list_backends())"`
Expected: List includes `"deep_agents"`. `get_backend_class("deep_agents")` returns `None` (module doesn't exist yet).

**Step 3: Commit**

```bash
git add src/pocketpaw/agents/registry.py
git commit -m "feat(agents): register deep_agents backend in registry"
```

---

## Task 2: Add Config Fields

**Files:**
- Modify: `src/pocketpaw/config.py`

**Step 1: Add settings fields after the Copilot SDK section (~line 267)**

```python
# Deep Agents (LangChain/LangGraph) Settings
deep_agents_model: str = Field(
    default="anthropic:claude-sonnet-4-6",
    description="Model for Deep Agents backend (provider:model format, e.g. 'openai:gpt-4o')",
)
deep_agents_max_turns: int = Field(
    default=100,
    description="Max turns per query in Deep Agents backend (0 = unlimited)",
)
```

**Step 2: Update `agent_backend` field description (~line 190)**

Add `'deep_agents'` to the description string of the `agent_backend` field.

**Step 3: Verify config loads**

Run: `uv run python -c "from pocketpaw.config import Settings; s = Settings(); print(s.deep_agents_model, s.deep_agents_max_turns)"`
Expected: `anthropic:claude-sonnet-4-6 100`

**Step 4: Commit**

```bash
git add src/pocketpaw/config.py
git commit -m "feat(config): add deep_agents settings fields"
```

---

## Task 3: Add Optional Dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add `deep-agents` extra after `copilot-sdk` (~line 90)**

```toml
deep-agents = [
    "deepagents>=0.1.0",
]
```

**Step 2: Add to `all-backends` composite extra (~line 204)**

```toml
all-backends = [
    "pocketpaw[openai-agents,google-adk,copilot-sdk,deep-agents,litellm]",
]
```

**Step 3: Add `deepagents` to `all` and `dev` extras (flattened)**

Add `"deepagents>=0.1.0",` to both the `all` and `dev` optional-dependencies lists, and to the `[dependency-groups] dev` list.

**Step 4: Sync deps**

Run: `uv sync --dev`
Expected: `deepagents` installs (or skips if not published yet, that's fine).

**Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "feat(deps): add deepagents as optional dependency"
```

---

## Task 4: Add Tool Bridge Function

**Files:**
- Modify: `src/pocketpaw/agents/tool_bridge.py`
- Test: `tests/test_tool_bridge_deep_agents.py`

**Step 1: Write the failing test**

```python
"""Tests for Deep Agents tool bridge."""

from unittest.mock import MagicMock, patch

from pocketpaw.agents.tool_bridge import _instantiate_all_tools


class TestDeepAgentsToolBridge:
    def test_build_deep_agents_tools_returns_list(self):
        """build_deep_agents_tools returns a list of StructuredTool objects."""
        from pocketpaw.agents.tool_bridge import build_deep_agents_tools
        from pocketpaw.config import Settings

        # Should return empty list when no tools available (graceful degradation)
        with patch.dict("sys.modules", {"langchain_core": None}):
            result = build_deep_agents_tools(Settings(), backend="deep_agents")
            assert result == []

    def test_tools_not_excluded_for_deep_agents(self):
        """Deep Agents backend gets shell/fs tools (not excluded like Claude SDK)."""
        tools = _instantiate_all_tools(backend="deep_agents")
        tool_names = [t.name for t in tools]
        # ShellTool is only excluded for claude_agent_sdk
        # (may not be present if deps missing, but should not be in excluded set)
        assert "deep_agents" != "claude_agent_sdk"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tool_bridge_deep_agents.py -v`
Expected: FAIL with `cannot import name 'build_deep_agents_tools'`

**Step 3: Add `build_deep_agents_tools` to tool_bridge.py**

Add after `build_adk_function_tools` (~line 210):

```python
def build_deep_agents_tools(settings: Any, backend: str = "deep_agents") -> list:
    """Build a list of LangChain ``StructuredTool`` wrappers for PocketPaw tools.

    Deep Agents accepts LangChain tools, plain callables, or dicts. We use
    StructuredTool for the richest schema support.

    Only tools permitted by the active ToolPolicy are included.

    Args:
        settings: A ``Settings`` instance used to build the ToolPolicy.

    Returns:
        List of ``langchain_core.tools.StructuredTool`` objects (empty if not installed).
    """
    try:
        from langchain_core.tools import StructuredTool
    except ImportError:
        logger.debug("langchain-core not installed — returning empty tools list")
        return []

    policy = ToolPolicy(
        profile=settings.tool_profile,
        allow=settings.tools_allow,
        deny=settings.tools_deny,
    )

    registry = ToolRegistry(policy=policy)
    for tool in _instantiate_all_tools(backend=backend):
        registry.register(tool)

    structured_tools: list = []
    for tool_name in registry.allowed_tool_names:
        tool = registry.get(tool_name)
        if tool is None:
            continue

        wrapper = _make_langchain_wrapper(tool)
        structured_tools.append(wrapper)

    logger.info("Built %d LangChain StructuredTools from PocketPaw tools", len(structured_tools))
    return structured_tools


def _make_langchain_wrapper(tool: Any):
    """Create a LangChain StructuredTool wrapper for a PocketPaw tool."""
    from langchain_core.tools import StructuredTool

    defn = tool.definition
    params_schema = dict(defn.parameters) if defn.parameters else {"type": "object"}

    async def _run(**kwargs: str) -> str:
        try:
            return await tool.execute(**kwargs)
        except Exception as exc:
            logger.error("LangChain tool %s execution error: %s", tool.name, exc)
            return f"Error executing {tool.name}: {exc}"

    return StructuredTool.from_function(
        coroutine=_run,
        name=defn.name,
        description=defn.description,
        args_schema=None,  # Use raw JSON schema instead
    )
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tool_bridge_deep_agents.py -v`
Expected: PASS (the graceful degradation test should pass; the full tool test depends on langchain_core being installed)

**Step 5: Commit**

```bash
git add src/pocketpaw/agents/tool_bridge.py tests/test_tool_bridge_deep_agents.py
git commit -m "feat(tools): add LangChain StructuredTool bridge for deep_agents"
```

---

## Task 5: Implement the Backend

**Files:**
- Create: `src/pocketpaw/agents/deep_agents.py`
- Test: `tests/test_deep_agents_backend.py`

**Step 1: Write the failing test**

```python
"""Tests for Deep Agents backend — mocked (no real SDK needed)."""

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings


class TestDeepAgentsBackendInfo:
    """Tests for static backend metadata."""

    def test_info_name(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert info.name == "deep_agents"

    def test_info_capabilities(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities
        assert Capability.CUSTOM_SYSTEM_PROMPT in info.capabilities

    def test_info_beta(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert info.beta is True

    def test_info_install_hint(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert info.install_hint["pip_spec"] == "pocketpaw[deep-agents]"


class TestDeepAgentsBackendInit:
    """Tests for backend initialization."""

    def test_init_sdk_unavailable(self):
        """Backend gracefully handles missing SDK."""
        with patch.dict("sys.modules", {"deepagents": None}):
            from pocketpaw.agents.deep_agents import DeepAgentsBackend

            backend = DeepAgentsBackend.__new__(DeepAgentsBackend)
            backend.settings = Settings()
            backend._sdk_available = False
            backend._stop_flag = False
            backend._custom_tools = None
            assert backend._sdk_available is False

    def test_custom_tools_cached(self):
        """_build_custom_tools caches the result."""
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        mock_tools = [MagicMock(), MagicMock()]

        with patch(
            "pocketpaw.agents.deep_agents.build_deep_agents_tools",
            return_value=mock_tools,
        ):
            # Reset cache
            backend._custom_tools = None
            result1 = backend._build_custom_tools()
            result2 = backend._build_custom_tools()
            assert result1 is result2


class TestDeepAgentsBackendRun:
    """Tests for the run() async generator."""

    @pytest.mark.asyncio
    async def test_run_sdk_unavailable_yields_error(self):
        """When SDK is missing, run() yields an error event."""
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        backend._sdk_available = False

        events = []
        async for event in backend.run("hello"):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "error"
        assert "not installed" in events[0].content.lower()

    @pytest.mark.asyncio
    async def test_run_streams_message_events(self):
        """run() yields message events from streaming chunks."""
        from pocketpaw.agents.deep_agents import DeepAgentsBackend
        from pocketpaw.agents.protocol import AgentEvent

        backend = DeepAgentsBackend(Settings())
        backend._sdk_available = True
        backend._custom_tools = []

        # Mock the agent graph and streaming
        mock_graph = MagicMock()

        async def mock_astream(*args, **kwargs):
            # Simulate message stream chunks
            yield {
                "type": "messages",
                "data": MagicMock(content="Hello ", type="AIMessageChunk"),
            }
            yield {
                "type": "messages",
                "data": MagicMock(content="world!", type="AIMessageChunk"),
            }

        mock_graph.astream = mock_astream

        with patch(
            "pocketpaw.agents.deep_agents.create_deep_agent",
            return_value=mock_graph,
        ), patch(
            "pocketpaw.agents.deep_agents.init_chat_model",
            return_value=MagicMock(),
        ):
            events = []
            async for event in backend.run("hello"):
                events.append(event)

            message_events = [e for e in events if e.type == "message"]
            assert len(message_events) >= 1
            done_events = [e for e in events if e.type == "done"]
            assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        await backend.stop()
        assert backend._stop_flag is True

    @pytest.mark.asyncio
    async def test_get_status(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        status = await backend.get_status()
        assert status["backend"] == "deep_agents"
        assert "available" in status
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deep_agents_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pocketpaw.agents.deep_agents'`

**Step 3: Implement `DeepAgentsBackend`**

Create `src/pocketpaw/agents/deep_agents.py`:

```python
"""LangChain Deep Agents backend for PocketPaw.

Uses the Deep Agents SDK (pip install deepagents) which provides:
- create_deep_agent() with built-in planning, filesystem, and subagent tools
- LangGraph runtime with durable execution and streaming
- Multi-provider LLM support via langchain init_chat_model
- Pluggable virtual filesystem backends

Requires: pip install deepagents
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import _DEFAULT_IDENTITY, BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class DeepAgentsBackend:
    """Deep Agents backend -- LangChain/LangGraph agent framework."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="deep_agents",
            display_name="Deep Agents (LangChain)",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["write_todos", "read_todos", "task", "ls", "read_file", "write_file"],
            tool_policy_map={
                "write_file": "write_file",
                "read_file": "read_file",
                "task": "shell",
                "ls": "read_file",
            },
            required_keys=[],
            supported_providers=[
                "anthropic", "openai", "google", "ollama",
                "openrouter", "openai_compatible", "litellm",
            ],
            install_hint={
                "pip_package": "deepagents",
                "pip_spec": "pocketpaw[deep-agents]",
                "verify_import": "deepagents",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._custom_tools: list | None = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            import deepagents  # noqa: F401

            self._sdk_available = True
            logger.info("Deep Agents SDK ready")
        except ImportError:
            logger.warning(
                "Deep Agents SDK not installed -- pip install 'pocketpaw[deep-agents]'"
            )

    def _build_custom_tools(self) -> list:
        """Lazily build and cache PocketPaw tools as LangChain StructuredTool wrappers."""
        if self._custom_tools is not None:
            return self._custom_tools
        try:
            from pocketpaw.agents.tool_bridge import build_deep_agents_tools

            self._custom_tools = build_deep_agents_tools(self.settings, backend="deep_agents")
        except Exception as exc:
            logger.debug("Could not build custom tools: %s", exc)
            self._custom_tools = []
        return self._custom_tools

    def _build_model(self) -> Any:
        """Build the model string or instance for Deep Agents.

        Deep Agents accepts a provider:model string for init_chat_model,
        or a pre-built BaseChatModel instance.
        """
        model_str = self.settings.deep_agents_model
        if model_str:
            return model_str
        # Fallback: try to infer from existing provider settings
        return "anthropic:claude-sonnet-4-6"

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
                    "Deep Agents SDK not installed.\n\n"
                    "Install with: pip install 'pocketpaw[deep-agents]'"
                ),
            )
            return

        self._stop_flag = False

        try:
            from deepagents import create_deep_agent
            from langchain.chat_models import init_chat_model

            model_str = self._build_model()
            model = init_chat_model(model_str)
            instructions = system_prompt or _DEFAULT_IDENTITY

            custom_tools = self._build_custom_tools()

            # Build messages list: history + current message
            messages: list[dict[str, str]] = []
            if history:
                for msg in history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if content:
                        messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": message})

            agent = create_deep_agent(
                model=model,
                tools=custom_tools if custom_tools else [],
                system_prompt=instructions,
            )

            # Stream using LangGraph's async streaming
            async for chunk in agent.astream(
                {"messages": messages},
                stream_mode=["updates", "messages"],
                version="v2",
            ):
                if self._stop_flag:
                    break

                chunk_type = chunk.get("type", "")

                if chunk_type == "messages":
                    data = chunk.get("data")
                    if data is None:
                        continue
                    # AIMessageChunk contains streamed tokens
                    content = getattr(data, "content", "")
                    if content and isinstance(content, str):
                        yield AgentEvent(type="message", content=content)

                elif chunk_type == "updates":
                    data = chunk.get("data", {})
                    if not isinstance(data, dict):
                        continue
                    # Check for tool calls in updates
                    for node_name, node_data in data.items():
                        if not isinstance(node_data, dict):
                            continue
                        node_messages = node_data.get("messages", [])
                        for msg in node_messages:
                            # Tool call messages
                            tool_calls = getattr(msg, "tool_calls", None)
                            if tool_calls:
                                for tc in tool_calls:
                                    name = tc.get("name", "Tool")
                                    yield AgentEvent(
                                        type="tool_use",
                                        content=f"Using {name}...",
                                        metadata={"name": name, "input": tc.get("args", {})},
                                    )
                            # Tool response messages
                            if getattr(msg, "type", "") == "tool":
                                tool_name = getattr(msg, "name", "tool")
                                tool_content = getattr(msg, "content", "")
                                if isinstance(tool_content, str):
                                    yield AgentEvent(
                                        type="tool_result",
                                        content=tool_content[:200],
                                        metadata={"name": tool_name},
                                    )

            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error("Deep Agents error: %s", e)
            yield AgentEvent(type="error", content=f"Deep Agents error: {e}")

    async def stop(self) -> None:
        self._stop_flag = True

    async def get_status(self) -> dict[str, Any]:
        return {
            "backend": "deep_agents",
            "available": self._sdk_available,
            "running": not self._stop_flag,
            "model": self.settings.deep_agents_model,
        }
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deep_agents_backend.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pocketpaw/agents/deep_agents.py tests/test_deep_agents_backend.py
git commit -m "feat(agents): implement DeepAgentsBackend with streaming support"
```

---

## Task 6: Wire Up Frontend Settings

**Files:**
- Modify: `src/pocketpaw/frontend/js/app.js`
- Modify: `src/pocketpaw/frontend/js/websocket.js`
- Modify: `src/pocketpaw/frontend/templates/components/modals/settings.html`

**Step 1: Add settings keys to `app.js`**

In the settings keys array (~line 505, after `copilotSdkProvider` etc.), add:

```javascript
'deepAgentsModel', 'deepAgentsMaxTurns',
```

**Step 2: Add to `saveSettings()` in `websocket.js`**

In the `saveSettings()` method (~line 208, after the `opencode` lines), add:

```javascript
deep_agents_model: settings.deepAgentsModel || 'anthropic:claude-sonnet-4-6',
deep_agents_max_turns: parseInt(settings.deepAgentsMaxTurns) || 0,
```

**Step 3: Add settings panel in `settings.html`**

After the Copilot SDK settings section (~line 823), add:

```html
<!-- Deep Agents Settings -->
<div
  x-show="settings.agentBackend === 'deep_agents' && isCurrentBackendAvailable()"
  x-transition
  class="flex flex-col gap-3 pl-4 border-l-2 border-[var(--accent-color)]/30"
>
  <p class="text-[11px] text-[var(--text-secondary)]/70">
    Requires <code class="text-[var(--accent-color)]">pip install deepagents</code>.
    Uses LangChain's <code class="text-[var(--accent-color)]">init_chat_model</code> for multi-provider support.
  </p>
  <div class="flex flex-col gap-1">
    <label class="text-[12px] font-medium text-[var(--text-secondary)]">Model</label>
    <input
      type="text"
      x-model="settings.deepAgentsModel"
      @change="saveSettings()"
      placeholder="anthropic:claude-sonnet-4-6"
      class="w-full bg-black\30 border border-[var(--glass-border)] rounded-[10px] py-2 px-3 text-[13px] text-white focus:outline-none focus:border-[var(--accent-color)] focus:bg-black/40 transition-all placeholder-white/40"
    />
    <small class="text-white/40 text-[10px]">
      Format: provider:model (e.g. openai:gpt-4o, anthropic:claude-sonnet-4-6, google_genai:gemini-2.0-flash)
    </small>
  </div>
  <div class="flex flex-col gap-1">
    <label class="text-[12px] font-medium text-[var(--text-secondary)]">Max Turns</label>
    <input
      type="number"
      x-model="settings.deepAgentsMaxTurns"
      @change="saveSettings()"
      min="1" max="200"
      class="w-full bg-black\30 border border-[var(--glass-border)] rounded-[10px] py-2 px-3 text-[13px] text-white focus:outline-none focus:border-[var(--accent-color)] focus:bg-black/40 transition-all"
    />
  </div>
</div>
```

**Step 4: Update `needsApiKey` / `needsApiKeyForSaving` in `app.js`**

Add `deep_agents` to the backends that don't need a top-level API key (similar to `copilot_sdk`):

- In the `needsApiKey` function (~line 900): add `else if (backend === 'deep_agents') return false;`
- In the `needsApiKeyForSaving` function (~line 925): add `deep_agents` to the return false case alongside `opencode, copilot_sdk`

**Step 5: Manual test**

Run: `uv run pocketpaw --dev`
Open dashboard, go to Settings. Verify "Deep Agents (LangChain) [Beta]" appears in the backend dropdown. Select it and verify the model/max-turns fields appear.

**Step 6: Commit**

```bash
git add src/pocketpaw/frontend/js/app.js src/pocketpaw/frontend/js/websocket.js src/pocketpaw/frontend/templates/components/modals/settings.html
git commit -m "feat(ui): add Deep Agents backend settings to dashboard"
```

---

## Task 7: Update CLAUDE.md Documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update the AgentRouter backend list**

In the Architecture > AgentLoop > AgentRouter > Backend section, add after the `copilot_sdk` entry:

```markdown
- `deep_agents` — LangChain Deep Agents with LangGraph runtime, built-in planning/subagent tools, and multi-provider support. Lives in `agents/deep_agents.py`.
```

**Step 2: Update the config env vars section if needed**

Add `deep_agents` to the `agent_backend` options list in the Key Conventions section.

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add deep_agents backend to architecture docs"
```

---

## Task 8: Run Full Test Suite

**Step 1: Run linter**

Run: `uv run ruff check src/pocketpaw/agents/deep_agents.py tests/test_deep_agents_backend.py tests/test_tool_bridge_deep_agents.py`
Expected: No errors

**Step 2: Run format check**

Run: `uv run ruff format --check src/pocketpaw/agents/deep_agents.py`
Expected: Already formatted

**Step 3: Run all tests**

Run: `uv run pytest --ignore=tests/e2e -x -v`
Expected: All pass

**Step 4: Run registry integration test**

Run: `uv run python -c "from pocketpaw.agents.registry import get_backend_info; info = get_backend_info('deep_agents'); print(info.display_name if info else 'NOT FOUND')"`
Expected: `Deep Agents (LangChain)` (if deepagents is installed) or `NOT FOUND` (graceful degradation if not installed)

---

## Summary of All Files Changed

| File | Action | Purpose |
|------|--------|---------|
| `src/pocketpaw/agents/registry.py` | Modify | Add registry entry |
| `src/pocketpaw/config.py` | Modify | Add settings fields |
| `pyproject.toml` | Modify | Add optional dependency |
| `src/pocketpaw/agents/tool_bridge.py` | Modify | Add LangChain tool bridge |
| `src/pocketpaw/agents/deep_agents.py` | Create | Backend implementation |
| `src/pocketpaw/frontend/js/app.js` | Modify | Frontend settings keys |
| `src/pocketpaw/frontend/js/websocket.js` | Modify | Settings save |
| `src/pocketpaw/frontend/templates/components/modals/settings.html` | Modify | Settings UI panel |
| `CLAUDE.md` | Modify | Architecture docs |
| `tests/test_deep_agents_backend.py` | Create | Backend tests |
| `tests/test_tool_bridge_deep_agents.py` | Create | Tool bridge tests |

## Key Design Decisions

1. **No provider field** -- Deep Agents uses `init_chat_model("provider:model")` natively, so the provider is embedded in the model string. No separate `deep_agents_provider` setting needed.
2. **No session persistence** -- LangGraph supports checkpointing but wiring it in adds complexity. The history injection pattern (used by other backends) works for v1. Checkpointing can be added later.
3. **Built-in tools passthrough** -- Deep Agents has its own planning/filesystem tools. We don't exclude them (unlike Claude SDK where we exclude shell/fs tools). PocketPaw tools are additive.
4. **Beta flag** -- Marked as beta like the other non-Claude backends.
