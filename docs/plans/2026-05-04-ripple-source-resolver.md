# Ripple `$source` Resolver — v1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a server-side resolver so pocket `rippleSpec.state` can declare `{"$source": "<name>", ...args}` markers that get replaced with live workspace data on read, instead of the LLM fabricating literal arrays.

**Architecture:** New module `backend/ee/cloud/ripple_resolver.py` walks the spec recursively, replacing `{"$source": ...}` dicts via a small registry of async source functions. The resolver runs **on read** inside `pockets.service.get` after `_pocket_to_domain` and before `pocket_to_wire_dict`. Mongo stores the spec verbatim (markers intact); writes are not touched. v1 ships two sources: `workspace.pockets` and `workspace.members`. Unknown source names log a warning and return `None` — never raise — so a stale spec can't break the canvas.

**Tech Stack:** Python 3.11+, Beanie 1.x (MongoDB), pytest + pytest-asyncio (`asyncio_mode = "auto"`), existing `ee/cloud` 4-file shape, ruff (line-length 100).

**Out of scope for v1 (do not add):** `pocket.tool_call` source, client write-back of state mutations, `action: tool` handlers from canvas buttons, realtime source-invalidation, caching layer, source pagination beyond a `limit` arg.

**Convention reminders (from `backend/CLAUDE.md`):**
- Tenant filter on every read. `ResolveCtx.workspace_id` is required.
- Errors via `_core.errors`, never `HTTPException` in non-router code.
- Module-level `async def`, not classes.
- Run `uv run ruff check . && uv run ruff format .` before commits.

---

### Task 1: Resolver module + walker (no sources yet)

**Files:**
- Create: `backend/ee/cloud/ripple_resolver.py`
- Create: `backend/tests/cloud/test_ripple_resolver.py`

**Step 1: Write the failing tests**

```python
# backend/tests/cloud/test_ripple_resolver.py
"""Tests for ripple $source resolver — walker behavior, no real sources."""

from __future__ import annotations

import pytest

from ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec


@pytest.fixture
def ctx() -> ResolveCtx:
    return ResolveCtx(workspace_id="w1", user_id="u1", pocket_id="p1")


async def test_empty_spec_returns_empty(ctx: ResolveCtx) -> None:
    assert await resolve_ripple_spec({}, ctx) == {}


async def test_spec_without_sources_is_identity(ctx: ResolveCtx) -> None:
    spec = {
        "state": {"draft": "", "next_id": 3, "tasks": [{"id": "t1", "title": "x"}]},
        "ui": {"type": "flex", "props": {"direction": "column"}, "children": []},
    }
    assert await resolve_ripple_spec(spec, ctx) == spec


async def test_resolver_does_not_mutate_input(ctx: ResolveCtx) -> None:
    spec = {"state": {"a": [1, 2, 3]}, "ui": {"type": "stat"}}
    snapshot = {"state": {"a": [1, 2, 3]}, "ui": {"type": "stat"}}
    await resolve_ripple_spec(spec, ctx)
    assert spec == snapshot
```

