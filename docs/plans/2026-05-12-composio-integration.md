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
6. **CLI path explicitly ruled out.** A `composio` CLI + Claude-Code-skill-via-Bash alternative was evaluated and rejected. Composio's CLI is single-identity by design: auth state lives in `~/.composio/`, and no general command accepts `--user-id` (only `composio dev playground-execute` does, and it's a developer sandbox). In a per-enterprise backend serving multiple end-users, the CLI cannot scope per user without either misusing the dev command or building our own user-switching layer on top of `~/.composio/`. Multi-tenant per-end-user execution is an SDK-only concern. Re-evaluate only if Composio ships a general `--user-id` flag on `composio execute`.

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
- **Direct-tools mode is deliberate** (see `providers.py:215-235`). We use `composio.tools.get(user_id, toolkits=[...])` (direct tools), NOT `composio.create(user_id).tools()` (meta-tools-only). Reason captured in the code comment: production LLMs hallucinate when given only meta-tools, so we surface concrete tool names (`GMAIL_SEND_EMAIL`, `GITHUB_LIST_ISSUES_FOR_REPOSITORY`, …) the model can pattern-match against. Do NOT regress to meta-tools-only.
- **No curated allow-list of action names.** Enterprise customers need the full toolkit surface — picking 15 "common" GitHub actions for them would cripple legitimate use cases. The per-toolkit `limit` parameter caps the count for tool-index sanity (Task 3a), but the cap selects breadth across the toolkit, not a hand-picked subset.
- Toolkit-level allow-list: `settings.composio_toolkits` controls which toolkits are exposed at all (gmail, slack, github, …). Empty list = fail closed (no tools fetched, warning logged). This is a tenancy/cost control, not a per-action curation.
- Expose an admin/discovery method `list_available_toolkits()` on `ee/cloud/composio/service.py` (queries Composio for the full catalog) so admins can pick what to put in `composio_toolkits` without spelunking docs.

---

## Task 3a: Search-fallback meta-tools alongside direct tools

**Problem.** With ~50 tools/toolkit × N toolkits, the agent's tool index can exceed the runtime's threshold for inline schema loading, falling back to a deferred index where `ToolSearch` returns alphabetical batches. Concretely: a user asked for "open issues and PRs assigned to me" and the agent couldn't surface `GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS` because the deferred-tool index kept returning A–C matches. Curating action names is rejected (enterprise can use anything); reducing per-toolkit count too aggressively hides legitimate actions; pure meta-tools-only causes LLM hallucination (`providers.py:215-235`). The fix is a **hybrid**: keep direct tools as the primary surface, append the 3 search-flow meta-tools as a discovery fallback the agent can call when its built-in tool index misses.

**Files:**
- Modify: `backend/ee/cloud/composio/providers.py::_build_session_and_tools` — after the per-toolkit `composio.tools.get(...)` loop, also fetch `COMPOSIO_SEARCH_TOOLS`, `COMPOSIO_GET_TOOL_SCHEMAS`, `COMPOSIO_MULTI_EXECUTE_TOOL` (one call: `composio_client.tools.get(user_id=..., tools=[...])`) and append them to `combined`. Do NOT include `COMPOSIO_MANAGE_CONNECTIONS` — we already own that flow via `connection_tool.py` and don't want the agent fragmenting between two auth paths.
- Modify: `backend/ee/cloud/chat/agent_service.py` (system prompt assembly) — add a Composio-search instruction block, gated on `composio_service.is_enabled()`:
  ```
  ## Composio fallback discovery

  Your Composio tools cover the most common actions per toolkit, but
  not every action is loaded into your tool index. If you need a
  Composio action you can't find directly:
    1. Call COMPOSIO_SEARCH_TOOLS with a keyword (e.g. "search issues
       pull requests").
    2. From the results, pick the exact tool name.
    3. Call COMPOSIO_GET_TOOL_SCHEMAS([tool_name]) to load the schema.
    4. Call COMPOSIO_MULTI_EXECUTE_TOOL with the resolved tool + args.
  Use COMPOSIO_SEARCH_TOOLS only as a fallback — prefer direct tools
  when they're already in your tool list.
  ```
