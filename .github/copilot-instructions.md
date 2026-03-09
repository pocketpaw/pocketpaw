# GitHub Copilot Instructions for PocketPaw

This file gives GitHub Copilot repo-specific guidance so its suggestions align with PocketPaw's architecture and conventions. Read [`CLAUDE.md`](../CLAUDE.md) for the full picture; this file focuses on the most frequently violated rules.

---

## 1. Event-driven message bus

All communication **must** flow through the message bus (`src/pocketpaw/bus/`). The three canonical event types are defined in `bus/events.py`:

| Type | Direction | Purpose |
|---|---|---|
| `InboundMessage` | Channel → Bus | User input from any channel |
| `OutboundMessage` | Bus → Channel | Agent response (supports streaming via `is_stream_chunk` / `is_stream_end`) |
| `SystemEvent` | Internal | Tool activity, errors, thinking — consumed by the dashboard Activity panel |

**Rules:**
- Adapters publish `InboundMessage` via `self._publish_inbound()`. They **never** call agent code directly.
- Agents subscribe to `InboundMessage` and publish `OutboundMessage` / `SystemEvent`. They **never** call adapter methods.
- Do not create new event types without discussion. Extend the metadata dict on existing types first.

---

## 2. Async everywhere

- Every method that performs I/O (network, file, subprocess) **must** be `async def`.
- Use `asyncio.to_thread()` for unavoidable blocking calls (e.g. a sync-only third-party SDK).
- Tests use `pytest-asyncio` in **`asyncio_mode = "auto"`** — no `@pytest.mark.asyncio` decorator needed.
- Never call `asyncio.run()` inside library code. Reserve it for entry points only.

```python
# ✅ correct
async def fetch_data(url: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
    return resp.text

# ❌ wrong — blocks the event loop
def fetch_data(url: str) -> str:
    return requests.get(url).text
```

---

## 3. Protocol-oriented interfaces

Use `Protocol` (not `ABC`) for all public interfaces. Implement the protocol; do **not** subclass the concrete implementation class.

Key protocols:

| Protocol | Location |
|---|---|
| `AgentBackend` | `agents/backend.py` |
| `ToolProtocol` | `tools/protocol.py` |
| `ChannelAdapter` / `BaseChannelAdapter` | `bus/adapters/base.py` |
| `MemoryStoreProtocol` | `memory/protocol.py` |

```python
# ✅ correct — implement the protocol
from pocketpaw.agents.backend import AgentBackend

class MyBackend:
    async def run(self, prompt: str, **kwargs) -> AsyncIterator[AgentEvent]:
        ...

# ❌ wrong — inheriting from the concrete class couples you to internals
from pocketpaw.agents.claude_sdk import ClaudeAgentBackend

class MyBackend(ClaudeAgentBackend):  # noqa: don't do this
    ...
```

---

## 4. Standardised `AgentEvent` stream

Every backend that implements `AgentBackend` must yield `AgentEvent` objects with these fields:

```python
@dataclass
class AgentEvent:
    type: str      # see valid values below
    content: str   # text payload (may be empty string)
    metadata: dict # arbitrary extra data
```

**Valid `type` values** (do not invent new ones without updating `backend.py`):

| Value | Meaning |
|---|---|
| `"text"` | Streamed text chunk from the model |
| `"tool_start"` | A tool invocation is beginning |
| `"tool_result"` | A tool returned its result |
| `"thinking"` | Model is reasoning (extended thinking) |
| `"error"` | Non-fatal error; stream continues |
| `"done"` | Final event — stream is complete |

**Rule: always yield `"done"` last.** The agent loop depends on this to close the stream.

```python
async def run(self, prompt, **kwargs):
    async for chunk in self._model.stream(prompt):
        yield AgentEvent(type="text", content=chunk, metadata={})
    yield AgentEvent(type="done", content="", metadata={})  # required
```

---

## 5. Lazy imports for optional dependencies

Optional SDK imports (discord, slack, anthropic, openai, google-genai, etc.) **must** live inside `__init__` or the first-use method — **never at module scope**.

```python
# ✅ correct — import inside __init__ or first-use method
class DiscordAdapter(BaseChannelAdapter):
    def __init__(self, ...):
        try:
            import discord  # lazy import
            self._discord = discord
        except ImportError as exc:
            raise ImportError("Install pocketpaw[discord]") from exc

# ❌ wrong — top-level import breaks installs that don't have the dep
import discord  # fails for users without pocketpaw[discord]
```

Register new backends in `_BACKEND_REGISTRY` inside `agents/registry.py`:

```python
_BACKEND_REGISTRY: dict[str, str] = {
    "my_new_backend": "pocketpaw.agents.my_module.MyBackend",
    # dotted path is imported lazily by AgentRouter._initialize_agent()
}
```

---

## 6. Where new code goes

### Adding a new agent backend

1. Create `src/pocketpaw/agents/<name>.py` implementing the `AgentBackend` protocol.
2. Import the optional SDK lazily inside `__init__` or `_initialize()`.
3. Register the backend string key in `_BACKEND_REGISTRY` in `agents/registry.py`.
4. Add the optional dependency as an extra in `pyproject.toml` (e.g. `[project.optional-dependencies] mybackend = ["my-sdk>=1.0"]`).
5. Add any legacy name aliases to `_LEGACY_BACKENDS`.
6. Write tests — mock the external SDK so tests run without the real dep.

### Adding a new channel adapter