**Step 2: Run tests to verify they fail**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py -v
```
Expected: FAIL with `ImportError: cannot import name 'ResolveCtx'`.

**Step 3: Write minimal implementation**

```python
# backend/ee/cloud/ripple_resolver.py
"""Ripple $source resolver — replaces {"$source": "<name>", ...args} markers
in pocket rippleSpecs with live workspace data on read.

Reads only. Persistence stores markers verbatim; resolution happens in
pockets.service.get before wire-dict conversion. Unknown sources log
and return None — they MUST NOT raise, so a stale spec can't brick the
canvas.

Sources are registered via @register("name"). Each source is an async
function (ResolveCtx, args) -> Any. Tenancy is the source's
responsibility — every Mongo read MUST scope by ctx.workspace_id.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_KEY = "$source"


@dataclass(frozen=True)
class ResolveCtx:
    workspace_id: str
    user_id: str
    pocket_id: str


SourceFn = Callable[[ResolveCtx, dict[str, Any]], Awaitable[Any]]
_REGISTRY: dict[str, SourceFn] = {}


def register(name: str) -> Callable[[SourceFn], SourceFn]:
    def deco(fn: SourceFn) -> SourceFn:
        _REGISTRY[name] = fn
        return fn
    return deco


async def resolve_ripple_spec(spec: dict[str, Any], ctx: ResolveCtx) -> dict[str, Any]:
    """Walk spec, replace {"$source": ...} dicts with resolved values.
    Returns a new structure; input is not mutated."""
    return await _walk(spec, ctx)


async def _walk(node: Any, ctx: ResolveCtx) -> Any:
    if isinstance(node, dict):
        if SOURCE_KEY in node:
            return await _resolve_marker(node, ctx)
        return {k: await _walk(v, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [await _walk(item, ctx) for item in node]
    return node


async def _resolve_marker(marker: dict[str, Any], ctx: ResolveCtx) -> Any:
    name = marker.get(SOURCE_KEY)
    if not isinstance(name, str):
        logger.warning("ripple_resolver: $source value is not a string: %r", name)
        return None
    fn = _REGISTRY.get(name)
    if fn is None:
        logger.warning("ripple_resolver: unknown $source %r", name)
        return None
    args = {k: v for k, v in marker.items() if k != SOURCE_KEY}
    try:
        return await fn(ctx, args)
    except Exception:
        logger.exception("ripple_resolver: source %r failed", name)
        return None


__all__ = ["ResolveCtx", "SOURCE_KEY", "register", "resolve_ripple_spec"]
```

**Step 4: Run tests to verify they pass**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py -v
```
Expected: 3 passed.

**Step 5: Commit**

```bash
git add backend/ee/cloud/ripple_resolver.py backend/tests/cloud/test_ripple_resolver.py
git commit -m "feat(ripple): scaffold \$source resolver walker (no sources yet)"
```

---

### Task 2: Marker resolution + unknown-source safety

**Files:**
- Modify: `backend/tests/cloud/test_ripple_resolver.py` (append)

**Step 1: Write the failing tests**

```python
# Append to backend/tests/cloud/test_ripple_resolver.py
from ee.cloud.ripple_resolver import register

# Module-level: register a test-only source. Re-registration overwrites,
# so this is safe across test reloads.
@register("test.echo")
async def _echo(ctx, args):
    return {"workspace_id": ctx.workspace_id, "args": args}


@register("test.boom")
async def _boom(ctx, args):
    raise RuntimeError("source intentionally failed")


async def test_top_level_marker_replaced(ctx: ResolveCtx) -> None:
    spec = {"state": {"hello": {"$source": "test.echo", "n": 5}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"hello": {"workspace_id": "w1", "args": {"n": 5}}}}


async def test_nested_marker_replaced(ctx: ResolveCtx) -> None:
    spec = {
        "ui": {
            "type": "kanban",
            "props": {"data": {"$source": "test.echo"}},
        }
    }
    out = await resolve_ripple_spec(spec, ctx)
    assert out["ui"]["props"]["data"] == {"workspace_id": "w1", "args": {}}


async def test_unknown_source_returns_none_does_not_raise(ctx: ResolveCtx) -> None:
    spec = {"state": {"x": {"$source": "does.not.exist"}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"x": None}}


async def test_failing_source_returns_none_does_not_raise(ctx: ResolveCtx) -> None:
    spec = {"state": {"x": {"$source": "test.boom"}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"x": None}}


async def test_non_string_source_name_returns_none(ctx: ResolveCtx) -> None:
    spec = {"state": {"x": {"$source": 42}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"x": None}}
```

**Step 2: Run tests to verify they pass**

These exercise code that already exists from Task 1; they should all pass on first run. If any fail, fix the resolver before moving on.

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py -v
```
Expected: 8 passed.

**Step 3: Commit**

```bash
git add backend/tests/cloud/test_ripple_resolver.py
git commit -m "test(ripple): cover marker dispatch, unknown-source, error paths"
```

---

### Task 3: `workspace.pockets` source

**Files:**
- Create: `backend/ee/cloud/ripple_sources.py`
- Modify: `backend/tests/cloud/test_ripple_resolver.py` (append source-specific tests)

**Read first (do not modify, just study):**
- `backend/ee/cloud/pockets/service.py:242-254` — existing `list_pockets` (returns wire dicts including the full rippleSpec — too heavy for source use).
- `backend/ee/cloud/models/pocket.py` — `_PocketDoc` shape.

**Why a separate `ripple_sources.py` module:** keeps `ripple_resolver.py` free of cloud-domain imports. The resolver is the dispatcher; sources live next to (and import from) the entities they read.

**Step 1: Write the failing test**

```python
# Append to backend/tests/cloud/test_ripple_resolver.py
from unittest.mock import AsyncMock, patch


async def test_workspace_pockets_source_returns_metadata_for_workspace(ctx):
    # Importing the sources module triggers @register side-effects.
    import ee.cloud.ripple_sources  # noqa: F401

    fake_docs = [
        type("D", (), {
            "id": "p1", "name": "Bookings", "type": "business",
            "icon": "calendar", "color": "#0A84FF",
        })(),
        type("D", (), {
            "id": "p2", "name": "Notes", "type": "deep-work",
            "icon": "note", "color": "#30D158",
        })(),
    ]

    class _FakeFind:
        def __init__(self, docs): self._docs = docs
        async def to_list(self): return self._docs

    with patch(
        "ee.cloud.ripple_sources._PocketDoc.find",
        return_value=_FakeFind(fake_docs),
    ) as find_mock:
        spec = {"state": {"all": {"$source": "workspace.pockets"}}}
        out = await resolve_ripple_spec(spec, ctx)

    assert out["state"]["all"] == [
        {"id": "p1", "name": "Bookings", "type": "business",
         "icon": "calendar", "color": "#0A84FF"},
        {"id": "p2", "name": "Notes", "type": "deep-work",
         "icon": "note", "color": "#30D158"},
    ]
    # Tenancy invariant: every find call must include workspace.
    args, kwargs = find_mock.call_args
    query = args[0] if args else kwargs
    assert "workspace" in str(query) and "w1" in str(query)
```

**Step 2: Run test to verify it fails**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py::test_workspace_pockets_source_returns_metadata_for_workspace -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'ee.cloud.ripple_sources'`.

**Step 3: Write minimal implementation**

```python
# backend/ee/cloud/ripple_sources.py
"""Concrete sources for the ripple $source resolver.