- Modify: `backend/tests/cloud/composio/test_providers.py` (or whichever existing test file covers `build_tools_for_backend`) — add tests for the new behavior.

**Step 1: Write the failing tests**

```python
# tests/cloud/composio/test_providers.py (excerpt)
async def test_search_fallback_meta_tools_appended(mock_composio_client):
    """The 3 search-flow meta-tools must be appended to the direct tools
    so the agent has a discovery fallback when its tool index can't
    surface a specific action."""
    mock_composio_client.tools.get.side_effect = _mock_tools_get  # returns toolkit tools per call
    reset_cache_for_tests()
    tools = build_tools_for_backend(BACKEND_CLAUDE_SDK, settings=_settings_with(["gmail", "github"]))
    tool_names = {_tool_name(t) for t in tools}
    assert "COMPOSIO_SEARCH_TOOLS" in tool_names
    assert "COMPOSIO_GET_TOOL_SCHEMAS" in tool_names
    assert "COMPOSIO_MULTI_EXECUTE_TOOL" in tool_names
    # We OWN the connect flow — don't expose Composio's MANAGE_CONNECTIONS too.
    assert "COMPOSIO_MANAGE_CONNECTIONS" not in tool_names


async def test_search_fallback_failure_is_non_fatal(mock_composio_client):
    """If the meta-tools fetch fails, direct tools must still be returned —
    losing search fallback degrades capability but doesn't break the turn."""
    mock_composio_client.tools.get.side_effect = _mock_tools_get_meta_fails
    reset_cache_for_tests()
    tools = build_tools_for_backend(BACKEND_CLAUDE_SDK, settings=_settings_with(["gmail"]))
    tool_names = {_tool_name(t) for t in tools}
    assert "GMAIL_FETCH_EMAILS" in tool_names  # direct tools still present
    assert "COMPOSIO_SEARCH_TOOLS" not in tool_names  # meta-tools absent
```

**Step 2: Run** — expect AssertionError on the missing meta-tools.

**Step 3: Implement**
- After the existing per-toolkit fetch loop in `_build_session_and_tools`, add a single bounded fetch for the 3 meta-tool slugs:
  ```python
  try:
      meta_tools = composio_client.tools.get(
          user_id=namespaced_user_id,
          tools=[
              "COMPOSIO_SEARCH_TOOLS",
              "COMPOSIO_GET_TOOL_SCHEMAS",
              "COMPOSIO_MULTI_EXECUTE_TOOL",
          ],
      )
  except Exception:  # noqa: BLE001
      logger.warning(
          "Composio: meta-tools fetch failed — agent runs without search fallback this turn"
      )
      meta_tools = []
  for t in meta_tools or []:
      name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
      if name and name in seen_names:
          continue
      if name:
          seen_names.add(name)
      combined.append(t)
  ```
- System-prompt block: append in the existing Composio-availability branch of `agent_service.py`. Gate on `is_enabled()` so OSS-only runs don't reference tools the agent can't call.

**Step 4: Verify** — tests pass, lint clean.

**Notes:**
- The `tools=[name,...]` form of `composio.tools.get` is documented; verify the exact kwarg name against the installed SDK (might be `actions=` in some minor versions).
- Per-toolkit `limit` default stays at 50 for now. The search fallback covers the deferred-tool case without forcing us to lower coverage.
- If we ever see the `tools=[name,...]` form return nothing for meta-tools (because the SDK requires a different surface for them), fall back to instantiating the meta-tool wrappers from `composio.tools_router` (or equivalent) directly. Validate at implementation time against the installed SDK version.

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

## Task 4a: Post-connection identity verification

Composio does **not** expose a uniform `whoami(user_id, toolkit)` primitive. `connected_accounts.get()` returns the OAuth token state but not the underlying GitHub login / Gmail address / Slack team. The documented pattern is toolkit-specific identity probes. Without this step, a user can authorize the wrong account (e.g. a personal GitHub instead of their work one) and the agent silently operates as the wrong identity — this is the bug surfaced in initial testing where a personal GitHub account got bound to a session expected to be a different teammate's.

