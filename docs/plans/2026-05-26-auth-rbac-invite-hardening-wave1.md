# Auth / RBAC / Invitation Hardening — Wave 1

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the five highest-severity security holes in the auth + invitation flow and ship the matching invite-acceptance UX improvements, so an enterprise customer can pen-test the login → invite → join path without finding cross-tenant escalation or token-replay vectors.

**Architecture:**
- **Backend:** harden `auth/` + `workspace/` services and routers in-place. Invite tokens become hashed at rest; the plaintext stays only in the email/URL (server never reads it back from DB unhashed). `accept_invite` and `set_active_workspace` gain explicit identity/membership guards.
- **Frontend:** keep the existing `/invite/[token]` page shape but split the flow into clearly typed states (`loading | invalid | expired | revoked | accepted | email_mismatch | ready_new | ready_existing | ready_wrong_user`), each with the right CTA. No new routes — only state and a small "sign out and use a different account" affordance.

**Tech Stack:**
- Backend: FastAPI, fastapi-users, Beanie/MongoDB, mongomock-motor (tests), pytest. Cloud rules in `backend/CLAUDE.md` (4-file entity shape, `_core.errors`, `model_validate` at entry).
- Frontend: SvelteKit 2 + Svelte 5 runes mode, Tailwind 4. Three-layer quality gate (`bun run check`, ESLint, `onwarn`) — see `paw-enterprise/CLAUDE.md`.

**Out of scope (deferred to Wave 2/3):** MFA, SSO, members management UI, session listing, API keys, password policy, audit-log API. These are tracked separately and depend on Wave 1 landing first.

**Branch:** `feat/auth-invite-hardening-wave1` (single branch, two PRs — one per repo — landed in order: backend first, then paw-enterprise).

**Commit cadence:** one commit per task. Co-Authored-By trailer as usual; no AI attribution in PR bodies (per `feedback_no_ai_attribution`).

---

## Pre-flight — verify the baseline before touching anything

### Task 0: Confirm baseline passes

**Files to inspect (no edits):**
- `D:/paw/backend/ee/pocketpaw_ee/cloud/auth/router.py:97-103`
- `D:/paw/backend/ee/pocketpaw_ee/cloud/auth/service.py:86-94`
- `D:/paw/backend/ee/pocketpaw_ee/cloud/auth/core.py:49`
- `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/service.py:407-557`
- `D:/paw/backend/ee/pocketpaw_ee/cloud/models/invite.py`

**Step 1:** Run the workspace + auth test scope to capture the green baseline so you can spot regressions.

```bash
cd D:/paw/backend
uv run pytest tests/cloud/workspace tests/cloud/auth -v
```

Expected: all green. If anything is red here, fix that first or stop and report — Wave 1 assumes a clean baseline. (Per `reference_backend_test_env`: ~86 root+cloud failures elsewhere are pre-existing env gaps; the workspace + auth scopes specifically should be clean against mongomock.)

**Step 2:** Note current line counts of the three files about to be touched (helps with sanity-check on the final diff).

```bash
wc -l ee/pocketpaw_ee/cloud/auth/{router.py,service.py,core.py} \
      ee/pocketpaw_ee/cloud/workspace/service.py \
      ee/pocketpaw_ee/cloud/models/invite.py
```

**Step 3:** No commit. Just a mental anchor.

---

## Backend — Part A: `set_active_workspace` membership check (Finding #1)

### Task 2: Refuse to set active workspace the caller isn't a member of

**Files:**
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/auth/service.py:86-94`
- Test: extend `D:/paw/backend/tests/cloud/auth/test_service_v2.py` (existing) — or add a new test if there's no `set_active_workspace` block yet.

**Why:** Any authenticated user can today set their `active_workspace` to *any* workspace id. Downstream code that scopes by `active_workspace` (chat, KB, files) treats that field as ground truth — so this is a silent cross-tenant escalation.

**Step 1: Look for the existing test seam.**

```bash
grep -n "set_active_workspace" tests/cloud/auth/test_service_v2.py
```

If a happy-path test exists, you'll add two cases alongside it. If not, add the file/class.

**Step 2: Write the failing tests.** Append to `tests/cloud/auth/test_service_v2.py`:

```python
async def test_set_active_workspace_rejects_non_member(mongo_db: Any) -> None:
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.auth import service as auth_service
    from pocketpaw_ee.cloud.models.workspace import Workspace as _WS

    user = await _seed_user(email="alice@x.c")
    other_owner = await _seed_user(email="bob@x.c")
    ws = _WS(name="Bob's WS", slug="bobs", owner=str(other_owner.id))
    await ws.insert()

    ctx = _ctx(str(user.id))
    with pytest.raises(Forbidden):
        await auth_service.set_active_workspace(ctx, str(ws.id))


async def test_set_active_workspace_accepts_member(mongo_db: Any) -> None:
    from pocketpaw_ee.cloud.auth import service as auth_service
    from pocketpaw_ee.cloud.models.user import WorkspaceMembership as _M
    from pocketpaw_ee.cloud.models.workspace import Workspace as _WS

    user = await _seed_user(email="alice@x.c")
    ws = _WS(name="Alice's WS", slug="alices", owner=str(user.id))
    await ws.insert()
    user.workspaces.append(_M(workspace=str(ws.id), role="owner", joined_at=datetime.now(UTC)))
    await user.save()

    ctx = _ctx(str(user.id))
    profile = await auth_service.set_active_workspace(ctx, str(ws.id))
    assert profile.active_workspace == str(ws.id)
```

(Reuse the existing `_seed_user`, `_ctx`, and `_seed_user`/imports in the file — don't re-declare. If the file's top doesn't import `Any`, add it.)

**Step 3: Run, watch fail.**

```bash
uv run pytest tests/cloud/auth/test_service_v2.py -k set_active_workspace -v
```

Expected: `rejects_non_member` FAILS (no Forbidden raised).

**Step 4: Implement the check** in `ee/pocketpaw_ee/cloud/auth/service.py`. Replace the body of `set_active_workspace`:

```python
async def set_active_workspace(ctx: RequestContext, workspace_id: str) -> AuthUser:
    if not workspace_id:
        raise ValidationError("workspace_id.required", "workspace_id required")
    doc = await _UserDoc.get(PydanticObjectId(ctx.user_id))
    if doc is None:
        raise NotFound("user", ctx.user_id)
    if not any(m.workspace == workspace_id for m in doc.workspaces):
        raise Forbidden(
            "workspace.not_a_member",
            "You are not a member of that workspace.",
        )
    doc.active_workspace = workspace_id
    await doc.save()
    return _to_domain(doc)