Importing this module registers every source via @register decorators.
``ripple_resolver`` itself stays free of cloud-domain imports — sources
live here, next to the entities they read.

Tenancy rule (from CLAUDE.md ee/cloud rule 7): every Mongo read MUST
scope by ctx.workspace_id.
"""

from __future__ import annotations

import logging
from typing import Any

from ee.cloud.models.pocket import Pocket as _PocketDoc
from ee.cloud.ripple_resolver import ResolveCtx, register

logger = logging.getLogger(__name__)


@register("workspace.pockets")
async def _workspace_pockets(ctx: ResolveCtx, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Return id+metadata for every pocket in the workspace.
    Visibility filter mirrors pockets.service.list_pockets — owner,
    shared_with, or workspace-visible. The full rippleSpec is excluded
    (would be wasteful and recursive)."""
    docs = await _PocketDoc.find(
        {
            "workspace": ctx.workspace_id,
            "$or": [
                {"owner": ctx.user_id},
                {"shared_with": ctx.user_id},
                {"visibility": "workspace"},
            ],
        }
    ).to_list()
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "type": d.type,
            "icon": d.icon,
            "color": d.color,
        }
        for d in docs
    ]


__all__ = ["_workspace_pockets"]
```

**Step 4: Run test to verify it passes**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py::test_workspace_pockets_source_returns_metadata_for_workspace -v
```
Expected: PASS.