1. Create `src/pocketpaw/bus/adapters/<name>.py`.
2. Extend `BaseChannelAdapter`.
3. Implement `_on_start()`, `_on_stop()`, and `send(message: OutboundMessage) -> None`.
4. Call `self._publish_inbound(msg)` to push incoming messages to the bus.
5. Add optional dependencies to `pyproject.toml` extras.
6. Register the adapter in the dashboard's channel management system.
7. Write tests — mock the external client and assert bus events.

### Adding a new tool

1. Create `src/pocketpaw/tools/builtin/<name>.py`.
2. Subclass `BaseTool` from `tools/protocol.py`.
3. Implement `name`, `description`, `parameters` (JSON Schema), and `async execute(**params) -> str`.
4. Add the class to `tools/builtin/__init__.py` under the lazy-import block.
5. Add the tool to the correct policy group in `tools/policy.py`.
6. Write tests.

---

## 7. Security non-negotiables

| Rule | Detail |
|---|---|
| Shell command safety | Every shell-executing tool must pass through `security/rails.py` before execution. |
| Secrets in `Settings` only | Never hardcode credentials. Use `get_settings()` and store secrets via `credentials.py` (Fernet-encrypted). New secret fields must be added to `SECRET_FIELDS` in `credentials.py`. |
| Audit logging | Any action gated on `trust_level = "high"` or `"critical"` must emit an audit log entry to `~/.pocketpaw/audit.jsonl`. |
| Channel allowlists | Channel identifiers (guild IDs, user IDs, phone numbers) must be validated against the configured allowlist before processing. |
| No credentials in logs | Never pass API keys, tokens, or user PII to `logging.*` calls. |
| API endpoint auth | Every new REST endpoint must go through the existing auth middleware — no unauthenticated routes. |

---

## 8. Configuration

- All settings live in `config.py` as a Pydantic `Settings` class with **`POCKETPAW_`-prefixed** environment variables.
- Call `get_settings()` **inside functions**, never at module import time (Pydantic reads env vars on construction — module-level calls break testing).
- Persist new settings through `Settings.save()` and the JSON config at `~/.pocketpaw/config.json`.

```python
# ✅ correct
async def my_function():
    settings = get_settings()
    token = settings.my_new_token

# ❌ wrong — evaluated at import time, breaks test isolation
settings = get_settings()
TOKEN = settings.my_new_token
```

Channel-specific env vars follow the pattern `POCKETPAW_<CHANNEL>_<FIELD>` (e.g. `POCKETPAW_DISCORD_BOT_TOKEN`).

---

## 9. Frontend

- The web dashboard (`frontend/`) is **vanilla JS / CSS / HTML**. No React, Vue, bundlers, or npm. No build step.
- Serve new UI via FastAPI + Jinja2 templates already wired in `dashboard.py`.
- Use the existing WebSocket connection for real-time streaming — do not open additional WebSocket connections.
- Keep CSS in the existing stylesheet; do not add `<style>` blocks inline in templates.

---

## 10. Testing expectations

- **Write tests before (or alongside) production code.** PRs without tests for new behaviour will be asked to add them.
- Mock external SDKs so the test suite runs without real API keys or network access.
- Use `pytest-asyncio` (`asyncio_mode = "auto"` — no decorator needed).
- Use `unittest.mock.patch` / `AsyncMock` for async boundaries.
- Tests live in `tests/`. Name files `test_<module>.py` and functions `test_<what_it_does>`.
- Run the full suite before pushing: `uv run pytest --ignore=tests/e2e`.

```python
# Example: mocking an optional SDK
from unittest.mock import AsyncMock, patch

async def test_my_backend_streams_done_event():
    with patch("pocketpaw.agents.my_module.my_sdk") as mock_sdk:
        mock_sdk.Client.return_value.stream = AsyncMock(return_value=aiter(["hello"]))
        backend = MyBackend()
        events = [e async for e in backend.run("hi")]
    assert events[-1].type == "done"
```

---

## 11. Code style

| Rule | Value |
|---|---|
| Linter / formatter | Ruff (`uv run ruff check .` / `uv run ruff format .`) |
| Lint rules | E, F, I, UP |
| Line length | 100 characters |
| Target Python | 3.11 |
| Future annotations | `from __future__ import annotations` at the top of every file |
| Interfaces | `Protocol` over `ABC` for all public interfaces |
| Type hints | Required on all public functions and methods |
| Imports | Standard library → third-party → local; sorted by Ruff |

Pre-commit hooks enforce ruff on every commit. CI blocks merges on lint or test failures.

---

## 12. Common mistakes

| Mistake | Why it breaks | Fix |
|---|---|---|
| `import discord` at module level | Import fails for users who haven't installed `pocketpaw[discord]` | Move import inside `__init__` or first-use method |
| `settings = get_settings()` at module level | Settings are instantiated at import time — test isolation is broken | Call `get_settings()` inside each function |
| Calling agent code directly from an adapter | Couples channel to agent; breaks the bus pattern | Publish an `InboundMessage` to the bus instead |
| Forgetting `yield AgentEvent(type="done", ...)` | Agent loop never closes; stream hangs | Always yield `"done"` as the last event |
| Inline auth in a new endpoint | Bypasses the security model | Use the existing auth middleware |
| Logging an API key or token | Leaks credentials to log files | Log only non-secret identifiers |
| Synchronous I/O in an async function | Blocks the event loop | Use `await asyncio.to_thread(...)` for blocking calls |
| Subclassing a concrete backend instead of the protocol | Tight coupling; breaks when the concrete class changes | Implement `AgentBackend` protocol directly |
| `asyncio.run()` inside library code | Breaks when called from an existing event loop | Reserve `asyncio.run()` for entry points only |
| Missing `SECRET_FIELDS` registration | New secret field stored in plaintext in config | Add field name to `SECRET_FIELDS` in `credentials.py` |