```

Add `Forbidden` to the existing `_core.errors` import at the top of the file:

```python
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
```

**Step 5: Re-run, watch pass.**

```bash
uv run pytest tests/cloud/auth/test_service_v2.py -k set_active_workspace -v
```

**Step 6: Run the wider auth + workspace scope** to catch any test that was relying on the loose behavior.

```bash
uv run pytest tests/cloud/auth tests/cloud/workspace -v
```

Expected: green. If a test seeds a user without membership and then calls `set_active_workspace`, fix the test to seed membership properly (the old behavior was the bug, not the spec).

**Step 7: Commit.**

```bash
git add ee/pocketpaw_ee/cloud/auth/service.py tests/cloud/auth/test_service_v2.py
git commit -m "$(cat <<'EOF'
fix(auth): reject set-active-workspace for non-members

Without this check, any authenticated user could pin their
active_workspace to an arbitrary id and downstream code that scopes
by active_workspace would read across tenants.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Backend — Part C: invite token hashing at rest (Finding #2)

### Task 3: Store sha256 of invite tokens, mail the plaintext

**Files:**
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/models/invite.py:15-37`
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/service.py:91-104, 442-490`
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/dto.py` (likely — add a response that surfaces the plaintext token *once* at create time)
- Test: extend `D:/paw/backend/tests/cloud/workspace/test_service_v2.py`

**Why:** A DB compromise today hands the attacker every unexpired invite token in cleartext. Hashing closes that. Plaintext only lives in the response from `create_invite` (so the inviter can email it / copy it) and in the email link itself; server side, only the hash is stored.

**Step 1: Decide the field shape.** Add `token_hash: Indexed(str, unique=True)`; keep the existing `token` field optional with a deprecation note (so legacy in-flight invites keep working through one release).

Edit `ee/pocketpaw_ee/cloud/models/invite.py`:

```python
"""Invite document — workspace membership invitations.

The plaintext token lives only in the email link the inviter shares.
We persist sha256(plaintext) so a DB read cannot reconstruct a usable
invite link. ``token`` is the legacy plaintext column kept Optional for
backfill during the hashing rollout — new invites set ``token_hash``
and leave ``token`` as None.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from beanie import Document, Indexed
from pydantic import Field


def _default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=7)


def hash_token(plaintext: str) -> str:
    """sha256(plaintext) — the canonical lookup value for an invite token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class Invite(Document):
    """Workspace invitation sent to an email address.

    ``token_hash`` is the authoritative lookup key. ``token`` is the
    legacy plaintext column retained Optional for one release so
    pre-hash invites keep working — backfilled by ``_migrate_invite``
    in the service on first read.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    email: Indexed(str)  # type: ignore[valid-type]
    role: str = Field(default="member", pattern="^(admin|member|viewer)$")
    invited_by: str
    token: str | None = None  # legacy plaintext (deprecated; nulled after migration)
    token_hash: Indexed(str, unique=True) | None = None  # type: ignore[valid-type]
    group: str | None = None
    accepted: bool = False
    revoked: bool = False
    accepted_at: datetime | None = None  # single-use stamp (Task 4)
    expires_at: datetime = Field(default_factory=_default_expiry)

    @property
    def expired(self) -> bool:
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return datetime.now(UTC) > exp

    class Settings:
        name = "invites"
```

**Step 2: Write failing tests** in `tests/cloud/workspace/test_service_v2.py` — append at the bottom:

```python
async def test_create_invite_hashes_token_at_rest(mongo_db: Any, captured_legacy_events) -> None:
    owner = await _seed_user(email="o@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="W", slug="w"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="invitee@x.c", role="member"),
    )

    # The returned domain object carries the plaintext token (the
    # only place it lives outside the email URL).
    assert invite.token and len(invite.token) >= 32

    # The DB row stores the HASH, not the plaintext.
    from pocketpaw_ee.cloud.models.invite import Invite as _D, hash_token

    row = await _D.find_one(_D.token_hash == hash_token(invite.token))
    assert row is not None
    assert row.token in (None, "")  # plaintext column is not populated for new invites


async def test_validate_invite_accepts_plaintext_by_hash(mongo_db: Any, captured_legacy_events) -> None:
    owner = await _seed_user(email="o2@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="W2", slug="w2"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="i2@x.c", role="member"),
    )

    looked_up, ws_name = await workspace_service.validate_invite(invite.token)
    assert looked_up.email == "i2@x.c"
    assert ws_name == "W2"


async def test_validate_invite_unknown_hash_404(mongo_db: Any) -> None:
    with pytest.raises(NotFound):
        await workspace_service.validate_invite("definitely-not-a-real-token")
```

**Step 3: Run, fail.**

```bash
uv run pytest tests/cloud/workspace/test_service_v2.py -k invite -v
```

Expected: `test_create_invite_hashes_token_at_rest` FAILS (token is in the plaintext column, no hash column populated).

**Step 4: Implement.** Edit `ee/pocketpaw_ee/cloud/workspace/service.py`.

4a. Update the imports at the top:

```python
from pocketpaw_ee.cloud.models.invite import Invite as _InviteDoc
from pocketpaw_ee.cloud.models.invite import hash_token
```

4b. Update `_invite_to_domain` so it can carry an Optional plaintext (only set at create-time). Find the existing helper at `:91-104` and add a parameter:

```python
def _invite_to_domain(doc: _InviteDoc, *, plaintext_token: str | None = None) -> Invite:
    return Invite(
        id=str(doc.id),
        workspace_id=doc.workspace,
        email=doc.email,
        role=doc.role,
        invited_by=doc.invited_by,
        token=plaintext_token,  # only populated on create; None on read
        group_id=doc.group,
        accepted=doc.accepted,
        revoked=doc.revoked,
        expired=doc.expired,
        expires_at=doc.expires_at,
    )
```

(Update the `Invite` domain in `workspace/domain.py` if `token` is currently a `str` — make it `str | None`. Grep:

```bash
grep -n "token" ee/pocketpaw_ee/cloud/workspace/domain.py
```

If it's `str`, change to `str | None = None`.)

4c. Update `create_invite` (around `:442-450`) to mint plaintext + store hash:

```python
plaintext = secrets.token_urlsafe(32)
invite_doc = _InviteDoc(
    workspace=workspace_id,
    email=body.email,
    role=body.role,
    invited_by=ctx.user_id,
    token=None,  # plaintext never persisted for new invites
    token_hash=hash_token(plaintext),
    group=body.group_id,
)
await invite_doc.insert()
invite = _invite_to_domain(invite_doc, plaintext_token=plaintext)
```

4d. Update `validate_invite` (around `:482`) to look up by hash, with a fallback for legacy plaintext rows during the rollout window:

```python
async def validate_invite(token: str) -> tuple[Invite, str]:
    """Return ``(invite, workspace_name)``. Raises NotFound if unknown."""
    th = hash_token(token)
    invite_doc = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
    if invite_doc is None:
        # Legacy: an invite created before hashing rollout. One-time
        # backfill so the row stops being plaintext-readable.
        invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
        if invite_doc is None:
            raise NotFound("invite")
        invite_doc.token_hash = th
        invite_doc.token = None
        await invite_doc.save()
    invite = _invite_to_domain(invite_doc)
    ws_doc = await _fetch_workspace(invite.workspace_id)
    ws_name = ws_doc.name if ws_doc is not None else ""
    return invite, ws_name
```

4e. Update `accept_invite` (`:493-545`) the same way — find by hash, fallback on plaintext, backfill on touch:

```python
async def accept_invite(ctx: RequestContext, token: str) -> None:
    th = hash_token(token)
    invite_doc = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
    if invite_doc is None:
        invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
        if invite_doc is None:
            raise NotFound("invite")
        invite_doc.token_hash = th
        invite_doc.token = None
        # don't save yet — accept path below saves once at the end
    # …rest of the body unchanged for now (Task 4 + Task 5 modify it)…
```

4f. Bump the notification source so it doesn't leak plaintext. Around `:472-476`:

```python
source=NotificationSource(
    type="invite",
    id=invite.id,  # the invite document id, not the token
    room_id=invite.group_id,
),
```

(Audit if anything reads `source.id` expecting a token. Quick grep: `grep -rn "source.id" ee/pocketpaw_ee/cloud/notifications/`. If something does, fix it to pull the token from the email URL, not from the notification.)

**Step 5: Run, pass.**

```bash
uv run pytest tests/cloud/workspace/test_service_v2.py -k invite -v
```

**Step 6: Run the full workspace + auth scope.**

```bash
uv run pytest tests/cloud/workspace tests/cloud/auth -v
```

Fix any test that asserted on `invite.token` being a stored plaintext on the DB doc — update those to assert on the domain object (where plaintext lives only at create-time).

**Step 7: Commit.**

```bash
git add ee/pocketpaw_ee/cloud/models/invite.py \
        ee/pocketpaw_ee/cloud/workspace/service.py \
        ee/pocketpaw_ee/cloud/workspace/domain.py \
        tests/cloud/workspace/test_service_v2.py
git commit -m "$(cat <<'EOF'
fix(workspace): hash invitation tokens at rest

DB rows now persist sha256(token) in token_hash; the plaintext is
returned only from create_invite (for the email link / clipboard) and
never read back from storage. Legacy plaintext rows are migrated on
first validate/accept touch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Backend — Part D: single-use invite tokens (Finding #6)

### Task 4: Lock invite token after first successful accept

**Files:**
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/service.py` (`accept_invite`)
- Test: extend `tests/cloud/workspace/test_service_v2.py`

**Why:** Backend already rejects re-accept via `if invite.accepted: raise ConflictError`, but the timing is between "check" and "mark", which is a TOCTOU window. Also, the front-end can re-prompt the user to "Accept" multiple times if their first click fails for a transient reason. Make the accept path atomic with `find_one_and_update`, and record `accepted_at` so the audit row (Wave 2) has a real timestamp.

**Step 1: Write the failing test.** Append:

```python
async def test_accept_invite_is_single_use(mongo_db: Any) -> None:
    owner = await _seed_user(email="own@x.c")
    invitee = await _seed_user(email="inv@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="SU", slug="su"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="inv@x.c", role="member"),
    )
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)
    with pytest.raises(ConflictError):
        await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)


async def test_accept_invite_concurrent_only_one_wins(mongo_db: Any) -> None:
    """Two concurrent accepts on the same token: exactly one succeeds."""
    import asyncio

    owner = await _seed_user(email="own2@x.c")
    inv = await _seed_user(email="i@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="CC", slug="cc"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="i@x.c", role="member"),
    )
    results = await asyncio.gather(
        workspace_service.accept_invite(_ctx(str(inv.id)), invite.token),
        workspace_service.accept_invite(_ctx(str(inv.id)), invite.token),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, ConflictError)]
    assert len(successes) == 1
    assert len(failures) == 1
```

**Step 2: Run, fail.**

```bash
uv run pytest tests/cloud/workspace/test_service_v2.py -k single_use -v
uv run pytest tests/cloud/workspace/test_service_v2.py -k concurrent_only_one -v
```

**Step 3: Implement atomic accept.** In `workspace/service.py`, rewrite `accept_invite`:

```python
async def accept_invite(ctx: RequestContext, token: str) -> None:
    th = hash_token(token)

    # Atomic claim: set accepted=True only if it's currently False.
    # Returns the original doc on success, None on lose.
    collection = _InviteDoc.get_pymongo_collection()
    claimed = await collection.find_one_and_update(
        {"token_hash": th, "accepted": False, "revoked": False},
        {"$set": {"accepted": True, "accepted_at": datetime.now(UTC)}},
        return_document=False,  # we want the BEFORE doc so we know if it existed
    )

    if claimed is None:
        # Disambiguate: is the invite missing, revoked, expired, or already accepted?
        existing = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
        if existing is None:
            # legacy plaintext fallback
            existing = await _InviteDoc.find_one(_InviteDoc.token == token)
        if existing is None:
            raise NotFound("invite")
        if existing.accepted:
            raise ConflictError("invite.already_accepted", "This invite has already been accepted")
        if existing.revoked:
            raise Forbidden("invite.revoked", "This invite has been revoked")
        if existing.expired:
            raise Forbidden("invite.expired", "This invite has expired")
        # Legacy row that needed migration — retry once with the now-rehydrated hash.
        existing.token_hash = th
        existing.token = None
        await existing.save()
        claimed = await collection.find_one_and_update(
            {"_id": existing.id, "accepted": False, "revoked": False},
            {"$set": {"accepted": True, "accepted_at": datetime.now(UTC)}},
            return_document=False,
        )
        if claimed is None:
            raise ConflictError("invite.already_accepted", "This invite has already been accepted")

    # claimed is the BEFORE doc; rebuild the domain object from it for the
    # downstream emit/membership logic.
    invite = _invite_to_domain(_InviteDoc.model_validate(claimed))

    ws_doc = await _fetch_workspace(invite.workspace_id)
    if ws_doc is None:
        # Roll back the claim — we don't want a "consumed" stamp on a
        # tombstoned workspace where the invitee never actually joined.
        await collection.update_one(
            {"_id": claimed["_id"]},
            {"$set": {"accepted": False, "accepted_at": None}},
        )
        raise NotFound("workspace", invite.workspace_id)

    already_member = await _get_member_role(invite.workspace_id, ctx.user_id) is not None
    if not already_member:
        member_count = await _count_members(invite.workspace_id)
        if member_count >= ws_doc.seats:
            await collection.update_one(
                {"_id": claimed["_id"]},
                {"$set": {"accepted": False, "accepted_at": None}},
            )
            raise SeatLimitError(ws_doc.seats)
        await _add_member(
            invite.workspace_id,
            ctx.user_id,
            role=invite.role,
            set_active=True,
        )

    # Events (unchanged)
    await event_bus.emit(
        "invite.accepted",
        {
            "workspace_id": invite.workspace_id,
            "user_id": ctx.user_id,
            "invite_id": invite.id,
            "group_id": invite.group_id,
        },
    )
    wid = invite.workspace_id
    await emit(WorkspaceInviteAccepted(
        data={"workspace_id": wid, "invite_id": invite.id, "user_id": ctx.user_id}
    ))
    await emit(WorkspaceMemberAdded(
        data={"workspace_id": wid, "user_id": ctx.user_id, "role": invite.role}
    ))
    get_resolver().invalidate_workspace(wid)
```

**Step 4: Run, pass.**

```bash
uv run pytest tests/cloud/workspace/test_service_v2.py -k "single_use or concurrent_only_one or invite" -v
```

**Step 5: Commit.**

```bash
git add ee/pocketpaw_ee/cloud/workspace/service.py tests/cloud/workspace/test_service_v2.py
git commit -m "$(cat <<'EOF'
fix(workspace): make invite accept atomic and single-use

find_one_and_update claims the invite row under {accepted:false,
revoked:false}; the loser of a concurrent race gets a 409. Rolls back
the claim if the workspace turns out to be deleted or seat-capped so
the invite stays usable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Backend — Part E: email match on accept (Finding #3)

### Task 5: Verify the logged-in user's email matches the invitee's

**Files:**
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/service.py` (`accept_invite`)
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/_core/errors.py` if `Forbidden` doesn't already have a clear "email mismatch" subcode pattern — most likely just a string code is fine.
- Test: extend `tests/cloud/workspace/test_service_v2.py`

**Why:** Combined with token leakage, this is the most exploitable hole in the whole flow — whoever holds the link claims the role. We tie acceptance to the logged-in identity. (UI-side fix in Task 7 lets a user log out and back in with the matching account.)

**Step 1: Failing test.** Append:

```python
async def test_accept_invite_rejects_email_mismatch(mongo_db: Any) -> None:
    from pocketpaw_ee.cloud._core.errors import Forbidden

    owner = await _seed_user(email="own3@x.c")
    invitee = await _seed_user(email="invitee@x.c")
    impostor = await _seed_user(email="impostor@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="EM", slug="em"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="invitee@x.c", role="admin"),
    )
    with pytest.raises(Forbidden, match="email"):
        await workspace_service.accept_invite(_ctx(str(impostor.id)), invite.token)

    # Invite is still usable by the real invitee — the rejected claim
    # must NOT have consumed it.
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)


async def test_accept_invite_case_insensitive_email(mongo_db: Any) -> None:
    owner = await _seed_user(email="own4@x.c")
    invitee = await _seed_user(email="Mixed@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="CI", slug="ci"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="mixed@x.c", role="member"),
    )
    # Should succeed — email comparison is case-insensitive.
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)
```

**Step 2: Run, fail.**

```bash
uv run pytest tests/cloud/workspace/test_service_v2.py -k "email_mismatch or case_insensitive_email" -v
```

**Step 3: Implement.** In `accept_invite`, **before** the atomic claim block, add:

```python
# Identity check: the logged-in user's email must match the invitee's.
# Comparison is case-insensitive (emails are case-insensitive at the
# mailbox level for all practical providers).
viewer = await _UserDoc.get(PydanticObjectId(ctx.user_id))
if viewer is None:
    raise NotFound("user", ctx.user_id)

# Look up the invite row WITHOUT claiming it, so a mismatch leaves
# the token usable by the rightful invitee.
preview = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
if preview is None:
    preview = await _InviteDoc.find_one(_InviteDoc.token == token)
if preview is None:
    raise NotFound("invite")
if preview.email.lower() != (viewer.email or "").lower():
    raise Forbidden(
        "invite.email_mismatch",
        "This invite was sent to a different email address. "
        "Sign in with the invited account to accept.",
    )
```

Then proceed with the atomic claim from Task 4. Note: the preview read is intentionally separate from the claim — if you race-accept after the email check, the claim's `{accepted:false}` filter still enforces single-use.

**Step 4: Run, pass.**

```bash
uv run pytest tests/cloud/workspace/test_service_v2.py -k invite -v
```

**Step 5: Full scope.**

```bash
uv run pytest tests/cloud/workspace tests/cloud/auth -v
```

Fix any test where the invite-accept call was using a context for a user with a different email than the invite. The test seeding pattern needs `_seed_user(email=<same as invite>)` now.

**Step 6: Commit.**

```bash
git add ee/pocketpaw_ee/cloud/workspace/service.py tests/cloud/workspace/test_service_v2.py
git commit -m "$(cat <<'EOF'
fix(workspace): require invitee email to match logged-in user

Without this check, anyone holding a leaked invite link could claim
the pre-assigned role regardless of which account they were signed
in to. The preview read intentionally precedes the atomic claim so a
mismatched attempt leaves the token usable by the rightful invitee.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Backend — Part F: surface typed accept errors to the frontend

### Task 6: Add a `GET /workspaces/invites/{token}/preview` endpoint that hints at the right CTA

**Files:**
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/router.py` (existing validate route — extend or replace with `/preview` that also signals viewer status)
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/workspace/dto.py` (response shape)
- Test: extend `tests/cloud/workspace/test_service_v2.py` OR new `tests/cloud/workspace/test_router_invite.py`

**Why:** The frontend currently collapses every failure into "Invalid or expired invite link" because the public preview endpoint returns the same thing for all error modes. Wave 1 UX (Task 7) wants distinct CTAs for `expired`, `revoked`, `already_accepted`, `email_mismatch`, `ready_existing` (logged in as right user), `ready_wrong_user` (logged in as someone else), `ready_new` (not logged in). A typed preview endpoint is the cleanest way to deliver that.

**Step 1: Find the existing route.**

```bash
grep -n "invites/" ee/pocketpaw_ee/cloud/workspace/router.py
```

You should see something like `@router.get("/workspaces/invites/{token}")` calling `validate_invite`. Don't delete it — frontend depends on it. Add a sibling route `GET /workspaces/invites/{token}/preview` that returns the typed shape, and update the frontend to prefer the new one. Keep the old endpoint returning the same body for one release.

**Step 2: Define the response DTO.** Edit `workspace/dto.py` — add at the bottom (keep imports tidy):

```python
class InvitePreviewResponse(BaseModel):
    """Typed preview of an invite token for the accept UI.

    ``state`` is the single field the UI switches on:
      - ``ready_new``         — token is valid, viewer is anonymous; show register form
      - ``ready_existing``    — token is valid, viewer logged in with matching email
      - ``ready_wrong_user``  — token is valid, viewer logged in with a DIFFERENT email
      - ``expired``           — token expired
      - ``revoked``           — token revoked by inviter
      - ``already_accepted``  — token already redeemed
      - ``not_found``         — token doesn't exist (or was tampered)
    """

    state: Literal[
        "ready_new",
        "ready_existing",
        "ready_wrong_user",
        "expired",
        "revoked",
        "already_accepted",
        "not_found",
    ]
    email: str | None = None         # the invitee's email; surfaces in UI for confirmation
    role: str | None = None
    workspace_name: str | None = None
    group: str | None = None
    group_name: str | None = None
    viewer_email: str | None = None  # echoed for the "you're signed in as X" line
```

Add `Literal` to the existing typing imports at the top of `dto.py`.

**Step 3: Add a service helper.** In `workspace/service.py`, after `validate_invite`:

```python
async def preview_invite(token: str, viewer_user_id: str | None) -> dict:
    """Typed preview for the accept UI — never raises, returns a state."""
    th = hash_token(token)
    invite_doc = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
    if invite_doc is None:
        invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
    if invite_doc is None:
        return {"state": "not_found"}

    if invite_doc.accepted:
        return {"state": "already_accepted", "email": invite_doc.email}
    if invite_doc.revoked:
        return {"state": "revoked", "email": invite_doc.email}
    if invite_doc.expired:
        return {"state": "expired", "email": invite_doc.email}

    ws_doc = await _fetch_workspace(invite_doc.workspace)
    ws_name = ws_doc.name if ws_doc is not None else ""

    viewer_email: str | None = None
    state = "ready_new"
    if viewer_user_id:
        viewer = await _UserDoc.get(PydanticObjectId(viewer_user_id))
        if viewer is not None:
            viewer_email = viewer.email
            if (viewer.email or "").lower() == invite_doc.email.lower():
                state = "ready_existing"
            else:
                state = "ready_wrong_user"

    # group_name is best-effort — leave None if the lookup is awkward
    # from this module; the chat router can fetch it client-side too.
    return {
        "state": state,
        "email": invite_doc.email,
        "role": invite_doc.role,
        "workspace_name": ws_name,
        "group": invite_doc.group,
        "group_name": None,
        "viewer_email": viewer_email,
    }
```

**Step 4: Wire the route.** In `workspace/router.py`:

```python
@router.get("/workspaces/invites/{token}/preview", response_model=InvitePreviewResponse)
async def preview_invite_route(
    token: str,
    viewer: Any | None = Depends(current_optional_user),
) -> dict:
    viewer_id = str(viewer.id) if viewer is not None else None
    return await workspace_service.preview_invite(token, viewer_id)
```

Add `current_optional_user` to the imports (it's already exported from `auth/core.py`).

**Step 5: Tests.** Add `tests/cloud/workspace/test_router_invite.py` OR extend the service test:

```python
async def test_preview_states(mongo_db: Any) -> None:
    owner = await _seed_user(email="o@x.c")
    matching_viewer = await _seed_user(email="m@x.c")
    other_viewer = await _seed_user(email="other@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="P", slug="p"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="m@x.c", role="member"),
    )

    # Anonymous viewer
    out = await workspace_service.preview_invite(invite.token, viewer_user_id=None)
    assert out["state"] == "ready_new"
    assert out["email"] == "m@x.c"

    # Logged in as matching
    out = await workspace_service.preview_invite(invite.token, viewer_user_id=str(matching_viewer.id))
    assert out["state"] == "ready_existing"
    assert out["viewer_email"] == "m@x.c"

    # Logged in as wrong user
    out = await workspace_service.preview_invite(invite.token, viewer_user_id=str(other_viewer.id))
    assert out["state"] == "ready_wrong_user"
    assert out["viewer_email"] == "other@x.c"

    # Revoke it
    await workspace_service.revoke_invite(ws.id, invite.id)
    out = await workspace_service.preview_invite(invite.token, viewer_user_id=None)
    assert out["state"] == "revoked"

    # Unknown token
    out = await workspace_service.preview_invite("not-a-token", viewer_user_id=None)
    assert out["state"] == "not_found"
```

**Step 6: Run.**

```bash
uv run pytest tests/cloud/workspace -v
```

**Step 7: Lint + types.**

```bash
uv run ruff check ee/pocketpaw_ee/cloud/auth ee/pocketpaw_ee/cloud/workspace ee/pocketpaw_ee/cloud/models/invite.py
uv run ruff format ee/pocketpaw_ee/cloud/auth ee/pocketpaw_ee/cloud/workspace ee/pocketpaw_ee/cloud/models/invite.py
uv run mypy ee/pocketpaw_ee/cloud/auth ee/pocketpaw_ee/cloud/workspace ee/pocketpaw_ee/cloud/models/invite.py
```

**Step 8: Commit.**

```bash
git add ee/pocketpaw_ee/cloud/workspace/{router.py,service.py,dto.py} tests/cloud/workspace/
git commit -m "$(cat <<'EOF'
feat(workspace): typed invite preview endpoint

Adds GET /workspaces/invites/{token}/preview returning a discriminated
``state`` so the accept UI can branch on ready_new / ready_existing /
ready_wrong_user / expired / revoked / already_accepted / not_found
instead of collapsing every failure into "invalid link".

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Backend — Part G: open the backend PR

### Task 7: Push the backend branch and open the PR

**Step 1: Verify everything in scope is green.**

```bash
cd D:/paw/backend
uv run pytest tests/cloud/workspace tests/cloud/auth -v
uv run ruff check . && uv run ruff format --check .
```

**Step 2: Push.**

```bash
git push -u origin feat/auth-invite-hardening-wave1
```

**Step 3: Open the PR** with `gh pr create`. Body — no AI attribution footer (per memory):

```
## Summary
- Hashes invite tokens at rest (sha256); plaintext lives only in the email link.
- Atomic single-use accept (find_one_and_update on accepted=false).
- Requires invite email == logged-in user's email (case-insensitive).
- Adds GET /workspaces/invites/{token}/preview with discriminated state for the accept UI.
- Refuses set_active_workspace for non-members.

## Test plan
- [ ] tests/cloud/workspace and tests/cloud/auth fully green
- [ ] Manual: create invite as owner, accept while signed in as the wrong user → 403 with `invite.email_mismatch`
- [ ] Manual: accept twice in quick succession → second returns 409 `invite.already_accepted`
```

---

## Frontend — Part H: rebuild the accept UX around the preview endpoint

### Task 8: Switch the invite page to the typed preview

**Files:**
- Modify: `D:/paw/paw-enterprise/src/routes/invite/[token]/+page.svelte`
- (Optional, only if you have an api wrapper) Modify: `D:/paw/paw-enterprise/src/lib/core/workspaces/api.ts`

**Why:** Implements UX findings U1, U2, U3, U6, U7 in one place. The page becomes a state machine driven by the backend's `state` field, so each failure mode has the right CTA instead of collapsing into "invalid link".

**Step 1: Pull current state.** Read the file once so you have line numbers in your head:

```bash
cat src/routes/invite/[token]/+page.svelte
```

(You already read it during the audit — `:42-47` is the catch-all error, `:134-156` is the new-user form, `:158-161` is the "signed in as X" branch.)

**Step 2: Rewrite the `<script>` block.** Replace lines 4–110 with the state-machine version below. Keep the `<style>` and the markup container — only the script and the `{#if ...}` chain change.

```svelte
<script lang="ts">
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";
  import { resolve } from "$app/paths";
  import { onMount } from "svelte";
  import { http, HttpError } from "$lib/core/shared/http";
  import { authStore } from "$lib/core/auth/store.svelte";
  import { authService } from "$lib/core/auth/service";
  import { signInEmail } from "$lib/core/auth/api";
  import { toast } from "svelte-sonner";

  type PreviewState =
    | "ready_new"
    | "ready_existing"
    | "ready_wrong_user"
    | "expired"
    | "revoked"
    | "already_accepted"
    | "not_found";

  interface InvitePreview {
    state: PreviewState;
    email: string | null;
    role: string | null;
    workspace_name: string | null;
    group: string | null;
    group_name: string | null;
    viewer_email: string | null;
  }

  let token = $derived($page.params.token);

  let loading = $state(true);
  let preview = $state<InvitePreview | null>(null);
  let loadError = $state<string | null>(null);

  // ready_new form fields
  let fullName = $state("");
  let password = $state("");
  let confirmPassword = $state("");
  let accepting = $state(false);

  onMount(async () => {
    // Best-effort: hydrate auth so the preview knows the viewer.
    try {
      await authService.init();
    } catch { /* anonymous viewer is fine */ }

    try {
      preview = await http<InvitePreview>(
        `/workspaces/invites/${token}/preview`,
      );
    } catch (e) {
      if (e instanceof HttpError) {
        loadError = e.body?.detail || "Could not validate this invite.";
      } else {
        loadError = "Could not validate this invite.";
      }
    } finally {
      loading = false;
    }
  });

  async function acceptExisting() {
    if (!preview) return;
    accepting = true;
    try {
      await http<{ ok: boolean }>(
        `/workspaces/invites/${token}/accept`,
        { method: "POST" },
      );
      await authService.init();
      toast.success(`Welcome to ${preview.workspace_name}!`);
      // U6: only run member onboarding for users who haven't completed it.
      const needsOnboarding = !authStore.user?.onboarded;
      goto(resolve(needsOnboarding ? "/onboarding?flow=member" : "/"));
    } catch (e: any) {
      toast.error(e?.body?.detail || "Failed to accept invite.");
    } finally {
      accepting = false;
    }
  }

  async function registerAndAccept() {
    if (!preview) return;
    if (!fullName.trim() || !password) {
      toast.error("Please fill in your name and password.");
      return;
    }
    if (password !== confirmPassword) {
      toast.error("Passwords don't match.");
      return;
    }
    if (password.length < 12) {
      toast.error("Password must be at least 12 characters.");
      return;
    }
    accepting = true;
    try {
      await http("/auth/register", {
        method: "POST",
        body: JSON.stringify({
          email: preview.email,
          password,
          full_name: fullName.trim(),
        }),
      });
      await signInEmail(preview.email!, password);
      await authService.init();
      await acceptExisting();
    } catch (e: any) {
      toast.error(e?.body?.detail || "Could not create your account.");
      accepting = false;
    }
  }

  async function signOutAndRetry() {
    // U2: let the user log out and try again as the right account.
    try {
      await http("/auth/logout", { method: "POST" });
    } catch { /* ignore — clearing local state is what matters */ }
    authStore.clear?.();
    // Reload so the preview re-runs anonymously.
    window.location.reload();
  }

  async function decline() {
    // U7: client-side decline. We don't have a /decline endpoint yet
    // (Wave 2); for now just navigate away with a toast so the user
    // gets closure. Replace with POST /workspaces/invites/{token}/decline
    // when that ships.
    toast.info("Invite declined.");
    goto(resolve("/"));
  }
</script>
```

**Step 3: Replace the markup.** The existing `<div class="invite-card">…</div>` content becomes a `{#if}` chain over `preview.state`. Replace lines 113–163 with:

```svelte
<div class="invite-page">
  <div class="invite-card">
    {#if loading}
      <div class="loading">
        <div class="spinner"></div>
        <p>Validating invite…</p>
      </div>
    {:else if loadError || !preview}
      <div class="error-state">
        <h2>Couldn't load this invite</h2>
        <p>{loadError ?? "Please try the link again."}</p>
        <a href={resolve("/")} class="link">Go to app</a>
      </div>
    {:else if preview.state === "not_found"}
      <div class="error-state">
        <h2>Invite not found</h2>
        <p>This link doesn't match any pending invite. It may have been mistyped — ask whoever invited you to resend.</p>
        <a href={resolve("/")} class="link">Go to app</a>
      </div>
    {:else if preview.state === "expired"}
      <div class="error-state">
        <h2>This invite has expired</h2>
        <p>Invites expire after 7 days. Ask your workspace admin to send a fresh one.</p>
        <a href={resolve("/")} class="link">Go to app</a>
      </div>
    {:else if preview.state === "revoked"}
      <div class="error-state">
        <h2>Invite revoked</h2>
        <p>Your workspace admin cancelled this invite. Reach out to them if you still need access.</p>
        <a href={resolve("/")} class="link">Go to app</a>
      </div>
    {:else if preview.state === "already_accepted"}
      <div class="error-state">
        <h2>Invite already accepted</h2>
        <p>This invite has been used. If you're already a member, just sign in.</p>
        <a href={resolve("/")} class="link">Go to app</a>
      </div>
    {:else if preview.state === "ready_wrong_user"}
      <h2 class="title">Wrong account</h2>
      <p class="subtitle">
        This invite was sent to <strong>{preview.email}</strong>, but you're signed in
        as <strong>{preview.viewer_email}</strong>.
      </p>
      <p class="note">Sign out and try the link again with the right account, or close this tab.</p>
      <button class="btn" onclick={signOutAndRetry} disabled={accepting}>
        Sign out and use {preview.email}
      </button>
      <button class="btn btn-secondary" onclick={decline} disabled={accepting}>
        Not me — decline
      </button>
    {:else if preview.state === "ready_existing"}
      <h2 class="title">Join {preview.workspace_name}</h2>
      <p class="subtitle">
        You've been invited as <strong>{preview.role}</strong>.
        {#if preview.group_name}You'll join the <strong>{preview.group_name}</strong> group.{/if}
      </p>
      <p class="note">Signed in as <strong>{preview.viewer_email}</strong>.</p>
      <button class="btn" onclick={acceptExisting} disabled={accepting}>
        {accepting ? "Joining…" : `Accept & Join ${preview.workspace_name}`}
      </button>
      <button class="btn btn-secondary" onclick={decline} disabled={accepting}>
        Decline
      </button>
    {:else}
      <!-- ready_new -->
      <h2 class="title">Join {preview.workspace_name}</h2>
      <p class="subtitle">
        You've been invited as <strong>{preview.role}</strong>.
        Create your account to get started.
      </p>
      <form onsubmit={(e) => { e.preventDefault(); registerAndAccept(); }}>
        <div class="field">
          <label for="inv-email">Email</label>
          <input id="inv-email" type="email" value={preview.email ?? ""} disabled class="input" />
        </div>
        <div class="field">
          <label for="inv-name">Full Name</label>
          <input id="inv-name" type="text" bind:value={fullName} class="input" placeholder="Your name" disabled={accepting} />
        </div>
        <div class="field">
          <label for="inv-pass">Password</label>
          <input id="inv-pass" type="password" bind:value={password} class="input" placeholder="At least 12 characters" disabled={accepting} />
        </div>
        <div class="field">
          <label for="inv-confirm">Confirm Password</label>
          <input id="inv-confirm" type="password" bind:value={confirmPassword} class="input" placeholder="Repeat password" disabled={accepting} />
        </div>
        <button type="submit" class="btn" disabled={accepting}>
          {accepting ? "Creating account…" : "Create Account & Join"}
        </button>
      </form>
    {/if}
  </div>
</div>
```

**Step 4: Add the secondary button style** to the existing `<style>` block (just append before the closing `</style>`):

```css
.btn-secondary {
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.12);
  color: rgba(255, 255, 255, 0.75);
  margin-top: 8px;
}
.btn-secondary:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.04);
}
```

**Step 5: Type-check.**

```bash
cd D:/paw/paw-enterprise
bun run check
```

Expected: no new errors in `routes/invite/[token]/+page.svelte`. If `authStore.clear` doesn't exist on the store yet, either add it (one-line method that resets the runed state) or replace the call with whatever method the store exposes for "forget the current user." Grep first:

```bash
grep -n "clear\|logout\|signOut" src/lib/core/auth/store.svelte.ts
```

**Step 6: Manual sanity in dev.**

```bash
bun run dev
```

Open the running backend (`cd ../backend && uv run pocketpaw serve --dev` — per `feedback_pocketpaw_serve`). In a second browser session:

1. Create a workspace as user A.
2. Invite `userB@x.c`.
3. Open the invite link while logged in as A → should hit `ready_wrong_user`, show "Sign out and use userB@x.c".
4. Click sign-out — page reloads, hits `ready_new`, register flow shows.
5. Fill in B's details, accept → lands at `/` or `/onboarding?flow=member`.
6. Open the link again → `already_accepted`.

**Step 7: Commit (in the paw-enterprise repo).**

```bash
cd D:/paw/paw-enterprise
git add src/routes/invite/[token]/+page.svelte
git commit -m "$(cat <<'EOF'
feat(invite): typed accept flow with explicit failure CTAs

Drives the page off the backend's typed preview endpoint instead of
collapsing every failure into "invalid link". Adds a "sign out and use
the right account" path for ready_wrong_user, a Decline action, and a
12-char password floor that matches what the backend will enforce next
sprint.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Frontend — Part I: open the paw-enterprise PR

### Task 9: Push and open PR

**Step 1: Push.**

```bash
git push -u origin feat/auth-invite-hardening-wave1
```

**Step 2: `gh pr create`.** Body:

```
## Summary
- Drives /invite/[token] off the backend's typed `preview` endpoint.
- Adds explicit CTAs for expired / revoked / already_accepted / not_found.
- Adds "sign out and use a different account" for ready_wrong_user (UX gap U2).
- Skips the member onboarding wizard for users who already onboarded (U6).
- Adds a Decline button (U7).
- Bumps the client-side password floor to 12 chars to match the backend roadmap.

Depends on the backend PR landing first so the /preview endpoint exists.

## Test plan
- [ ] Cold link, anonymous → ready_new, register + accept works
- [ ] Cold link, signed in as right user → ready_existing, accept works
- [ ] Cold link, signed in as wrong user → ready_wrong_user, sign-out button works
- [ ] Revoked invite → revoked state
- [ ] Expired invite (manually set expires_at in Mongo) → expired state
- [ ] Already-accepted invite (re-open link) → already_accepted state
```

---

## Wrap-up

### Task 10: Update memory + bookkeeping

**Step 1: Update `MEMORY.md`** to add a project memory pointing at this plan as the resume point if work is paused:

Create `C:/Users/Rohit/.claude/projects/D--paw/memory/project_auth_invite_hardening.md`:

```markdown
---
name: project-auth-invite-hardening
description: Wave 1 of auth/RBAC/invite hardening — security holes + invite UX. Branch feat/auth-invite-hardening-wave1, plan at backend/docs/plans/2026-05-26-auth-rbac-invite-hardening-wave1.md.
metadata:
  type: project
---

Wave 1 of the auth/RBAC/invite audit: 4 P0 backend fixes + matched invite-UX states.

**Why:** Pre-enterprise pen-test prep. The original audit found cross-tenant
escalation via set_active_workspace, plaintext invite tokens in Mongo,
no email-match check on accept, and no single-use. Plus the accept UI
collapsed every failure into one error.

**How to apply:** On "continue wave 1," resume from
backend/docs/plans/2026-05-26-auth-rbac-invite-hardening-wave1.md — the
plan is task-ordered and each task is a single commit. Wave 2 (members
page) and Wave 3 (MFA / SSO / sessions / API keys) are separate plans
written after Wave 1 lands.
```

Then add to `MEMORY.md`:

```
- [Auth invite hardening Wave 1](project_auth_invite_hardening.md) — backend security + invite UX, branch feat/auth-invite-hardening-wave1, plan at backend/docs/plans/2026-05-26-auth-rbac-invite-hardening-wave1.md
```

**Step 2: Park Wave 2 + Wave 3 backlog** at the bottom of this plan file (or in a sibling `2026-05-26-auth-rbac-invite-hardening-backlog.md` if the user prefers) so nothing in the original audit is lost:

- **Wave 2** — members page (U8–U11), rate limiting (#5), audit-log rows (#13), invite resend cooldown (U17), bulk invite (U16), owner-role hard-confirm (U18), copy-link button (U19), audit history view (U20), realtime channel guards (#11), self-demote / last-owner UI guard (#10), stale-invite GC (#12).
- **Wave 3** — MFA (#17), API keys (#20), session listing (#21), SSO/SAML (#18), domain capture (#19), password policy + HIBP (#22), admin audit log API (#23), JIT deprovisioning (#24), CSRF cookie attrs audit (#25), group-agent workspace check (#8), `get_workspace_plan` fail-safe (#9), inviter privilege re-check on revoke+reinvite (#15), soft-deleted workspace accept guard (#16).

---

## Roll-up checklist

- [ ] Task 0  baseline green
- [ ] Task 2  set_active_workspace membership check
- [ ] Task 3  invite token hashed at rest
- [ ] Task 4  invite accept is atomic + single-use
- [ ] Task 5  invite accept enforces email match
- [ ] Task 6  GET /workspaces/invites/{token}/preview typed state
- [ ] Task 7  backend PR opened
- [ ] Task 8  frontend invite page state machine
- [ ] Task 9  paw-enterprise PR opened
- [ ] Task 10 memory + backlog updates