**Step 5: Verify nothing else regressed**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py -v
```
Expected: 9 passed.

**Step 6: Commit**

```bash
git add backend/ee/cloud/ripple_sources.py backend/tests/cloud/test_ripple_resolver.py
git commit -m "feat(ripple): workspace.pockets source"
```

---

### Task 4: `workspace.members` source

**Files:**
- Modify: `backend/ee/cloud/ripple_sources.py` (append)
- Modify: `backend/tests/cloud/test_ripple_resolver.py` (append)

**Read first:**
- `backend/ee/cloud/workspace/service.py:577` — existing `list_member_ids(workspace_id)`. Lower-level, no `RequestContext` required, returns just IDs.
- `backend/ee/cloud/workspace/service.py:303` — `list_members(ctx, workspace_id)` — returns enriched member objects but requires building a `RequestContext`.

**Decision:** wrap `list_member_ids` and join to user lookup directly in the source; avoid synthesizing a `RequestContext`. If the existing `list_members` is more convenient, build a minimal `RequestContext` there — but prefer the lower-level helper.

**Step 1: Write the failing test**

```python
# Append to backend/tests/cloud/test_ripple_resolver.py
async def test_workspace_members_source_returns_member_list(ctx):
    import ee.cloud.ripple_sources  # noqa: F401

    with patch(
        "ee.cloud.ripple_sources._list_workspace_members",
        new=AsyncMock(return_value=[
            {"id": "u1", "name": "Alex", "email": "a@x", "role": "owner"},
            {"id": "u2", "name": "Brit", "email": "b@x", "role": "member"},
        ]),
    ):
        spec = {"state": {"team": {"$source": "workspace.members"}}}
        out = await resolve_ripple_spec(spec, ctx)
    assert out["state"]["team"] == [
        {"id": "u1", "name": "Alex", "email": "a@x", "role": "owner"},
        {"id": "u2", "name": "Brit", "email": "b@x", "role": "member"},
    ]
```

**Step 2: Run test to verify it fails**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py::test_workspace_members_source_returns_member_list -v
```
Expected: FAIL — `_list_workspace_members` doesn't exist yet.

**Step 3: Write minimal implementation**

```python
# Append to backend/ee/cloud/ripple_sources.py

async def _list_workspace_members(workspace_id: str) -> list[dict[str, Any]]:
    """Indirection so tests can patch a single seam.
    Implementation calls into ee.cloud.workspace.service — kept private
    to insulate the resolver from changes in that module's signature."""
    from ee.cloud.workspace import service as _ws

    member_ids = await _ws.list_member_ids(workspace_id)
    # For v1 we return just IDs as the wire shape — the workspace
    # service layer is already responsible for hydration when
    # callers need names/avatars. Pockets that want richer data
    # should call list_members through a future RequestContext-aware
    # path. Keep v1 small.
    return [{"id": uid} for uid in member_ids]


@register("workspace.members")
async def _workspace_members(ctx: ResolveCtx, args: dict[str, Any]) -> list[dict[str, Any]]:
    return await _list_workspace_members(ctx.workspace_id)
```

**Step 4: Update test to match v1 wire shape**

The earlier test asserts a richer shape than v1 returns. Adjust the test to the actual v1 contract (ids only) — and add a follow-up TODO comment in the test pointing at the future enrichment.

```python
async def test_workspace_members_source_returns_member_list(ctx):
    import ee.cloud.ripple_sources  # noqa: F401

    with patch(
        "ee.cloud.ripple_sources._list_workspace_members",
        new=AsyncMock(return_value=[{"id": "u1"}, {"id": "u2"}]),
    ):
        spec = {"state": {"team": {"$source": "workspace.members"}}}
        out = await resolve_ripple_spec(spec, ctx)
    # v1 ships ids only; richer hydration (names, avatars, roles) is
    # tracked for v2 along with a RequestContext-aware path.
    assert out["state"]["team"] == [{"id": "u1"}, {"id": "u2"}]
```

