# Spec: Workspace RBAC Consolidation

## Objective

Consolidate the two parallel RBAC implementations in the backend into a single, route-level enforced authorization system covering every resource in the cloud module: workspaces, chat groups, messages, pockets, agents, sessions, KB, invites, and billing.

Today the cloud module has two RBAC frameworks (`src/pocketpaw/ee/guards/` newer, `ee/cloud/shared/` legacy) with duplicated enums, and permission checks are scattered inside service methods rather than declared on routes. Group roles are effectively binary (owner vs. member) despite a `member_roles` dict already being present. The goal is one framework, one enforcement pattern, and a documented action matrix — so that every protected operation has exactly one place where its access rule is defined.

**Target users:** Backend engineers working in `ee/cloud/`; frontend engineers who need stable `Forbidden.code` values to key UI state off of.

**Success looks like:**
- All cloud routes enforce authorization via FastAPI dependencies, not via ad-hoc checks inside services.
- `ee/cloud/shared/permissions.py` and `ee/cloud/shared/deps.py:require_role` are deleted; all callers migrated to `src/pocketpaw/ee/guards/`.
- Groups support a 3-tier role model (OWNER > ADMIN > MEMBER) with per-member override via `Group.member_roles`.
- Every `Forbidden` raised has a stable machine-readable `code` listed in the action matrix.
- A role × action matrix test exists and passes, covering every row in the matrix below.

## Tech Stack

- Python 3.11+, FastAPI, Beanie (MongoDB ODM), fastapi-users
- Pytest with `asyncio_mode = "auto"`
- Ruff (line-length 100, E/F/I/UP), mypy
- Existing deps — no new dependencies

## Commands

```bash
# From D:\paw\backend
uv sync --dev

# Run cloud RBAC tests only
uv run pytest tests/cloud/test_ee_guards.py tests/cloud/test_permissions.py -v

# Run full cloud suite
uv run pytest tests/cloud -v

# Run full test suite (excluding e2e)
uv run pytest --ignore=tests/e2e

# Lint + format
uv run ruff check ee/cloud src/pocketpaw/ee/guards tests/cloud
uv run ruff format ee/cloud src/pocketpaw/ee/guards tests/cloud

# Type check
uv run mypy src/pocketpaw/ee/guards ee/cloud

# Rebuild KB wiki after changes
kb build ./ee/cloud --scope paw-cloud --output docs/wiki/
```

## Project Structure

```
src/pocketpaw/ee/guards/           # Canonical RBAC framework (keep)
  rbac.py                          # WorkspaceRole, PocketAccess enums + check_* functions
  policy.py                        # PolicyContext, PolicyResult, Forbidden
  abac.py                          # Plan features, ACTION_ROLES, tool limits, agent ceiling
  deps.py                          # require_role, require_pocket_access, require_policy,
                                   #   require_plan_feature, require_group_role (NEW)
  actions.py                       # NEW — single source of truth for action → (role, code) mapping
  audit.py                         # NEW — helper: log_denial(), log_privileged_action()

ee/cloud/
  shared/
    errors.py                      # CloudError hierarchy (keep)
    permissions.py                 # DELETE after migration
    deps.py                        # Keep current_user; DELETE local require_role
  workspace/router.py              # Add Depends(require_role(ADMIN)) etc. to routes
  workspace/service.py             # Remove inline role checks (move to router deps)
  chat/router.py                   # Add Depends(require_group_role(...)) to routes
  chat/group_service.py            # Remove inline _require_group_admin etc.
  pockets/router.py                # Add Depends(require_pocket_access(...)) to routes
  agents/router.py                 # Add Depends + plan/ceiling checks
  sessions/router.py               # Add deps
  kb/router.py                     # Add deps
  models/group.py                  # Extend MemberRole to include "admin" tier

tests/cloud/
  test_ee_guards.py                # Unit tests for enums + check functions (extend)
  test_permissions.py              # DELETE (duplicates test_ee_guards)
  test_rbac_matrix.py              # NEW — parametrized (role, action, resource) → allow/deny
  test_rbac_routes.py              # NEW — integration tests hitting real routers

docs/wiki/                         # Auto-regenerated via kb build
SPEC-rbac.md                       # This file (ee/cloud/SPEC-rbac.md)
```

## Code Style

**Route-level enforcement — the target pattern:**

```python
# ee/cloud/workspace/router.py
from pocketpaw.ee.guards.deps import require_role
from pocketpaw.ee.guards.rbac import WorkspaceRole

@router.patch("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    _: User = Depends(require_role(WorkspaceRole.ADMIN)),
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceRead:
    return await service.update(workspace_id, payload)


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    _: User = Depends(require_role(WorkspaceRole.OWNER)),
    service: WorkspaceService = Depends(get_workspace_service),
) -> None:
    await service.delete(workspace_id)
```

