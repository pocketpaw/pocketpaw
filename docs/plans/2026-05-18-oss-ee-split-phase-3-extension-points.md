# OSS/EE Split — Phase 3: Introduce Extension Points Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove every `from pocketpaw_ee.*` import from `pocketpaw/` core. Replace lazy try/except imports with `typing.Protocol` extension points; have `pocketpaw_ee` register implementations via Python entry-points. After this phase, an `import-linter` contract enforces `pocketpaw → pocketpaw_ee` is never imported, and `pip install pocketpaw` (with no `pocketpaw_ee` on disk) yields a working OSS install.

**Architecture:** Inversion of control. Core defines small, narrow Protocols for each extension surface (tools, models, routes, agent extensions, storage backends). `pocketpaw_ee` registers concrete implementations via `[project.entry-points]` in its pyproject.toml. Core discovers them via `importlib.metadata.entry_points()` at startup, lazily where possible.

**Tech Stack:** Same as prior phases; adds the `entry_points` discovery pattern (stdlib `importlib.metadata`).

**Reference:** Design doc Sections 3–4. Depends on Phases 1 and 2 being merged.

---

## Scope

Phase 3 covers every remaining `from pocketpaw_ee.*` import inside `src/pocketpaw/`. From a fresh grep on the post-Phase-2 tree, the surfaces requiring conversion are (illustrative — re-grep at execution time):

| Core file | Imports today | Extension-point group |
|---|---|---|
| `pocketpaw/features.py` | `pocketpaw_ee.cloud.features` | `pocketpaw.features` (capability registry) |
| `pocketpaw/api/serve.py`, `pocketpaw/dashboard.py` | `pocketpaw_ee.cloud.mount_cloud`, `pocketpaw_ee.cloud.socketio_server.wrap_asgi_app` | `pocketpaw.routes` |
| `pocketpaw/dashboard_lifecycle.py` | `pocketpaw_ee.cloud.db.init_cloud_db`, `pocketpaw_ee.cloud.auth.core.*`, `pocketpaw_ee.cloud.sessions.title_listener.register` | `pocketpaw.lifecycle` (startup hooks) |
| `pocketpaw/bootstrap/context_builder.py` | `pocketpaw_ee.cloud.embeddings.build_embedder` | `pocketpaw.embeddings` |
| `pocketpaw/memory/manager.py` | `pocketpaw_ee.cloud.memory.mongo_store.MongoMemoryStore` | `pocketpaw.memory_backends` |
| `pocketpaw/agents/pool.py`, `agents/loop.py`, `agents/codex_cli.py`, `agents/claude_sdk.py`, `agents/deep_agents.py`, `agents/google_adk.py`, `agents/openai_agents.py`, `agents/sdk_mcp_pocket.py`, `agents/tool_bridge.py` | `pocketpaw_ee.cloud.models.*`, `pocketpaw_ee.cloud.shared.errors.*`, `pocketpaw_ee.cloud.composio.providers.*`, `pocketpaw_ee.agent.pocket_specialist.*`, `pocketpaw_ee.cloud.chat.agent_service.*` | `pocketpaw.models`, `pocketpaw.tools`, `pocketpaw.agent_extensions` |
| `pocketpaw/api/v1/chat.py` | `pocketpaw_ee.cloud.auth.core.current_optional_user` | `pocketpaw.auth` |
| `pocketpaw/api/v1/pockets.py` | (post Phase 2, ripple already in core; check residuals) | (verify) |
| `pocketpaw/runtime/connector_bus.py` | `pocketpaw_ee.cloud.shared.events.event_bus` | `pocketpaw.events` |
| `pocketpaw/uploads/resolver.py` | `pocketpaw_ee.cloud.uploads.router._ADAPTER`, `_META` | `pocketpaw.storage_backends` |
| `pocketpaw/tools/cli.py` | many `pocketpaw_ee.cloud.*` admin calls | leave as CLI-only commands; consider gating with capability check rather than wiring through Protocols (admin CLI for an EE install is a reasonable exception) |
| `pocketpaw/tools/builtin/fabric_tools.py`, `instinct_tools.py`, `instinct_corrections.py` | after Phase 2, fabric/instinct are core; residuals are `pocketpaw_ee.api.get_*_store` → `pocketpaw.api.get_*_store` with EE override registered via `pocketpaw.stores` entry-point |

---

## Pre-flight

```bash
git checkout main
git pull
git checkout -b chore/oss-ee-phase-3-extension-points
uv sync --dev
uv run pytest --ignore=tests/e2e -q
```

Capture an authoritative current list of violations:
```bash
grep -rn "from pocketpaw_ee\.\|import pocketpaw_ee\." src/pocketpaw --include="*.py" > .phase-3-violations.txt
wc -l .phase-3-violations.txt
```