**Step 5: Run tests to verify they pass**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py -v
```
Expected: 10 passed.

**Step 6: Commit**

```bash
git add backend/ee/cloud/ripple_sources.py backend/tests/cloud/test_ripple_resolver.py
git commit -m "feat(ripple): workspace.members source (v1: ids only)"
```

---

### Task 5: Wire resolver into `pockets.service.get`

**Files:**
- Modify: `backend/ee/cloud/pockets/service.py:257-267`
- Modify: `backend/tests/cloud/` — add a service-level test (or extend existing pocket service tests).

**Read first:**
- `backend/ee/cloud/pockets/service.py:257` — current `get` body.
- `backend/tests/cloud/` — locate the existing pocket service tests; reuse their fixtures (Mongo setup) rather than re-rolling.

```bash
cd D:/paw/backend && uv run pytest --collect-only tests/cloud/ 2>&1 | grep -i pocket | head -20
```

**Step 1: Write the failing service-level test**

Place it next to the closest existing pocket service test file (likely `tests/cloud/test_pocket_*.py`). The test creates a pocket whose `rippleSpec.state.all` is a `$source` marker, calls `service.get`, and asserts the marker is replaced.

```python
# Suggested location: backend/tests/cloud/test_pocket_service_resolver.py
"""service.get must resolve $source markers in rippleSpec on read."""

from __future__ import annotations

import pytest

from ee.cloud.pockets import service as pocket_service


async def test_get_resolves_source_markers(workspace_fixture, user_fixture):
    """Reuse existing fixtures — replace names with whatever exists."""
    spec = {
        "state": {
            "all": {"$source": "workspace.pockets"},
            "draft": "",
        },
        "ui": {"type": "flex", "props": {"direction": "column"}, "children": []},
    }
    pocket_id = await pocket_service.create_from_ripple_spec(
        workspace_id=workspace_fixture.id,
        owner_id=user_fixture.id,
        ripple_spec=spec,
    )
    out = await pocket_service.get(pocket_id, user_fixture.id)
    state = out["rippleSpec"]["state"]
    assert isinstance(state["all"], list)  # resolved
    assert state["draft"] == ""  # untouched
    # The source marker must NOT remain in the resolved output.
    assert "$source" not in str(state["all"])
```

If existing fixtures use different names, adapt the imports to match. Do NOT invent a new fixture system.

**Step 2: Run test to verify it fails**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_pocket_service_resolver.py -v
```
Expected: FAIL — `service.get` does not yet call the resolver, so `state["all"]` is still the raw marker dict.

**Step 3: Modify `pockets.service.get`**

Open `backend/ee/cloud/pockets/service.py:257-267` and change `get`:

```python
async def get(pocket_id: str, user_id: str) -> dict:
    """Get a single pocket. Access check: owner, shared_with, or workspace-visible.
    rippleSpec $source markers are resolved on read against the calling user's
    workspace context."""
    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)
    if (
        pocket.owner != user_id
        and user_id not in pocket.shared_with
        and pocket.visibility == "private"
    ):
        raise Forbidden("pocket.access_denied", "You do not have access to this pocket")
    if pocket.ripple_spec:
        from ee.cloud import ripple_sources  # noqa: F401  — register sources
        from ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec

        resolved = await resolve_ripple_spec(
            pocket.ripple_spec,
            ResolveCtx(
                workspace_id=doc.workspace,
                user_id=user_id,
                pocket_id=str(doc.id),
            ),
        )
        pocket = pocket.model_copy(update={"ripple_spec": resolved})
    return pocket_to_wire_dict(pocket)
```

**Notes:**
- Local imports inside `get` keep module-import time clean and avoid circular-import risk during boot.
- `pocket.model_copy` assumes the domain class is a Pydantic model; check `pockets/domain.py` and adapt (`_replace` for dataclass, `dataclasses.replace`, etc.) if not.
- `list_pockets` (line 242) is intentionally NOT touched — it returns metadata only, no spec.

**Step 4: Run test to verify it passes**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_pocket_service_resolver.py -v
```
Expected: PASS.

**Step 5: Run the broader pocket service test suite to check for regressions**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/ -k pocket -v
```
Expected: all pocket-related tests pass. Investigate any failures before committing.

**Step 6: Commit**