**Service stays auth-agnostic:**

```python
# ee/cloud/workspace/service.py — AFTER
async def update(self, workspace_id: str, payload: WorkspaceUpdate) -> Workspace:
    ws = await self._get(workspace_id)
    ws.apply(payload)
    await ws.save()
    return ws
# No role checks here — the route dependency already enforced it.
```

**Denials use stable codes:**

```python
# pocketpaw/ee/guards/actions.py
ACTIONS: dict[str, ActionRule] = {
    "workspace.update":          ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "workspace.delete":          ActionRule(WorkspaceRole.OWNER, "workspace.insufficient_role"),
    "workspace.member.remove":   ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "group.post":                ActionRule(GroupRole.MEMBER,    "group.view_only"),
    "group.admin":               ActionRule(GroupRole.ADMIN,     "group.not_admin"),
    "group.delete":              ActionRule(GroupRole.OWNER,     "group.not_owner"),
    "pocket.read":               ActionRule(PocketAccess.VIEW,   "pocket.access_denied"),
    "pocket.edit":               ActionRule(PocketAccess.EDIT,   "pocket.access_denied"),
    "pocket.share":              ActionRule(PocketAccess.OWNER,  "pocket.not_owner"),
    "agent.create":              ActionRule(WorkspaceRole.MEMBER,"agent.ceiling_exceeded"),
    "billing.manage":            ActionRule(WorkspaceRole.OWNER, "billing.owner_only"),
    # ... full matrix below
}
```

**Naming:** snake_case for functions, PascalCase for classes, `SCREAMING_SNAKE` for action codes in dotted form (`resource.reason`). Dependency factories return callables, named `require_*`.

## Action Matrix

Authoritative source: `src/pocketpaw/ee/guards/actions.py`. Tests iterate this dict.

| Resource | Action | Min Role | Deny Code |
|---|---|---|---|
| Workspace | view | MEMBER | `workspace.not_member` |
| Workspace | update settings | ADMIN | `workspace.insufficient_role` |
| Workspace | delete | OWNER | `workspace.insufficient_role` |
| Workspace | transfer ownership | OWNER | `workspace.insufficient_role` |
| Workspace | invite member | ADMIN | `workspace.insufficient_role` |
| Workspace | remove member | ADMIN | `workspace.insufficient_role` |
| Workspace | change member role | ADMIN (cannot promote ≥ self) | `workspace.insufficient_role` / `workspace.cannot_promote_above_self` |
| Workspace | demote owner | — | `workspace.cannot_demote_owner` |
| Group | view | MEMBER (ws) + group member | `group.not_member` |
| Group | create | MEMBER (ws) | `workspace.insufficient_role` |
| Group | post message | GROUP_MEMBER (not view-only) | `group.view_only` |
| Group | edit group / add admin | GROUP_ADMIN | `group.not_admin` |
| Group | delete | GROUP_OWNER | `group.not_owner` |
| Group | transfer ownership | GROUP_OWNER | `group.not_owner` |
| Message | edit (own) | author | `message.not_author` |
| Message | delete (any) | GROUP_ADMIN | `group.not_admin` |
| Pocket | read | VIEW+ | `pocket.access_denied` |
| Pocket | comment | COMMENT+ | `pocket.access_denied` |
| Pocket | edit | EDIT+ | `pocket.access_denied` |
| Pocket | share / delete | OWNER | `pocket.not_owner` |
| Agent | run | MEMBER | `workspace.insufficient_role` |
| Agent | create | MEMBER + plan + ceiling | `agent.ceiling_exceeded` / `plan.feature_denied` |
| Agent | edit / delete | ADMIN or agent.owner | `agent.not_owner` |
| Session | read (own) | author | `session.not_owner` |
| Session | read (any in ws) | ADMIN | `workspace.insufficient_role` |
| KB | read | MEMBER | `workspace.insufficient_role` |
| KB | write | MEMBER | `workspace.insufficient_role` |
| Invite | create | ADMIN | `workspace.insufficient_role` |
| Invite | revoke | ADMIN | `workspace.insufficient_role` |
| Billing | view | ADMIN | `billing.admin_only` |
| Billing | manage | OWNER | `billing.owner_only` |

## Testing Strategy