**Files:**
- Create: `backend/ee/cloud/composio/identity.py` — per-toolkit identity-probe registry:
  ```python
  IDENTITY_PROBES: dict[str, IdentityProbe] = {
      "github": IdentityProbe(action="GITHUB_GET_AUTHENTICATED_USER", field="login"),
      "gmail":  IdentityProbe(action="GMAIL_USERS_GET_PROFILE",       field="emailAddress"),
      "slack":  IdentityProbe(action="SLACK_AUTH_TEST",               field="user"),
      "googlecalendar": IdentityProbe(action="GOOGLECALENDAR_GET_CURRENT_USER", field="email"),
      "googledrive":    IdentityProbe(action="GOOGLEDRIVE_GET_ABOUT",           field="user.emailAddress"),
      # extend per toolkit added to the allow-list
  }
  async def probe_identity(ctx, toolkit: str) -> str | None: ...
  ```
- Create: `backend/ee/cloud/composio/domain.py` — extend with `ComposioConnection` value object (frozen, fields: `workspace_id`, `paw_user_id`, `toolkit`, `external_identity`, `verified_at`).
- Create: `backend/ee/cloud/models/composio_connection.py` — Beanie document, unique index on `(workspace, paw_user_id, toolkit)`.
- Create: `backend/ee/cloud/composio/service.py::record_connection(ctx, toolkit, external_identity)` — upserts the doc, emits `ComposioConnectionVerified` event.
- Modify: `backend/ee/cloud/composio/service.py` — after a Connect Link auth flow completes, the next agent turn runs `probe_identity` for the just-connected toolkit, stores the result via `record_connection`, and emits an inline Ripple confirmation ("Connected as `@octocat`. Continue / disconnect?") via the existing chat-send loop.
- Modify: `backend/ee/cloud/composio/connect_link.py` — `as_inline_ripple` gains a "verification pending" variant that shows the identity once it's known.

**Step 1: Write failing tests** covering:
- Unknown toolkit returns `None` from `probe_identity` and logs (does not raise).
- Successful probe upserts `ComposioConnection` with the expected `external_identity`.
- A subsequent probe for the same `(workspace, user, toolkit)` with a **different** `external_identity` flags loudly (event emission + log.warning) — this is the tripwire: re-authorizing as a different person should not silently overwrite.
- The post-link inline Ripple includes the resolved `external_identity` text.

**Step 2 → 4:** Implement, verify.

