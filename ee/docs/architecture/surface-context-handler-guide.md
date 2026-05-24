# Surface Context — Handler Guide

Created: 2026-05-24
Status: Living document — companion to [`surface-context.md`](./surface-context.md).

How to add a new surface handler. The architecture doc explains *why* the module exists; this guide explains *how* to extend it.

---

## 1. When to add a new handler

Add a handler when one of two things is true:

- **A new paw-enterprise route gained a chat bar** and the agent should see what's on the surface. The route also needs a new `SurfaceKind` enum value plus a client stamp in `paw-enterprise/src/lib/core/chat/surface-context.ts:mapRouteToSurface`.
- **An existing minimal-placeholder handler should become rich** because its upstream service finally exposed the helper it was waiting on. The current placeholders (`knowledge.py`, `calendar.py`, `sidepanel.py`) name their expected upstream helper in the module docstring.

If neither applies — for instance, you're tweaking the wording of an existing preamble or adding a new tag to a block — that's an in-place edit on the existing handler, not a new module.

---

## 2. The handler contract

Every handler exports exactly one async function with this signature:

```python
async def build_preamble(
    workspace_id: str,
    user_id: str,
    meta: SurfaceMeta,
) -> str:
```

Four invariants:

- **Read-only.** No writes, no event emits. The preamble is a snapshot of state the user can already see; it never mutates.
- **Workspace-scoped.** Every read passes `workspace_id` (or its `RequestContext` equivalent). When an upstream service gates by id alone — `pockets_service.get`, `agents_service.get` — the handler adds its own workspace check on the returned object (see Section 5).
- **Idempotent.** Calling `build_preamble` twice with the same inputs returns the same string. No hidden state, no incrementing counters, no logging side effects that change next-call behaviour.
- **Fails to empty string on any error.** Catch broadly, log at `debug`, return an empty string or the canonical `(unavailable)` snapshot. Never raise — the resolver also catches, but the handler-local catch keeps the failure mode crisp (the resolver sees a return, not an exception, and the surface tag stays in the preamble).

The dispatcher in `service.py` passes the validated `meta` and tenancy tuple. Individual handlers don't re-derive either.

---

## 3. Worked example — `handlers/agents.py`

`agents.py` is the simplest rich handler. ~30 lines, one upstream read, one snapshot block, one list block. The full source — used here as a template:

```python
# agents.py — /agents surface preamble.
#
# Created: 2026-05-24 — Workspace agents list. Reads via
# ``agents_service.list_agents`` (tenancy via workspace_id).

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the agents-list surface preamble."""
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agents = await agents_service.list_agents(workspace_id)
    except Exception:
        logger.debug("agents_handler: list failed", exc_info=True)
        return (
            '<surface kind="agents" route="/agents" />'
            "<agents-snapshot>(unavailable)</agents-snapshot>"
        )

    parts = [
        '<surface kind="agents" route="/agents" />',
        f'<agents-snapshot count="{len(agents)}" />',
    ]
    if not agents:
        parts.append("<agents-list>(no agents in workspace)</agents-list>")
    else:
        rows = []
        for a in agents[:LIST_LIMIT]:
            name = getattr(a, "name", None) or "(unnamed)"
            slug = getattr(a, "slug", None) or "?"
            rows.append(f"- {name} (slug={slug})")
        if len(agents) > LIST_LIMIT:
            rows.append(f"... (+{len(agents) - LIST_LIMIT} more)")
        parts.append("<agents-list>\n" + "\n".join(rows) + "\n</agents-list>")
    return truncate_preamble("\n".join(parts))


__all__ = ["build_preamble"]
```

Five things to note for your own handler:

1. **Lazy import the upstream service.** `from pocketpaw_ee.cloud.agents import service as agents_service` lives inside `build_preamble`, not at module top. This keeps a handler-module import failure from cascading — the resolver's `_load_handlers` pulls every handler module on first call, and a broken upstream-service import would otherwise nuke the whole surface module's dispatch table.
2. **One try/except around the upstream read.** Broad catch. Log at `debug`. Return the canonical `<…-snapshot>(unavailable)</…-snapshot>` shape with the surface tag preserved so the agent still knows the route.
3. **Workspace-scope guard.** Here it's implicit — `agents_service.list_agents(workspace_id)` enforces tenancy on the query path. Compare with `handlers/agent.py` for the explicit guard pattern when the upstream service doesn't.
4. **Cap the list.** `LIST_LIMIT = 10`, then `... (+N more)` for the tail. Don't dump unbounded lists into a 1500-char budget.
5. **Always wrap with `truncate_preamble`.** Even when you know the output is short. The helper is the single line-aware-truncation point and the only thing keeping handler-local renderings consistent with the preamble cap.

