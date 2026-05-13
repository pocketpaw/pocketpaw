# Composio Integration — v1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add [Composio](https://docs.composio.dev) as a tool provider for the cloud chat agent. Existing custom connectors stay; Composio is layered in as a second tool source so the pocket specialist (and, later, runtime chat) gain access to 200+ pre-built OAuth-managed integrations (Gmail, Slack, GitHub, Drive, Calendar, Linear, …) without us building one OAuth dance per service.

**Architecture (one-paragraph summary):** Composio's `claude_agent_sdk` provider exposes Composio's meta-tools (`COMPOSIO_SEARCH_TOOLS`, `COMPOSIO_GET_TOOL_SCHEMAS`, `COMPOSIO_MULTI_EXECUTE_TOOL`, `COMPOSIO_MANAGE_CONNECTIONS`) as an MCP server. v1 wires this MCP server into the **parent cloud chat agent's** `ClaudeAgentOptions.mcp_servers` (via `src/pocketpaw/agents/claude_sdk.py::_get_mcp_servers`, alongside the existing `pocket_specialist` EE-guarded entry) so the agent can discover and call any Composio toolkit at runtime — no per-toolkit Python glue. The **pocket specialist does NOT get Composio**; when a pocket needs to render Composio-sourced data, the parent agent fetches the data first and passes it into the specialist's brief. A `ComposioConnector` adapter implementing the existing `ConnectorProtocol` is **out of scope for v1**; deferred until the MCP-direct path is proven and we know which toolkits non-agent code (Ripple `$source`, batch jobs) actually needs to call deterministically. Multi-tenancy is handled via Composio's `user_id` parameter, namespaced as `{enterprise_id}:{paw_user_id}` so one Composio account can serve multiple enterprise deployments without identifier collision. When a tool needs auth the user hasn't granted, Composio returns a Connect Link URL; we surface it to the chat as an inline Ripple spec (button → opens link in new tab), and the agent retries the tool after the user authorizes. Composio cloud only for v1 (no self-hosted runtime); revisit when an enterprise customer demands true data isolation.

**Tech Stack:** Python 3.11+, `composio` SDK + `composio_claude_agent_sdk` provider (new deps), claude-agent-sdk (existing), pytest + pytest-asyncio, ruff (line-length 100). Implementation lands under `ee/cloud/composio/` following the 4-file shape (`domain.py`, `dto.py`, `service.py`, `router.py` — though v1 may not need a router).

**Out of scope for v1 (do not add):**
- `ComposioConnector` implementing `ConnectorProtocol` — defer until v2 (per-toolkit adapter layer for Ripple `$source` consumers).
- Migration of existing custom connectors to Composio — keep both stacks running; case-by-case migration is a separate effort.
- Toolkit allow-listing UX in paw-enterprise — v1 uses an env-var allow-list (`POCKETPAW_COMPOSIO_TOOLKITS`); admin UI comes later.
- Self-hosted Composio runtime support — single `COMPOSIO_BASE_URL` env var is the only hook we'll add; full self-host validation is a follow-up.
- Replacing the OSS-side tool registry (`src/pocketpaw/tools/`). This plan only touches the cloud agent path (`ee/cloud/`).
- Composio Triggers / Webhooks. Tool-call path only for v1.

**Convention reminders (from `backend/CLAUDE.md`):**
- Tenant filter on every read. `RequestContext.workspace_id` is required for any Composio session creation.
- Errors via `_core.errors` (`CloudError` subclasses). Never `HTTPException` outside routers.
- Module-level `async def`, not classes. State stays in `RequestContext` or function arguments.
- Run `uv run ruff check . && uv run ruff format .` before commits. Mypy clean.
- `ee/cloud` lives under the 4-file shape; new entity `composio/` follows the same.
- KB rebuild hook will fire on commits touching `ee/cloud/` — let it run.