**Critical:**
- The probe MUST happen before the original failing tool call retries — otherwise we burn a request as the wrong identity. Sequence is: (1) auth-required signal → (2) emit Connect Link → (3) user authorizes → (4) probe identity → (5) emit confirmation → (6) wait for explicit "yes/continue" → (7) retry original tool. NOT: probe-and-immediately-retry-original.
- The stored `external_identity` is a **tripwire**, not just a label. Future probe mismatches must surface to the user, not be silently overwritten.
- Probe failure (network, toolkit doesn't expose a whoami action) is non-fatal: log a warning, store `external_identity=None`, and let the user proceed (better than blocking when an obscure toolkit lacks a probe). Add the missing toolkit to `IDENTITY_PROBES` later.
- Multi-tenancy: storing identity verification on a per-`(workspace_id, paw_user_id, toolkit)` key — not global — is required so two paw users in different workspaces can each have their own connected GitHub.

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
6. Expected: chat renders an inline "Connected as `<your-email>`. Continue?" confirmation (Task 4a). Click "Continue".
7. Expected: agent fetches and summarizes.
8. **Identity tripwire check:** disconnect Gmail in Composio dashboard, re-authorize as a **different** Gmail account. Send the same prompt. Expected: chat surfaces a "Connected as `<new-email>` — this differs from previously verified `<old-email>`. Confirm?" warning, not silent reuse.

If step 4 emits a raw URL instead of an inline button, Task 4's detection logic needs tightening. If step 6 doesn't include the email/login, Task 4a's probe registry is missing the toolkit. If step 8 silently proceeds without warning, the tripwire in `record_connection` is broken.

**Sanity check before steps 1–8:** open a Python REPL with the dev env, run `from composio import Composio; print([t.name for t in Composio().create(user_id='probe').tools()])`. Output should be the 4–6 meta-tool slugs. If you see hundreds of toolkit actions, Task 3a's assertion will trip on the first chat turn — fix the config (likely a stray `preload=` or `session_preset=`) before proceeding.

---

## Test coverage targets

- `tests/cloud/composio/test_config.py` — env parsing + invariants (Task 1).
- `tests/cloud/composio/test_service.py` — user_id namespacing, client caching, is_enabled gate (Task 2).
- `tests/cloud/composio/test_providers.py` — `build_tools_for_backend` direct-tools fetch per toolkit, search-fallback meta-tools appended (Task 3a), graceful degrade when meta-tools fetch fails, fail-closed on empty `composio_toolkits`.
- `tests/cloud/composio/test_connect_link.py` — inline Ripple shape, auth-error detection (Task 4).
- `tests/cloud/composio/test_identity.py` — per-toolkit probe registry, tripwire on identity mismatch, missing-toolkit graceful degrade (Task 4a).

No live Composio calls in CI. Mock the `composio` SDK at the `Composio.create()` boundary. The hands-on "print tools after create()" verification step is one-time, manual, and documented in Task 3a — not part of CI.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Composio cloud outage takes down all tool-calling | MCP server build wraps `session.tools()` in try/except — on failure, return empty MCP server (agent falls back to its built-in tools and custom connectors) and log loudly. Never let Composio failure 500 the chat. |
| Composio bills per tool call | `POCKETPAW_COMPOSIO_TOOLKITS` allow-list is the cost lever. Default empty = nothing enabled. |
| Tool I/O leaks PII to Composio cloud | Documented in customer-facing docs as a v1 limitation. Enterprise customers requiring isolation must wait for self-hosted runtime support. |
| Composio SDK breaking changes | Pin `composio` to a single minor version in `pyproject.toml`. Bump deliberately. |
| `user_id` collision across enterprise deployments | Namespacing (Task 2) prevents this; enforced by the `composio_enterprise_id` required-when-enabled invariant (Task 1). |
| Agent can't find a specific Composio action it needs | Task 3a's hybrid: direct tools as the primary surface + `COMPOSIO_SEARCH_TOOLS` / `GET_TOOL_SCHEMAS` / `MULTI_EXECUTE_TOOL` as a discovery fallback. System prompt instructs the agent to use search when direct tools miss. Caveat: if Composio's SDK changes `tools=[name,...]` arg name, the meta-tools fetch silently returns 0 — test covers the assertion that they're present. |
| User authorizes Composio toolkit as wrong identity | Task 4a's probe + tripwire. Initial connection always renders an explicit "Connected as X — continue?" confirmation; re-authorizing as a different identity surfaces the mismatch instead of silently overwriting. |
| Composio ships `--user-id` on CLI later, tempting us to rip out SDK | Not a risk today; flagged so future-us doesn't re-litigate. Re-evaluate only when Composio docs explicitly add `--user-id` to `composio execute` (not `composio dev`). The SDK path is the durable choice. |

---

## Follow-ups (NOT in this plan, but should land before any customer GA)

1. **v2: `ComposioConnector` per-toolkit adapter** implementing `ConnectorProtocol.execute` — so Ripple `$source` and non-agent code paths can call Composio toolkits with the same interface as custom connectors.
2. **Per-workspace toolkit allow-list** stored on the workspace document, with an admin UI in paw-enterprise's Settings → Integrations panel. Env var becomes the cluster-wide default; workspace can narrow it.
3. **Self-hosted Composio validation** — confirm parity for at least Gmail/Slack/GitHub against a self-hosted Composio instance before promising it to a customer.
4. **Audit logging** — every Composio tool execution emits an audit event (`composio.tool.executed`) with toolkit, action, user_id, latency. Hook into existing `_core/audit` (if it exists) or add it.
5. **Composio Triggers / Webhooks** — receive Composio-side events (new email, new Slack message) and route them through the existing in-process bus. Big enough to be its own plan.
6. **OSS-side integration** — once cloud path is stable, consider exposing Composio to `src/pocketpaw/` users via the same MCP injection pattern on the `claude_agent_sdk` OSS backend.