```bash
git add backend/ee/cloud/pockets/service.py backend/tests/cloud/test_pocket_service_resolver.py
git commit -m "feat(pockets): resolve \$source markers on read in service.get"
```

---

### Task 6: Teach the agent — `<state-sources>` prompt block

**Files:**
- Modify: `backend/ee/ripple/_pockets.py`
- Create: `backend/tests/cloud/test_pocket_prompt_state_sources.py`

**Read first:**
- `backend/ee/ripple/_pockets.py:675-699` — `_assemble_creation` and `_assemble_interaction`.
- `backend/tests/cloud/test_pocket_prompts_single_source.py` — pattern for prompt-content regression tests.

**Step 1: Write the failing test**

```python
# backend/tests/cloud/test_pocket_prompt_state_sources.py
"""Regression: every creation prompt teaches the $source mechanism."""

from __future__ import annotations

import pytest

from ee.ripple._pockets import (
    POCKET_CREATION_PROMPT_CLI,
    POCKET_CREATION_PROMPT_MCP,
)


@pytest.mark.parametrize(
    "prompt", [POCKET_CREATION_PROMPT_MCP, POCKET_CREATION_PROMPT_CLI]
)
def test_creation_prompts_contain_state_sources_block(prompt: str) -> None:
    assert "<state-sources>" in prompt
    assert "</state-sources>" in prompt
    # The two v1 sources must be named so the agent learns the allowlist.
    assert "workspace.pockets" in prompt
    assert "workspace.members" in prompt
    # The literal marker syntax must appear so the agent emits the right shape.
    assert '"$source"' in prompt


@pytest.mark.parametrize(
    "prompt", [POCKET_CREATION_PROMPT_MCP, POCKET_CREATION_PROMPT_CLI]
)
def test_state_sources_block_appears_before_examples(prompt: str) -> None:
    """Agents anchor on examples; the rule must come first so the example
    can demonstrate it."""
    sources_idx = prompt.index("<state-sources>")
    examples_idx = prompt.index("<creation-examples>")
    assert sources_idx < examples_idx
```

**Step 2: Run test to verify it fails**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_pocket_prompt_state_sources.py -v
```
Expected: FAIL — the block doesn't exist yet.

**Step 3: Add the `_STATE_SOURCES_BLOCK` and splice it into `_assemble_creation`**

In `backend/ee/ripple/_pockets.py`, add a new constant near `_INTERACTIVE_DEFAULT_BLOCK`:

```python
_STATE_SOURCES_BLOCK = """\
<state-sources>
For lists or values that should reflect REAL workspace data — pockets in
this workspace, members of this workspace — do NOT inline literal arrays.
Emit a `$source` marker and let the server hydrate it on read:

  "state": {
    "all_pockets": {"$source": "workspace.pockets"},
    "team":        {"$source": "workspace.members"},
    "draft":       ""
  }

The server replaces each marker with live data before the canvas renders.
Available v1 sources:

- `workspace.pockets`  → list of {id, name, type, icon, color} for every
  pocket the user can see in this workspace.
- `workspace.members`  → list of {id} for workspace members. (Richer
  member fields land in v2.)

Use literal arrays ONLY for canvas-local UI state the user types in
themselves: `draft` inputs, `next_id` counters, todo rows the user adds
via the Add button. Never invent business data the user expects to be
real (bookings, customers, revenue, alerts) — if no source exists, omit
the widget rather than fabricating rows.

Unknown source names resolve to `null`. Stick to the allowlist above.
</state-sources>
"""
```

Then update `_assemble_creation` (around line 675) to splice it in **between** `_INTERACTIVE_DEFAULT_BLOCK` and the examples:

```python
def _assemble_creation(*, mcp: bool) -> str:
    parts = [
        _SCOPE_BLOCK,
        _CANVAS_BLOCK,
        _LIST_BEFORE_CREATE_MCP if mcp else _LIST_BEFORE_CREATE_CLI,
        _TOOLS_MCP if mcp else _TOOLS_CLI,
        _CREATION_OVERVIEW_MCP if mcp else _CREATION_OVERVIEW_CLI,
        _INTERACTIVE_DEFAULT_BLOCK,
        _STATE_SOURCES_BLOCK,           # <-- new
        _CREATION_EXAMPLES_MCP if mcp else _CREATION_EXAMPLES_CLI,
        _RESEARCH_PROTOCOL,
        RIPPLE_DESIGN_RULES,
    ]
    return "\n".join(parts) + "\n"