**Prerequisites:**
- `ee/cloud/connectors/` (4-file shape) merged in. Existing on `feat/pocket-specialist` as of 2026-05-12.
- `ee/ripple/_pockets.py` pocket specialist with `claude_agent_sdk` backend. Existing on `feat/pocket-specialist`.
- `ee/cloud/ripple_normalizer.py` and inline-Ripple chat send loop (per `feedback_inline_ripple_not_pockets`). Existing.

This plan should be implemented on a branch rebased onto a base that has all three. `feat/pocket-specialist` (or its merge into `ee`) is the natural base.

---

## Design decisions (locked)

1. **`user_id` scope: one per end-user, namespaced.** Composio sessions are created with `user_id=f"{enterprise_id}:{paw_user_id}"`. Rationale: per-user OAuth is Composio's design (each human authorizes their own Gmail, Slack, etc.); namespacing prevents collisions if one Composio org serves multiple PocketPaw enterprise deployments.
2. **MCP-direct in the parent chat agent** (v1). The parent agent (claude_agent_sdk backend used by the cloud chat path) gets Composio's meta-tools and discovers/calls toolkits dynamically. The pocket specialist sub-agent does NOT receive Composio MCP — data flows from parent → specialist brief, not specialist → Composio. No `ConnectorProtocol` adapter in v1 — that's v2 work, gated on a concrete need.
3. **Cloud-only for v1.** `COMPOSIO_BASE_URL` env var is the escape hatch; self-hosted parity is a follow-up.
4. **Connect Links render as inline Ripple, not raw markdown.** Per `feedback_inline_ripple_not_pockets` and `reference_inline_ripple_spec_shape` — clickable button, opens in new tab.
5. **Env-var toolkit allow-list for v1.** `POCKETPAW_COMPOSIO_TOOLKITS="gmail,slack,github,googlecalendar,googledrive"`. Per-workspace admin UI deferred.

---

## Task 1: Add dependencies + config

**Files:**
- Modify: `backend/pyproject.toml` — add `composio` and `composio_claude_agent_sdk` to the `[project.optional-dependencies] ee` group (or wherever cloud deps live; check existing `ee` extra).
- Modify: `backend/src/pocketpaw/config.py` — add 4 new settings under the existing `POCKETPAW_` prefix:
  - `composio_api_key: SecretStr | None = None`
  - `composio_base_url: str | None = None` (defaults to Composio cloud)
  - `composio_toolkits: list[str] = []` (parsed from comma-separated env)
  - `composio_enterprise_id: str | None = None` (the namespace prefix for `user_id`; required when `composio_api_key` is set)

**Step 1: Write the failing test**

```python
# backend/tests/cloud/composio/test_config.py
"""Composio config wiring — env-var parsing and required-fields validation."""

from __future__ import annotations

import pytest
from pocketpaw.config import Settings


def test_composio_disabled_by_default() -> None:
    s = Settings(_env_file=None)
    assert s.composio_api_key is None
    assert s.composio_toolkits == []


def test_composio_enabled_requires_enterprise_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_COMPOSIO_API_KEY", "ck_xxx")
    monkeypatch.delenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", raising=False)
    with pytest.raises(ValueError, match="composio_enterprise_id"):
        Settings(_env_file=None)


def test_composio_toolkits_csv_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_COMPOSIO_API_KEY", "ck_xxx")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", "ent_acme")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_TOOLKITS", "gmail, slack ,github")
    s = Settings(_env_file=None)
    assert s.composio_toolkits == ["gmail", "slack", "github"]
```

**Step 2: Run** `uv run pytest tests/cloud/composio/test_config.py -v` — expect ImportError / AttributeError.

**Step 3: Implement** the settings additions + a `model_validator(mode="after")` enforcing the `composio_api_key → composio_enterprise_id` invariant.

**Step 4: Verify** tests pass. Run `uv run ruff check . && uv run mypy .`

---

## Task 2: Composio session factory + user_id namespacing

