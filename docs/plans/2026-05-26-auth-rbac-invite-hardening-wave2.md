# Auth / RBAC / Invitation Hardening — Wave 2

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the members-management surface (UI + backend gaps) and close the remaining medium-severity holes from the audit so a workspace owner can run their org without filing tickets — invite, see who's in, change roles, kick people, see who did what, and not get blown up by abuse vectors.

**Architecture:**
- **Backend:** add the missing endpoints (decline invite, bulk invite, resend cooldown, audit query, last-owner guard, stale-invite GC), wire rate limits via a Redis bucket, persist structured audit rows on every workspace mutation. Plug realtime-channel auth gaps. Tighten plan-feature dep + group-agent IDOR.
- **Frontend:** build `/settings/workspace/members` (the page that doesn't exist). Add role-explainer popovers, last-owner warnings, delete blast-radius, bulk-invite paste-list, resend cooldown chips, copy-link affordance, owner hard-confirm. Surface the audit log at `/audit` (route exists — wire it).

**Tech Stack:** FastAPI + Beanie (backend), slowapi for rate limits, structured audit collection. SvelteKit 2 + Svelte 5 + bits-ui Table / Popover / AlertDialog for the members UI.

**Depends on:** Wave 1 landed (`feat/auth-invite-hardening-wave1`). Specifically: the typed preview endpoint, hashed tokens, and the `Forbidden` error subcodes pattern.

**Branches:** `feat/auth-invite-hardening-wave2-backend` and `feat/auth-invite-hardening-wave2-ui`. Backend lands first; UI rebases on top.

---

## Pre-flight

### Task 0: Confirm Wave 1 is in main

```bash
cd D:/paw/backend
git log --oneline origin/main -20 | grep -i "invite\|auth_secret\|active_workspace"
```

You should see Wave 1's commits. If not, stop — Wave 2 depends on the preview endpoint and hashed tokens.

```bash
uv run pytest tests/cloud/workspace tests/cloud/auth -v
```

Expected: green.

---

## Backend — Part A: rate limiting (Finding #5)

### Task 1: Tighten rate limits on login / register / invite

**Audit findings before starting (2026-05-26 verify pass):**

- A global token-bucket limiter already exists at `src/pocketpaw/security/rate_limiter.py` (in-memory, per-IP, no deps). Tiers: `api_limiter` (10/s, burst 30), `auth_limiter` (1/s, burst 5).
- It's wired as middleware in `src/pocketpaw/dashboard_auth.py:384-396` and runs for every request that isn't on the exempt list.
- **The exempt list explicitly skips `/api/v1/auth/login`, `/api/v1/auth/register`, `/api/v1/auth/bearer/login`** (`dashboard_auth.py:357-367`) — these get NO rate limiting today. That's the audit finding's actual exposure.
- General API endpoints (workspaces, invites, etc.) get the loose `api_limiter` tier (30 burst per IP) — fine for normal use, but not tight enough for invite-spam protection.
- The existing limiter is per-IP only. Brute-force protection wants per-(IP, email) so attackers can't hop emails behind one IP.

**Scope shift:** don't add slowapi / redis from scratch. Layer on top of the existing limiter. The work is:

1. **Remove the exemptions for `/auth/login`, `/auth/register`, `/auth/bearer/login`** and route them through a NEW, stricter limiter than the default `auth_limiter`. The new tier: per-(IP, email) keyed, 5 attempts per 15 min. Add it as `login_limiter` in `security/rate_limiter.py` alongside the existing tiers.
2. **For `POST /workspaces/{id}/invites`** (cloud, hits the general middleware): add a per-(actor_user_id, workspace_id) bucket at 50/day on top of the IP limit. Implement as a `Depends()` on the route (or in the workspace service entry) since the user/workspace IDs aren't extractable from the path alone in the middleware.

The Redis-backed implementation can wait for Wave 3 (when sessions and API keys also need Redis state). In-memory is fine for now — single-instance backend.

**Files:**
- Modify: `src/pocketpaw/security/rate_limiter.py` (add `login_limiter` and a generic factory for the invite path).
- Modify: `src/pocketpaw/dashboard_auth.py` (remove login/register from exemption, apply `login_limiter` keyed on `(ip, email_from_form_body)`).
- Modify: `ee/pocketpaw_ee/cloud/workspace/router.py` (Depends-style rate-limit guard on the invite-create route).
- Test: `tests/test_rate_limiter.py` (existing? extend) or `tests/cloud/workspace/test_invite_rate_limit.py` (new).

**Step 1: Failing tests.** Three:
- 6 login attempts to `/api/v1/auth/login` from same IP+email → 6th returns 429 with `Retry-After` header.
- 4 registers from same IP → 4th returns 429.
- 51 invites within 24h from one actor to one workspace → 51st returns 429.

**Step 2: Implement.**
- New `login_limiter = RateLimiter(rate=5/900, capacity=5)` (5 in 15 min).
- In `dashboard_auth.py`: drop the auth-path exemptions; add a branch that parses the form body for the username (email) on `/auth/login` POSTs and keys on `f"login:{ip}:{email}"`.
- Cloud invite route: `Depends(rate_limit_invite_create)` that hits a `(user_id, workspace_id)` keyed bucket.

**Step 3: Tests + commit.** Message: `fix(security): rate-limit login, register, and invite-create endpoints`. Note in the body: closes the exemption gap in dashboard_auth that left brute-force fully open.

---

## Backend — Part B: invite flow polish (#10, #12, #15, #16, #18)

### Task 2: Stale-invite GC on create (#12)

**Files:**
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/service.py` (`create_invite` and indexing setup)
- Test: extend `tests/cloud/workspace/test_service_v2.py`

**Step 1: Failing test.** Create an invite, manually set `expires_at` to the past, create a new invite to the same email + workspace → existing pre-existing-invite check must not collide.

**Step 2: Fix.** Before the existing-invite collision check, hard-delete any rows where `expired or revoked` for the same `(workspace, email, group)`. Or add a TTL index — Mongo's `expireAfterSeconds` on `expires_at` would auto-clean. Pick TTL since it's free and works for revoked too if we set a `revoked_at`. Add the index in the `Invite` model's `Settings`:

```python
class Settings:
    name = "invites"
    indexes = [
        IndexModel("expires_at", expireAfterSeconds=86400 * 14),  # 7-day grace past expiry
    ]
```

**Step 3: Tests + commit.** `chore(workspace): GC stale invites via TTL index + pre-create cleanup`.

### Task 3: Invitation decline endpoint (closes UX U7)

**Files:**
- Modify: `workspace/router.py`, `workspace/service.py`, `workspace/dto.py`
- Test: append to `test_service_v2.py`

**Step 1: Endpoint.** `POST /workspaces/invites/{token}/decline` (public, like accept). Marks the invite `revoked=True, revoked_reason="declined"`. Emits `WorkspaceInviteRevoked` with `{reason: "declined"}`.

**Step 2: Tests.** Decline + verify state transitions; decline on already-accepted returns 409.

**Step 3: Commit.** `feat(workspace): POST /invites/{token}/decline`.

### Task 4: Last-owner guard on demote and remove (#10)

**Files:**
- Modify: `workspace/service.py` (`update_member_role`, `remove_member`)
- Test: `test_service_v2.py`

**Step 1: Failing test.** Single-owner workspace; call `update_member_role(owner_id, "admin")` → expects `Forbidden("workspace.last_owner")`. Same for `remove_member`.

**Step 2: Implement.** Before mutating, count current owners via `_UserDoc.find({"workspaces": {"$elemMatch": {"workspace": wid, "role": "owner"}}}).count()`. If the target is an owner being demoted/removed and count == 1, raise.

**Step 3: Commit.** `fix(workspace): block demoting or removing the last owner`.

### Task 5: Soft-deleted workspace re-check at accept (#16)

**Files:**
- Modify: `workspace/service.py` `accept_invite` — the `_fetch_workspace` already returns None on tombstone, but `_add_member` runs before that path was reached on Wave 1. Audit and confirm; if a race window exists, fetch + check inside the atomic block.

**Step 1:** Add a regression test: tombstone the workspace mid-flight (monkeypatch to set deleted_at between preview and add_member) → expects NotFound, no membership row created, invite NOT consumed.

**Step 2:** If test fails, move the `_fetch_workspace` check to run after the atomic claim but before `_add_member`; rollback on miss.

**Step 3: Commit.** `fix(workspace): rollback invite claim if workspace tombstoned mid-accept`.

### Task 6: Re-check inviter privilege at every invite (#15)

**Files:**
- Modify: `workspace/router.py` (the `create_invite` route's Depends)
- Modify: `workspace/service.py` if the guard isn't at route level

**Step 1: Audit.** Confirm `create_invite` route has `Depends(require_action("workspace.invite"))` or equivalent. If it's a manual `if role not in (...)` in the service, lift to a route guard so the check runs on every call, not just on first hit.

**Step 2: Tests.** Demote actor to viewer mid-session (in test, manually edit user.workspaces) → next invite call returns 403.

**Step 3: Commit.** `fix(workspace): re-check inviter privilege at route layer`.

### Task 7: Bulk invite endpoint (UX U16)

**Files:**
- Modify: `workspace/router.py`, `workspace/dto.py`, `workspace/service.py`
- Test: `test_service_v2.py`

**Step 1: DTO.**

```python
class BulkInviteRequest(BaseModel):
    emails: list[EmailStr] = Field(min_length=1, max_length=100)
    role: str = "member"
    group_id: str | None = None

class BulkInviteResponse(BaseModel):
    created: list[InviteResponse]
    skipped: list[dict]  # [{email, reason}]
```

**Step 2: Service.**

```python
async def bulk_create_invites(ctx, workspace_id, body) -> dict:
    # Iterate; per-email skip on already-member / pending-invite / seat-limit / invalid.
    # Single seat-limit check up front against the total batch size to fail fast.
```

**Step 3: Route.** `POST /workspaces/{id}/invites/bulk` — same rate-limit bucket as single invite, but count each email in the batch.

**Step 4: Tests.** 100-email batch with 3 dupes → 97 created, 3 skipped with reason; over-seat batch → SeatLimitError before any inserts.

**Step 5: Commit.** `feat(workspace): POST /invites/bulk for paste-a-list invites`.

### Task 8: get_workspace_plan fails closed on transient errors (#9)

**Files:**
- Modify: `workspace/service.py` `get_workspace_plan` + `_core/deps.py` `require_plan_feature`
- Test: `test_service_v2.py`

**Step 1: Failing test.** Monkeypatch `_fetch_workspace` to raise `OperationFailure` → current code swallows and returns `"team"`. New behavior: raise `ServiceUnavailable` (or whatever CloudError maps to 503).

**Step 2: Implement.** Differentiate "workspace genuinely missing" (return None / 404 at the dep) from "DB hiccup" (re-raise as 503). The dep `require_plan_feature` catches None as 404; uncaught raises become 503.

**Step 3: Commit.** `fix(workspace): fail-closed on transient plan lookup errors`.

### Task 9: Group-agent workspace check (#8)

**Files:**
- Modify: `chat/router.py` group-agent add/update/remove routes (`:196-234` per audit)
- Test: existing chat router tests

**Step 1:** Add a route-level dep that loads the agent by id and verifies `agent.workspace == ctx.workspace_id`. If mismatch, 404 (don't leak existence).

**Step 2:** Cross-workspace test — try to add agent from workspace B to a group in workspace A → 404.

**Step 3: Commit.** `fix(chat): verify agent belongs to workspace on group-agent ops`.

---

## Backend — Part C: audit log persistence (#13, #23 partial)

### Task 10: Add an audit_events collection + write path

**Files:**
- Create: `D:/paw/backend/ee/pocketpaw_ee/cloud/audit/models.py` (or use `models/audit_event.py` per cloud rules)
- Create: `D:/paw/backend/ee/pocketpaw_ee/cloud/audit/{domain,dto,router,service}.py` (the 4-file shape)
- Modify: `workspace/service.py` to call `audit_service.record(...)` on each mutating op
- Test: `tests/cloud/audit/test_service_v2.py`

**Why:** Today, mutations emit events on the bus but nothing persists "actor X did Y at time Z." Compliance / forensics needs a queryable history.

**Step 1: Doc.**

```python
class AuditEvent(Document):
    workspace: Indexed(str)
    actor_id: Indexed(str)
    action: Indexed(str)   # "workspace.member_added", "workspace.invite_revoked", etc.
    target_type: str        # "user", "invite", "workspace", "group"
    target_id: str | None
    metadata: dict          # role transitions, old/new values, etc.
    ip: str | None
    user_agent: str | None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

TTL index on `at` (e.g. 365 days) so the collection stays bounded.

**Step 2: Service.**

```python
async def record(workspace_id, actor_id, action, *, target_type, target_id=None, metadata=None, ctx=None):
    ...
```

`ctx` carries IP / UA from request middleware (add a tiny `RequestContext.ip` if it doesn't already).

**Step 3: Wire writes.** In `workspace/service.py`, call `audit_service.record(...)` from:

- `create`, `update`, `delete`
- `update_member_role`, `remove_member`
- `create_invite`, `accept_invite`, `revoke_invite`, `decline` (Task 3)
- `bulk_create_invites`

**Step 4: Query API.** `GET /workspaces/{id}/audit?cursor=&action=&actor=&since=&until=` with cursor pagination. Owner/admin only. Returns paginated `AuditEventResponse`.

**Step 5: Tests.** Each mutating op produces exactly one row; query filters work; non-admin gets 403.

**Step 6: Commit.** `feat(audit): persist workspace audit events with query API`.

---

## Backend — Part D: realtime channel auth (#11)

### Task 11: Re-enforce membership on socket subscribe

**Files:**
- Modify: realtime channel subscribe handlers (find them — likely in `_core/realtime/`)
- Test: realtime test scope

**Step 1: Audit.**

```bash
grep -rn "subscribe\|on_connect\|on_subscribe" ee/pocketpaw_ee/cloud/_core/realtime/ ee/pocketpaw_ee/cloud/realtime/
```

Walk every subscribe-handling function. For each: does it verify the requesting user is in the audience? If using the audience resolver (`list_member_ids` etc), confirm subscribe-time path also funnels through it.

**Step 2: Failing test.** Have user A subscribe to workspace B's channel directly → should be denied.

**Step 3: Fix at the subscribe layer.** Don't sprinkle checks per channel; centralize: a single audience-check at subscribe should map `(user_id, channel) → bool`.

**Step 4: Commit.** `fix(realtime): enforce audience check at channel subscribe`.

---

## Backend — Part E: open PR

### Task 12: Backend PR

```bash
git push -u origin feat/auth-invite-hardening-wave2-backend
```

PR body lists the 11 changes above with a checklist. Test plan covers the manual paths (manual invite over rate-limit, bulk invite of 100, last-owner demote attempt, cross-workspace realtime subscribe).

---

## Frontend — Part F: members management page

### Task 13: `/settings/workspace/members` route scaffolding

**Files:**
- Create: `D:/paw/paw-enterprise/src/routes/settings/workspace/members/+page.svelte`
- Create: `D:/paw/paw-enterprise/src/lib/core/workspaces/members-api.ts` (API client)
- Modify: existing settings nav to add a "Members" tab

**Step 1: API client.** Wrap the workspace-members + invites endpoints:

```ts
export async function listMembers(workspaceId: string): Promise<Member[]>
export async function updateMemberRole(workspaceId: string, userId: string, role: Role): Promise<void>
export async function removeMember(workspaceId: string, userId: string): Promise<void>
export async function listInvites(workspaceId: string): Promise<Invite[]>
export async function createInvite(workspaceId: string, body: CreateInviteBody): Promise<Invite>
export async function createInvitesBulk(workspaceId: string, body: BulkInviteBody): Promise<BulkInviteResult>
export async function revokeInvite(workspaceId: string, inviteId: string): Promise<void>
export async function resendInvite(workspaceId: string, inviteId: string): Promise<void>
```

**Step 2: Page shell.** Two tabs ("Members" / "Pending invites"), each a Table. Use bits-ui Table primitives (already in the project per CLAUDE.md).

**Step 3: Empty / loading / error states** all spelled out — no silent skeletons that never resolve.

**Step 4: Commit.** `feat(settings): scaffold workspace members page`.

### Task 14: Members table — list, role change, remove (UX U8, U9, U11)

**Step 1: Table columns.** Avatar, name + email, role chip with explainer Popover, joined-at, actions menu (Change role, Remove). Role chip's popover surfaces "Owner / Admin / Member can do X" — text from a single source-of-truth `ROLE_DESCRIPTIONS` const so backend and frontend agree.

**Step 2: Change role.** Click → `Select` with confirm. Last-owner case: backend now returns 403; surface it as a `toast.error` with the exact reason string ("Cannot demote the last owner — promote someone else first").

**Step 3: Remove.** AlertDialog with name typed-to-confirm pattern (mirror the workspace-delete UX). On backend 403 last-owner, same flow as above.

**Step 4: Commit.** `feat(members): list + change role + remove with last-owner UX`.

### Task 15: Pending invites tab — revoke, resend (cooldown), copy link (UX U17, U19)

**Step 1: Table.** Email, role chip, invited-by, expires-at relative time, status (Pending / Expired), actions: Resend (with cooldown), Copy link, Revoke.

**Step 2: Resend cooldown.** After a successful resend, store `lastResentAt` in a `$state` Map keyed by invite id. Disable the button + show "Resent · 4:32 left" for 5 minutes. Cooldown lives client-side AND backend rate-limits — defense-in-depth.

**Step 3: Copy link.** The plaintext token returned at invite-create time is gone after page navigation (server only hashes it). So Copy Link only works for invites created in this session — display "Resend to copy a new link" on older invites. (Alternative: have backend's `resend_invite` return a fresh plaintext token, which is the simpler model — Task 16.)

**Step 4: Commit.** `feat(members): pending invites tab with resend cooldown + copy link`.

### Task 16: Backend resend mints a fresh token (server side)

**Files:**
- Modify: `backend/ee/pocketpaw_ee/cloud/workspace/service.py` — add `resend_invite`
- Modify: `workspace/router.py` — `POST /workspaces/{id}/invites/{invite_id}/resend`

**Step 1:** `resend_invite` revokes the old row's `token_hash` (delete? rotate?), creates a fresh `token_urlsafe(32)`, updates `token_hash`, resets `expires_at`, increments a `resend_count`. Returns the new plaintext token in the response so the UI can put it on the clipboard.

**Step 2:** Rate-limit at 5 resends per 30 min per invite.

**Step 3: Commit.** `feat(workspace): POST /invites/{id}/resend rotates token`.

### Task 17: Owner role hard-confirm (UX U18)

**Step 1:** In the invite form, when role = `owner` or `admin`, render an inline warning box: "Owners can delete the workspace and remove other owners. Type the role name to confirm." Input must match before Send is enabled.

**Step 2: Commit.** `ui(members): hard-confirm owner/admin invites`.

### Task 18: Bulk invite paste-list (UX U16)

**Step 1:** Toggle "Invite many" → textarea that splits on newline / comma / semicolon. Validate emails inline; show count + invalid-row count. Submit → `createInvitesBulk` → result modal: "Sent 47, skipped 3" with a per-skip reason table.

**Step 2: Commit.** `ui(members): bulk invite paste-a-list`.

### Task 19: Delete workspace blast-radius (UX U10)

**Files:**
- Modify: `paw-enterprise/src/routes/settings/workspace/+page.svelte`
- Modify: backend `workspace/service.py` — add `delete_preview(workspace_id)` returning `{member_count, room_count, file_count, total_bytes}`

**Step 1: Backend.** `GET /workspaces/{id}/delete-preview`. Cheap aggregation across users, chat groups, files collections.

**Step 2: UI.** Before the existing type-name-to-confirm step, show the preview: "Deleting will remove 12 rooms, 47 members, 3.2 GB of files. This cannot be undone."

**Step 3: Commit.** `ui(settings): show blast-radius before workspace delete`.

### Task 20: Wire `/audit` page (UX U20)

**Files:**
- Modify: `paw-enterprise/src/routes/audit/+page.svelte`
- Create: `paw-enterprise/src/lib/core/audit/api.ts`

**Step 1: API.** `listAuditEvents(workspaceId, {cursor, action, actor, since, until})`.

**Step 2: UI.** Virtual-scrolled list. Filter bar: action multiselect (populated from the catalog of `workspace.*`, `chat.*` actions), actor selector, date range. Row format: "Sarah Chen invited bob@x.c · 2h ago · 192.168.x.x" with expandable metadata.

**Step 3: Commit.** `feat(audit): wire workspace audit history at /audit`.

---

## Frontend — Part G: open PR

### Task 21: Frontend PR

PR body summarizes the 8 UI changes. Test plan: manual walk through inviting, role change with last-owner block, bulk invite of 50, resend with cooldown chip, audit page filters, delete preview.

---

## Bookkeeping

### Task 22: Memory update

Update `project_auth_invite_hardening.md` to flip Wave 1 → done, Wave 2 → in progress. Add a reference memory if any audit catalog turned out to be the source-of-truth file (e.g., `reference_audit_action_catalog.md` pointing at where action strings are enumerated).

---

## Roll-up

- [ ] Task 0  baseline
- [ ] Task 1  rate limits
- [ ] Task 2  stale-invite GC
- [ ] Task 3  invite decline endpoint
- [ ] Task 4  last-owner guard backend
- [ ] Task 5  soft-deleted workspace race
- [ ] Task 6  inviter privilege re-check
- [ ] Task 7  bulk invite endpoint
- [ ] Task 8  plan lookup fail-closed
- [ ] Task 9  group-agent workspace check
- [ ] Task 10 audit events collection + API
- [ ] Task 11 realtime channel auth
- [ ] Task 12 backend PR
- [ ] Task 13 members page scaffold
- [ ] Task 14 members table
- [ ] Task 15 pending invites tab
- [ ] Task 16 resend rotates token
- [ ] Task 17 owner hard-confirm
- [ ] Task 18 bulk paste invite UI
- [ ] Task 19 delete blast-radius
- [ ] Task 20 audit page wiring
- [ ] Task 21 frontend PR
- [ ] Task 22 memory + bookkeeping