---

## Task 1: Define `pocketpaw/extensions.py` with all Protocols

**File:** `src/pocketpaw/extensions.py` (new)

Define narrow Protocols, one per extension surface. Example skeleton:

```python
"""Extension-point Protocols for pluggable PocketPaw functionality.

Concrete implementations live in `pocketpaw_ee` (and potentially third-party
packages). They are discovered at runtime via `importlib.metadata.entry_points`
groups documented below. Core code must never import from `pocketpaw_ee`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolProvider(Protocol):
    """Entry-point group: pocketpaw.tools"""
    def get_tools(self) -> list[Any]: ...
    priority: int  # higher wins on conflicts


@runtime_checkable
class ModelProvider(Protocol):
    """Entry-point group: pocketpaw.models"""
    def beanie_document_models(self) -> list[type]: ...


@runtime_checkable
class RouteProvider(Protocol):
    """Entry-point group: pocketpaw.routes"""
    def fastapi_routers(self) -> list[tuple[str, Any]]:
        """Returns list of (mount_path, APIRouter)."""
        ...


@runtime_checkable
class LifecycleHook(Protocol):
    """Entry-point group: pocketpaw.lifecycle"""
    async def on_startup(self, app: Any) -> None: ...
    async def on_shutdown(self, app: Any) -> None: ...


@runtime_checkable
class AgentExtension(Protocol):
    """Entry-point group: pocketpaw.agent_extensions"""
    def install(self, agent_runtime: Any) -> None: ...


@runtime_checkable
class MemoryBackend(Protocol):
    """Entry-point group: pocketpaw.memory_backends"""
    name: str
    def build(self, config: Any) -> Any: ...


@runtime_checkable
class StorageBackend(Protocol):
    """Entry-point group: pocketpaw.storage_backends"""
    name: str
    def adapter(self) -> Any: ...
    def meta(self) -> Any: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Entry-point group: pocketpaw.embeddings"""
    def build_embedder(self, config: Any) -> Any: ...


@runtime_checkable
class AuthProvider(Protocol):
    """Entry-point group: pocketpaw.auth"""
    def current_optional_user(self) -> Any: ...


@runtime_checkable
class EventBusProvider(Protocol):
    """Entry-point group: pocketpaw.events"""
    def get_event_bus(self) -> Any: ...
```

Commit:
```bash
git add src/pocketpaw/extensions.py
git commit -m "feat(core): define extension-point Protocols for OSS/EE boundary"
```

---

## Task 2: Add the discovery registry

**File:** `src/pocketpaw/_registry.py` (new, internal)

```python
"""Discovers extension implementations registered via importlib entry-points.

Caches results per group. Implementations are imported lazily on first access
so that an OSS install with no `pocketpaw_ee` package on disk does not pay
import cost for cloud features.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import entry_points
from typing import Any


@lru_cache(maxsize=None)
def providers(group: str) -> list[Any]:
    eps = entry_points(group=group)
    return [ep.load()() for ep in eps]


def first(group: str) -> Any | None:
    items = providers(group)
    return items[0] if items else None


def has(group: str) -> bool:
    return bool(entry_points(group=group))
```

Commit.

---

## Task 3: Convert violations one extension surface at a time

For each surface in the scope table, do these steps in order. Treat each surface as its own commit for reviewability.

**Pattern (illustrated with `pocketpaw.embeddings`):**

**Step 1: Identify the EE implementation to register.**
Find the concrete class/function in `pocketpaw_ee` (e.g. `pocketpaw_ee.cloud.embeddings.build_embedder`).

**Step 2: Wrap it as an entry-point class in `pocketpaw_ee`.**

`ee/pocketpaw_ee/cloud/embeddings/__init__.py` (add):
```python
class CloudEmbeddingProvider:
    def build_embedder(self, config):
        from .builder import build_embedder  # local import to avoid cycles
        return build_embedder(config)
```

**Step 3: Register the entry-point in `ee/pyproject.toml`** (Phase 4 creates this file; for Phase 3, put the entry-points in the root `pyproject.toml` under `[project.entry-points]` and migrate them to the new EE pyproject in Phase 4):

```toml
[project.entry-points."pocketpaw.embeddings"]
cloud = "pocketpaw_ee.cloud.embeddings:CloudEmbeddingProvider"
```

**Step 4: Rewrite the core consumer.**

`src/pocketpaw/bootstrap/context_builder.py` (was):
```python
try:
    from pocketpaw_ee.cloud.embeddings import build_embedder
    embedder = build_embedder(config)
except ImportError:
    embedder = None
```

becomes:
```python
from pocketpaw._registry import first

provider = first("pocketpaw.embeddings")
embedder = provider.build_embedder(config) if provider else None
```