**Files:**
- Create: `backend/ee/cloud/composio/__init__.py`
- Create: `backend/ee/cloud/composio/domain.py` — `ComposioUserId` value object, `ComposioSessionRef` (lightweight wrapper around the SDK's session object).
- Create: `backend/ee/cloud/composio/service.py` — module-level async functions:
  - `composio_user_id(ctx: RequestContext) -> str` — returns `f"{settings.composio_enterprise_id}:{ctx.user_id}"`.
  - `get_session(ctx: RequestContext) -> ComposioSessionRef` — lazy-initializes a Composio client (singleton per-process, configured from settings), creates a session for the namespaced `user_id`, returns wrapped reference.
  - `is_enabled() -> bool` — convenience for callers; returns `True` iff settings has both api_key and enterprise_id.

**Step 1:** Write failing tests covering: namespacing format, missing-config raises `CloudError`, session factory caches the client across calls within a process.

**Step 2 → 4:** Implement, verify.

**Notes:**
- Composio's client init is sync; wrap any blocking calls in `asyncio.to_thread` if the SDK exposes only sync methods.
- Do not store the session globally — Composio sessions are cheap; create per-request and let GC clean up.
- The client (API-key holder) IS process-global; cache it via `functools.lru_cache(maxsize=1)` or a module-level `_client` variable guarded by an `asyncio.Lock`.

---

## Task 3: MCP server injection into the parent chat agent

**Files:**
- Modify: `backend/src/pocketpaw/agents/claude_sdk.py::_get_mcp_servers` — append a Composio MCP server entry when `composio.service.is_enabled()` returns `True`. Follow the existing `pocket_specialist` pattern: try/except EE import so the OSS install still works without the EE dir present, gated by `self._policy.is_mcp_server_allowed("composio")`.
- Create: `backend/ee/cloud/composio/mcp.py` — `build_composio_mcp_server()` returns the SDK MCP server entry (per `_get_mcp_servers`'s `dict[str, dict]` contract). Tool callables read the current `user_id` from the per-stream contextvars (`_active_workspace_id`, `_active_user_id` in `ee/cloud/chat/agent_service.py`) at call time, NOT at server-build time.
- Explicitly do NOT touch `backend/ee/ripple/_pockets.py`. The pocket specialist must remain Composio-free; if pocket UI needs Composio data, the parent agent fetches and passes it via the specialist brief.

**Step 1:** Write a failing test that builds the claude_sdk backend with Composio settings populated and asserts a `composio` entry appears in `_get_mcp_servers()`. Also assert no Composio entry appears in the pocket-specialist's options when invoked directly (regression guard for the redirected architecture).

**Step 2 → 4:** Implement, verify.

**Critical:**
- The MCP server entry is built once per backend instance, but each tool invocation must resolve the `user_id` fresh from the contextvar — otherwise tools called by user B during user A's pool-shared instance would leak across tenants.
- Toolkit filter: pass `settings.composio_toolkits` as the allow-list to `session.tools(toolkits=[...])` so the agent only sees what's permitted. If the list is empty, default to **no toolkits** (fail closed) and log a warning — never expose all 200+ tools by accident.
- Expose an admin/discovery method `list_available_toolkits()` on `ee/cloud/composio/service.py` (queries Composio for the full catalog) so admins can pick what to put in the allow-list without spelunking docs.

---

## Task 4: Connect Link → inline Ripple spec

**Files:**
- Create: `backend/ee/cloud/composio/connect_link.py` — `as_inline_ripple(url: str, toolkit: str, action_label: str) -> dict` returns a Ripple spec matching `reference_inline_ripple_spec_shape` (a single button that opens `url` in a new tab, with copy like "Connect {toolkit}").
- Modify: `backend/ee/cloud/composio/service.py` — wrap `session.execute()` calls with auth-error detection. When Composio returns a "needs connection" response, extract the Connect Link URL and emit it via the existing agent-message channel as an inline Ripple, rather than letting the agent get a raw error string.

**Step 1:** Write failing tests:
- `as_inline_ripple` returns a `{version, ui}` shape that passes `ripple_normalizer` validation.
- An auth-required Composio response triggers an inline Ripple emission instead of a raw exception.

**Step 2 → 4:** Implement, verify.

**Open question for implementation:** Composio's exact "needs auth" signal shape. Inspect a real failing call before finalizing the detection logic; the docs describe `COMPOSIO_MANAGE_CONNECTIONS` as the discovery path but don't pin down the exception class. Default to feature-flag-gated rollout (`composio_connect_link_inline: bool = True`) so we can disable inline rendering if the detection is too brittle.

---

## Task 5: End-to-end smoke test (manual)

Once Tasks 1–4 land:

1. Set env vars in a dev shell:
   ```
   POCKETPAW_COMPOSIO_API_KEY=<dev-key>
   POCKETPAW_COMPOSIO_ENTERPRISE_ID=ent_dev
   POCKETPAW_COMPOSIO_TOOLKITS=gmail
   ```
2. Start backend (`uv run pocketpaw --dev`) and paw-enterprise (`bun run tauri dev`).
3. From a chat in a pocket: prompt "summarize my last 3 unread emails".
4. Expected: agent calls `COMPOSIO_SEARCH_TOOLS` → `GMAIL_FETCH_EMAILS` (or equivalent), gets a "needs connection" response on first try, chat renders an inline "Connect Gmail" button.
5. Click button → authorize in browser → return to chat.
6. Send the same prompt again. Expected: agent fetches and summarizes.

If step 4 emits a raw URL instead of an inline button, Task 4's detection logic needs tightening.

---

## Test coverage targets

- `tests/cloud/composio/test_config.py` — env parsing + invariants (Task 1).
- `tests/cloud/composio/test_service.py` — user_id namespacing, client caching, is_enabled gate (Task 2).
- `tests/cloud/composio/test_mcp.py` — MCP server build, toolkit filter, fail-closed on empty allow-list (Task 3).
- `tests/cloud/composio/test_connect_link.py` — inline Ripple shape, auth-error detection (Task 4).

No live Composio calls in CI. Mock the `composio` SDK at the `Composio.create()` boundary.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Composio cloud outage takes down all tool-calling | MCP server build wraps `session.tools()` in try/except — on failure, return empty MCP server (agent falls back to its built-in tools and custom connectors) and log loudly. Never let Composio failure 500 the chat. |
| Composio bills per tool call | `POCKETPAW_COMPOSIO_TOOLKITS` allow-list is the cost lever. Default empty = nothing enabled. |
| Tool I/O leaks PII to Composio cloud | Documented in customer-facing docs as a v1 limitation. Enterprise customers requiring isolation must wait for self-hosted runtime support. |
| Composio SDK breaking changes | Pin `composio` to a single minor version in `pyproject.toml`. Bump deliberately. |
| `user_id` collision across enterprise deployments | Namespacing (Task 2) prevents this; enforced by the `composio_enterprise_id` required-when-enabled invariant (Task 1). |

---

## Follow-ups (NOT in this plan, but should land before any customer GA)

1. **v2: `ComposioConnector` per-toolkit adapter** implementing `ConnectorProtocol.execute` — so Ripple `$source` and non-agent code paths can call Composio toolkits with the same interface as custom connectors.
2. **Per-workspace toolkit allow-list** stored on the workspace document, with an admin UI in paw-enterprise's Settings → Integrations panel. Env var becomes the cluster-wide default; workspace can narrow it.
3. **Self-hosted Composio validation** — confirm parity for at least Gmail/Slack/GitHub against a self-hosted Composio instance before promising it to a customer.
4. **Audit logging** — every Composio tool execution emits an audit event (`composio.tool.executed`) with toolkit, action, user_id, latency. Hook into existing `_core/audit` (if it exists) or add it.
5. **Composio Triggers / Webhooks** — receive Composio-side events (new email, new Slack message) and route them through the existing in-process bus. Big enough to be its own plan.
6. **OSS-side integration** — once cloud path is stable, consider exposing Composio to `src/pocketpaw/` users via the same MCP injection pattern on the `claude_agent_sdk` OSS backend.