- **Framework:** pytest + pytest-asyncio (auto mode), existing fixtures in `tests/cloud/conftest.py`.
- **Unit tests** (`test_ee_guards.py`): role comparisons, `check_workspace_role`, `check_pocket_access`, agent-ceiling, plan-feature gates.
- **Matrix test** (`test_rbac_matrix.py` — NEW): `@pytest.mark.parametrize` over every entry in `ACTIONS`; for each row iterate all roles and assert allow/deny and exact `code`.
- **Route integration tests** (`test_rbac_routes.py` — NEW): spin up the FastAPI app with test DB; for each protected route, call it as OWNER/ADMIN/MEMBER/non-member and assert 200 / 403 + `code`. Fixture factory: `authed_client(role)`.
- **Coverage target:** every `ACTIONS` row exercised at least once in positive (allow) and negative (deny) direction. Enforced by a meta-test that diffs `ACTIONS.keys()` against covered actions.
- **No mocks for DB** — use an in-memory/real Mongo test instance per existing `tests/cloud` convention.

## Boundaries

**Always:**
- Declare permission rules at the route layer via `Depends(require_*)`.
- Register every new guarded action in `ACTIONS` with a stable `code`; add a matrix test row.
- Raise `Forbidden(code=...)` — never a bare `HTTPException(403)`.
- Log privileged actions (role change, invite accept, billing change, workspace delete) and every denial to the audit log.
- Run `ruff check`, `ruff format`, `mypy`, and the cloud test suite before considering a change done.
- Update `docs/wiki/` by rebuilding KB when cloud modules change.

**Ask first:**
- Adding any new dependency.
- Schema changes to `User`, `Workspace`, `Group`, `Pocket`, `Agent` (beyond extending `MemberRole` enum values).
- Introducing custom roles or org-level hierarchies.
- Changing any existing `Forbidden.code` string (frontend consumes these).
- Touching billing / plan-gate logic.
- Deleting `ee/cloud/shared/permissions.py` (confirm all callers migrated first).

**Never:**
- Put auth logic inside service methods. Services are auth-agnostic.
- Bypass `require_*` with inline `if user.role != "admin"` checks.
- Return 404 to mask a 403 (leaks no less info than 403 does in this system — consistency matters).
- Silently widen a role's permissions without adding a matrix test row.
- Remove or disable audit logging for denials.
- Commit secrets; modify `vendor/` or fastapi-users internals.

## Success Criteria

1. `rg "ee.cloud.shared.permissions|ee.cloud.shared.deps.require_role" src ee` returns no hits.
2. `rg "if .*\.role" ee/cloud --type py` returns only model/schema field access, no authorization branching.
3. Every route in `ee/cloud/*/router.py` that mutates or reads non-public data has a `Depends(require_*)` in its signature.
4. `uv run pytest tests/cloud` passes with the new matrix + route tests.
5. Meta-test confirms every key in `ACTIONS` has at least one allow and one deny test.
6. `uv run mypy src/pocketpaw/ee/guards ee/cloud` passes.
7. Docs wiki regenerated; a new `docs/wiki/rbac-overview.md` describes the model and action matrix.

## Open Questions

1. **Group admin tier storage:** extend `MemberRole` literal to `"owner" | "admin" | "edit" | "view"`, or split into two fields (`role` + `access_level`)? Leaning toward single field with 4 values — simpler.
2. **Workspace owner uniqueness:** should `WorkspaceRole.OWNER` be limited to exactly one user (transfer-only) or allow multiple? Today it's singular via `Workspace.owner: str`; keeping that assumption.
3. **Self-service role change:** can a user leave a workspace (remove themselves)? Proposed yes, except the sole OWNER must transfer first.
4. **Agent-as-actor:** when an agent performs an action on behalf of a user, does the ceiling check happen at invocation time, creation time, or both? `abac.py` currently implies creation-time; confirm that's sufficient.

## Phased Rollout

1. **Phase 1 — Foundation:** Add `actions.py`, `audit.py`, `require_group_role` dep. Write matrix test that initially xfails for unmigrated routes.
2. **Phase 2 — Migrate workspace routes:** Move all `check_workspace_role` calls out of `workspace/service.py` into `workspace/router.py` deps. Green the matrix for workspace rows.
3. **Phase 3 — Migrate chat/group routes:** Extend `MemberRole`, introduce `GroupRole` enum, migrate `group_service.py` checks out to router. Green group rows.
4. **Phase 4 — Migrate pockets, agents, sessions, KB, invites, billing.**
5. **Phase 5 — Delete legacy:** Remove `ee/cloud/shared/permissions.py` + legacy `require_role`. Remove `tests/cloud/test_permissions.py`. Final grep verification per Success Criteria.
6. **Phase 6 — Docs:** KB rebuild, add `docs/wiki/rbac-overview.md`.

Each phase ends with: matrix test green for its scope, `ruff` + `mypy` clean, PR reviewed.