For richer examples: `handlers/home.py` shows multiple blocks composed together (`<pinned-widgets>`, `<live-snapshot>`, `<available-data-tools>`, `<recent-activity>`). `handlers/pocket.py` and `handlers/agent.py` show the explicit tenancy-guard pattern.

---

## 4. Registering the handler

Wiring a new handler is mechanical. The steps:

1. **Add the `SurfaceKind` enum value.** If the surface is genuinely new, append it to `ee/pocketpaw_ee/cloud/surface/domain.py:SurfaceKind`. The string value must match what the client stamps from `surface-context.ts:mapRouteToSurface`.
2. **Drop the handler module.** Create `ee/pocketpaw_ee/cloud/surface/handlers/<surface_name>.py`. Use `handlers/agents.py` as the template. Add the file-top comment block per the project convention.
3. **Register in the dispatch table.** Edit `ee/pocketpaw_ee/cloud/surface/service.py:_load_handlers`:
   - Add the handler import to the alphabetical block at the top of the function (use the same `import <module> as <module>_handler` pattern when the module name shadows a stdlib name).
   - Add the `SurfaceKind.<NAME>: <module>.build_preamble` entry to the returned dict.
4. **Add tests.** Mirror the existing `tests/cloud/surface/test_*_handler.py` shape (see Section 6).

The dispatch table is the single source of truth. No additional registration is needed — no plugin auto-discovery, no setup.py entry point, no `__init__.py` re-export of the handler. The resolver and tests are the only consumers of the table.

---

## 5. Tenancy checklist

Before merging, walk through the three questions:

1. **Does every read pass `workspace_id`?** Look at every upstream call in your handler. Either the upstream signature takes `workspace_id` (or `RequestContext` carrying it) — or it doesn't, in which case go to question 2.
2. **Does the handler reject a `meta` with a cross-workspace id?** If your upstream gates by id alone (no workspace filter), you must fetch the object and compare its workspace field against the chat's `workspace_id`. Reject mismatches via the canonical `(unavailable)` snapshot path. See `handlers/pocket.py:_load_pocket`, `handlers/agent.py`, and `handlers/pocket_widget.py:_pocket_in_workspace` for three working examples.
3. **Does a handler failure return empty (not crash)?** Try running the handler against an offline mock that raises on the upstream call. Assert the return is a string (with or without the surface tag) — not an exception. The resolver also catches, but the handler-local catch keeps the preamble shape recognisable.

A "yes" to all three is the merge gate.

---

## 6. Testing pattern

Each handler test file mirrors this four-test shape:

```python
# tests/cloud/surface/test_<surface_name>_handler.py
#
# Created: <date> — <one-line summary of what's covered>.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import <surface_name> as handler

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-surface-<name>"


async def test_<name>_happy_path() -> None:
    """Seeded data renders into the expected block."""
    # Seed the workspace via the canonical service (not a Beanie write).
    # Drive the handler. Assert the rendered preamble carries the tag and
    # the seeded names.
    ...


async def test_<name>_empty_state() -> None:
    """Empty workspace still renders the surface tag and a usable placeholder."""
    # No seeding. Drive the handler. Assert <surface kind="..."> is in the
    # preamble and the count attribute is 0 / the empty marker text appears.
    ...


async def test_<name>_failure_fallback(monkeypatch) -> None:
    """Upstream raising returns the canonical (unavailable) snapshot."""
    # Monkeypatch the upstream service call to raise. Drive the handler.
    # Assert the return is a string carrying the surface tag and the
    # (unavailable) marker — not an exception.
    ...


async def test_<name>_rejects_cross_workspace_stamp() -> None:
    """A meta pointing at another workspace's artifact falls through."""
    # Seed artifact in WORKSPACE_B owned by the SAME user. Drive handler
    # with workspace_id=WORKSPACE_A and meta pointing at the B artifact.
    # Assert the B artifact's identifying data (name, slug, etc.) does
    # NOT appear in the preamble — it falls through to the unavailable
    # snapshot path.
    ...
```

The fourth test is only required when the handler uses `meta` to fetch by id (`pocket.py`, `agent.py`, `pocket_widget.py`). List-style handlers (`agents.py`, `pockets_list.py`, `files.py`) don't take an id from the client and can skip it.

For reference cases:

- **Happy path + empty state** — see `tests/cloud/surface/test_home_handler.py`.
- **Resolver-level failure fallback** — see `tests/cloud/surface/test_surface_service.py::test_resolve_handler_failure_returns_empty_preamble` for the monkeypatch pattern.
- **Cross-workspace guards** — see `tests/cloud/surface/test_tenancy_guards.py` for all four (pocket, pocket_widget, agent, activity).

The `mongo_db` fixture from the cloud test conftest gives you a real workspace-scoped collection set. Seeding through the canonical service (`pockets_service.ensure_home_pocket`, `agents_service.create`, etc.) rather than direct Beanie writes is the rule — it exercises the same code path the chat router would hit and matches the pattern of every existing test file.