```

`_assemble_interaction` is intentionally NOT updated in v1 — interactive edits to existing pockets shouldn't be teaching new primitives. Add the block to interaction prompts only after we've watched a few real edits.

**Step 4: Run test to verify it passes**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_pocket_prompt_state_sources.py -v
```
Expected: PASS.

**Step 5: Verify the existing prompt-single-source regression still passes**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_pocket_prompts_single_source.py -v
```
Expected: still PASS — we only ADDED a block.

**Step 6: Commit**

```bash
git add backend/ee/ripple/_pockets.py backend/tests/cloud/test_pocket_prompt_state_sources.py
git commit -m "feat(ripple): teach pocket-creation agent the \$source mechanism"
```

---

### Task 7: End-to-end smoke + lint + format

**Files:**
- (no new code — verification only)

**Step 1: Run the full ripple+pockets test slice**

```bash
cd D:/paw/backend && uv run pytest tests/cloud/test_ripple_resolver.py tests/cloud/ -k pocket -v
```
Expected: all green.

**Step 2: Run lint and format**

```bash
cd D:/paw/backend && uv run ruff check ee/cloud/ripple_resolver.py ee/cloud/ripple_sources.py ee/cloud/pockets/service.py ee/ripple/_pockets.py
cd D:/paw/backend && uv run ruff format ee/cloud/ripple_resolver.py ee/cloud/ripple_sources.py ee/cloud/pockets/service.py ee/ripple/_pockets.py
```
Expected: no lint errors, no format diff (or commit any format-only diff separately with `chore: ruff format`).

**Step 3: Run mypy on the new modules**

```bash
cd D:/paw/backend && uv run mypy ee/cloud/ripple_resolver.py ee/cloud/ripple_sources.py
```
Expected: no type errors. If mypy flags `Beanie` shape mismatches, narrow with explicit casts rather than disabling — sources are tenancy-critical.

**Step 4: Final commit (if format made changes)**

```bash
git add -u
git commit -m "chore: ruff format ripple resolver modules"
```

**Step 5: Update the wiki**

Per `backend/CLAUDE.md`, the wiki auto-rebuilds on commits that touch `ee/cloud/`. After the last commit, confirm the rebuild ran (the post-commit hook should fire) or run manually:

```bash
cd D:/paw/backend && kb build ./ee/cloud --scope paw-cloud --output docs/wiki/ 2>&1 | tail -10
```

If the hook is not configured, skip — don't add it as part of this PR.

---

## Done criteria

- [ ] All 8 resolver-walker tests pass.
- [ ] Both `workspace.*` source tests pass with patched seams.
- [ ] `pocket_service.get` returns resolved spec for a real pocket created via `create_from_ripple_spec`.
- [ ] Both creation prompts (MCP + CLI) include `<state-sources>` block; existing single-source regression still passes.
- [ ] `ruff check` clean, `mypy` clean on new files.
- [ ] No changes to write paths (`create_pocket`, `update_pocket`, `create_from_ripple_spec`, `list_pockets`).
- [ ] No new dependencies in `pyproject.toml`.

## Manual verification (after merge)

1. Start the backend and create a pocket via the cloud chat agent with the prompt: *"make me a pocket that lists all my other pockets"*.
2. The agent should emit a spec with `"state": {"all": {"$source": "workspace.pockets"}}`.
3. Open the pocket in `paw-enterprise`. The list should populate with real workspace pockets, not LLM-fabricated names.
4. Create a second pocket. Re-open the first. The list should now include the second one (because the resolver runs on every read).

If step 4 doesn't reflect the new pocket, the renderer is caching — out of scope for this plan, but file a follow-up.