**Step 5: Test that core no longer imports from `pocketpaw_ee` at this site.**
```bash
grep -n "pocketpaw_ee" src/pocketpaw/bootstrap/context_builder.py
```
Expected: empty.

**Step 6: Run the relevant test suites.**

**Step 7: Commit.**
```bash
git add src/pocketpaw/bootstrap/context_builder.py ee/pocketpaw_ee/cloud/embeddings/__init__.py pyproject.toml
git commit -m "feat(core): replace embeddings cross-import with entry-point provider"
```

**Repeat for each surface.** Suggested order (low → high risk):

1. `pocketpaw.events` (one site)
2. `pocketpaw.embeddings`
3. `pocketpaw.memory_backends`
4. `pocketpaw.storage_backends`
5. `pocketpaw.auth`
6. `pocketpaw.lifecycle` (multiple call sites in `dashboard_lifecycle.py`)
7. `pocketpaw.routes` (mount points in `api/serve.py`, `dashboard.py`)
8. `pocketpaw.models` (used by `agents/pool.py`, `agents/loop.py`, etc. — likely the largest)
9. `pocketpaw.tools` (composio + builtin)
10. `pocketpaw.agent_extensions` (pocket specialist, chat agent_service)
11. `pocketpaw.features` (capability presence registry)

`pocketpaw/tools/cli.py` is admin CLI; leave it gated behind a runtime check that `pocketpaw_ee` is installed rather than rewiring through Protocols. Document the exception.

---

## Task 4: Add the `import-linter` enforcement contract

**File:** `pyproject.toml`

Append a new contract:

```toml
[[tool.importlinter.contracts]]
name = "Core may not import from EE"
type = "forbidden"
source_modules = ["pocketpaw"]
forbidden_modules = ["pocketpaw_ee"]
ignore_imports = [
    "pocketpaw.tools.cli -> pocketpaw_ee.*",  # admin CLI exception, documented
]
```

Run:
```bash
uv run lint-imports
```
Expected: green. Any other violation = an extension surface was missed; either convert it or add a justified ignore with a comment.

Commit.

---

## Task 5: Add CI job A — OSS-only build

**File:** existing CI config (likely `.github/workflows/*.yml` — discover during execution).

Add a job that:
1. Sets up a temp directory with only `src/`, `tests/`, root `pyproject.toml`, `LICENSE`.
2. `pip install .` (no `pocketpaw_ee`).
3. Runs `pytest --ignore=tests/e2e --ignore=tests/cloud --ignore=tests/ee` to prove the OSS subset works in isolation.
4. Runs `python -c "import pocketpaw; print(pocketpaw.__version__)"` and `python -c "import pocketpaw_ee" 2>&1 | grep ModuleNotFoundError`.

Implementation sketch:
```yaml
oss-only-build:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v3
    - name: Build OSS-only sdist
      run: |
        mkdir _oss && cd _oss
        cp -r ../src ../tests ../pyproject.toml ../LICENSE ../README.md .
        uv venv && . .venv/bin/activate
        uv pip install -e .
        pytest --ignore=tests/e2e --ignore=tests/cloud --ignore=tests/ee -q
        python -c "import pocketpaw; print(pocketpaw.__file__)"
        ! python -c "import pocketpaw_ee" 2>/dev/null
```

Commit.

---

## Task 6: Verify and clean up

```bash
grep -rn "from pocketpaw_ee\|import pocketpaw_ee" src/pocketpaw --include="*.py" | grep -v "pocketpaw/tools/cli.py"
```
Expected: empty.

```bash
rm .phase-3-violations.txt
uv run ruff check . --fix
uv run ruff format .
uv run mypy .
uv run lint-imports
uv run pytest --ignore=tests/e2e -q
uv run pytest tests/cloud -q
uv run pytest tests/ee -q
```
All green at parity.

---

## Task 7: Open PR

Title: `feat(core): replace EE cross-imports with entry-point Protocols (Phase 3)`

Body: list of converted extension surfaces, the `import-linter` contract added, the new CI job, and a callout to the `tools/cli.py` exception.

---

## Definition of done

- [ ] `grep -rn "from pocketpaw_ee\|import pocketpaw_ee" src/pocketpaw --include="*.py"` returns only the documented `tools/cli.py` exception
- [ ] `src/pocketpaw/extensions.py` and `src/pocketpaw/_registry.py` exist
- [ ] `import-linter` "Core may not import from EE" contract passes in CI
- [ ] OSS-only CI job passes (proves core builds and tests pass without `pocketpaw_ee` on disk)
- [ ] All EE entry-points declared in `pyproject.toml` (Phase 4 will migrate to `ee/pyproject.toml`)
- [ ] All test suites at parity
- [ ] PR opened, CI green
