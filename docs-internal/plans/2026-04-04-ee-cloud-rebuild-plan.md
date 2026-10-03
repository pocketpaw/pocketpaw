# EE Cloud Module Rebuild — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Strip and rebuild `ee/cloud/` with domain-driven subpackages, proper service layers, Pydantic validation, WebSocket (replacing Socket.IO), and full test coverage.

**Architecture:** Domain subpackages (auth, workspace, chat, pockets, sessions, agents) each with router/service/schemas. Shared cross-cutting concerns (errors, events, permissions, deps, db). Native FastAPI WebSocket replaces Socket.IO. TDD throughout.

**Tech Stack:** Python 3.11+, FastAPI, Beanie (async MongoDB ODM), fastapi-users (JWT auth), Pydantic v2, pytest + pytest-asyncio, httpx (test client)

---

## Phase 1: Foundation (shared/ + model cleanup)

### Task 1: Create shared/errors.py — Unified Error Hierarchy

**Files:**
- Create: `ee/cloud/shared/__init__.py`
- Create: `ee/cloud/shared/errors.py`
- Create: `tests/cloud/__init__.py`
- Create: `tests/cloud/test_errors.py`

**Step 1: Write the failing test**

```python
# tests/cloud/__init__.py
# (empty)

# tests/cloud/test_errors.py
from __future__ import annotations

import pytest
from ee.cloud.shared.errors import (
    CloudError,
    NotFound,
    Forbidden,
    ConflictError,
    ValidationError,
    SeatLimitError,
)


def test_cloud_error_base():
    err = CloudError(404, "test.not_found", "Thing not found")
    assert err.status_code == 404
    assert err.code == "test.not_found"
    assert err.message == "Thing not found"


def test_not_found():
    err = NotFound("group", "abc123")
    assert err.status_code == 404
    assert err.code == "group.not_found"
    assert "abc123" in err.message


def test_forbidden():
    err = Forbidden("workspace.not_member")
    assert err.status_code == 403
    assert err.code == "workspace.not_member"


def test_conflict():
    err = ConflictError("workspace.slug_taken", "Slug already in use")
    assert err.status_code == 409
    assert err.code == "workspace.slug_taken"


def test_validation_error():
    err = ValidationError("message.too_long", "Max 10000 chars")
    assert err.status_code == 422
    assert err.code == "message.too_long"


def test_seat_limit():
    err = SeatLimitError(seats=5)
    assert err.status_code == 402
    assert "5" in err.message


def test_cloud_error_to_dict():
    err = NotFound("group", "abc123")
    d = err.to_dict()
    assert d == {"error": {"code": "group.not_found", "message": err.message}}
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_errors.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Write minimal implementation**

```python
# ee/cloud/shared/__init__.py
"""Shared cross-cutting concerns for ee/cloud."""

# ee/cloud/shared/errors.py
"""Unified error hierarchy for cloud module.

All cloud endpoints raise CloudError subclasses. A single FastAPI
exception handler converts them to consistent JSON:
    {"error": {"code": "group.not_found", "message": "Group abc123 not found"}}
"""

from __future__ import annotations


class CloudError(Exception):
    """Base cloud error with status code, machine-readable code, and message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFound(CloudError):
    def __init__(self, resource: str, resource_id: str = "") -> None:
        detail = f"{resource.title()} {resource_id} not found" if resource_id else f"{resource.title()} not found"
        super().__init__(404, f"{resource}.not_found", detail)


class Forbidden(CloudError):
    def __init__(self, code: str, message: str = "Access denied") -> None:
        super().__init__(403, code, message)


class ConflictError(CloudError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class ValidationError(CloudError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(422, code, message)


class SeatLimitError(CloudError):
    def __init__(self, seats: int) -> None:
        super().__init__(402, "workspace.seat_limit_reached", f"Workspace seat limit ({seats}) reached")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cloud/test_errors.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add ee/cloud/shared/ tests/cloud/
git commit -m "feat(cloud): add unified error hierarchy for cloud module"
```

---

### Task 2: Create shared/events.py — Internal Async Event Bus

**Files:**
- Create: `ee/cloud/shared/events.py`
- Create: `tests/cloud/test_events.py`

**Step 1: Write the failing test**

```python
# tests/cloud/test_events.py
from __future__ import annotations

import pytest
from ee.cloud.shared.events import EventBus


@pytest.fixture
def bus():
    return EventBus()


async def test_subscribe_and_emit(bus: EventBus):
    received = []

    async def handler(data):
        received.append(data)

    bus.subscribe("message.sent", handler)
    await bus.emit("message.sent", {"group_id": "g1", "content": "hello"})
    assert len(received) == 1
    assert received[0]["group_id"] == "g1"


async def test_multiple_handlers(bus: EventBus):
    results = []

    async def h1(data):
        results.append("h1")

    async def h2(data):
        results.append("h2")

    bus.subscribe("invite.accepted", h1)
    bus.subscribe("invite.accepted", h2)
    await bus.emit("invite.accepted", {})
    assert results == ["h1", "h2"]


async def test_emit_unknown_event_does_nothing(bus: EventBus):
    # Should not raise
    await bus.emit("nonexistent.event", {})


async def test_unsubscribe(bus: EventBus):
    received = []

    async def handler(data):
        received.append(data)

    bus.subscribe("test.event", handler)
    bus.unsubscribe("test.event", handler)
    await bus.emit("test.event", {"x": 1})
    assert len(received) == 0


async def test_handler_error_does_not_stop_others(bus: EventBus):
    results = []

    async def bad_handler(data):
        raise RuntimeError("boom")

    async def good_handler(data):
        results.append("ok")

    bus.subscribe("test.event", bad_handler)
    bus.subscribe("test.event", good_handler)
    await bus.emit("test.event", {})
    assert results == ["ok"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_events.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Write minimal implementation**

```python
# ee/cloud/shared/events.py
"""In-process async event bus for cross-domain side effects.

Usage:
    bus = EventBus()
    bus.subscribe("message.sent", notify_mentions)
    await bus.emit("message.sent", {"group_id": "...", "sender": "..."})

Handlers that raise are logged and skipped — never block other handlers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    """Simple async pub/sub for internal cloud events."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        for handler in self._handlers.get(event, []):
            try:
                await handler(data)
            except Exception:
                logger.exception("Event handler failed for %s", event)


# Module-level singleton — import and use directly
event_bus = EventBus()
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cloud/test_events.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add ee/cloud/shared/events.py tests/cloud/test_events.py
git commit -m "feat(cloud): add internal async event bus for cross-domain side effects"
```

---

### Task 3: Create shared/permissions.py — Role & Access Checks

**Files:**
- Create: `ee/cloud/shared/permissions.py`
- Create: `tests/cloud/test_permissions.py`

**Step 1: Write the failing test**

```python
# tests/cloud/test_permissions.py
from __future__ import annotations

import pytest
from ee.cloud.shared.permissions import (
    WorkspaceRole,
    PocketAccess,
    check_workspace_role,
    check_pocket_access,
)
from ee.cloud.shared.errors import Forbidden


def test_workspace_role_hierarchy():
    assert WorkspaceRole.OWNER.level > WorkspaceRole.ADMIN.level
    assert WorkspaceRole.ADMIN.level > WorkspaceRole.MEMBER.level


def test_check_workspace_role_passes():
    # owner passes admin check
    check_workspace_role("owner", minimum="admin")


def test_check_workspace_role_fails():
    with pytest.raises(Forbidden):
        check_workspace_role("member", minimum="admin")


def test_pocket_access_hierarchy():
    assert PocketAccess.OWNER.level > PocketAccess.EDIT.level
    assert PocketAccess.EDIT.level > PocketAccess.COMMENT.level
    assert PocketAccess.COMMENT.level > PocketAccess.VIEW.level


def test_check_pocket_access_passes():
    check_pocket_access("edit", minimum="view")


def test_check_pocket_access_fails():
    with pytest.raises(Forbidden):
        check_pocket_access("view", minimum="edit")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_permissions.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Write minimal implementation**

```python
# ee/cloud/shared/permissions.py
"""Role and access level checks for cloud resources."""

from __future__ import annotations

from enum import Enum

from ee.cloud.shared.errors import Forbidden


class WorkspaceRole(Enum):
    MEMBER = ("member", 1)
    ADMIN = ("admin", 2)
    OWNER = ("owner", 3)

    def __init__(self, value: str, level: int) -> None:
        self._value_ = value
        self.level = level


class PocketAccess(Enum):
    VIEW = ("view", 1)
    COMMENT = ("comment", 2)
    EDIT = ("edit", 3)
    OWNER = ("owner", 4)

    def __init__(self, value: str, level: int) -> None:
        self._value_ = value
        self.level = level


def check_workspace_role(role: str, *, minimum: str) -> None:
    """Raise Forbidden if role is below minimum."""
    try:
        actual = WorkspaceRole(role)
        required = WorkspaceRole(minimum)
    except ValueError:
        raise Forbidden("workspace.invalid_role", f"Unknown role: {role}")
    if actual.level < required.level:
        raise Forbidden("workspace.insufficient_role", f"Requires {minimum}, got {role}")


def check_pocket_access(access: str, *, minimum: str) -> None:
    """Raise Forbidden if access level is below minimum."""
    try:
        actual = PocketAccess(access)
        required = PocketAccess(minimum)
    except ValueError:
        raise Forbidden("pocket.invalid_access", f"Unknown access level: {access}")
    if actual.level < required.level:
        raise Forbidden("pocket.insufficient_access", f"Requires {minimum}, got {access}")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cloud/test_permissions.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add ee/cloud/shared/permissions.py tests/cloud/test_permissions.py
git commit -m "feat(cloud): add role and access level permission checks"
```

---

### Task 4: Create shared/db.py — Clean MongoDB Init

**Files:**
- Create: `ee/cloud/shared/db.py`
- Modify: `ee/cloud/db.py` (keep as re-export for backward compat during migration)

**Step 1: Write the implementation**

```python
# ee/cloud/shared/db.py
"""MongoDB connection and Beanie ODM initialization."""

from __future__ import annotations

import logging

from beanie import init_beanie
from pymongo import AsyncMongoClient

logger = logging.getLogger(__name__)

_client: AsyncMongoClient | None = None


async def init_cloud_db(mongo_uri: str = "mongodb://localhost:27017/paw-cloud") -> None:
    """Initialize Beanie ODM with all cloud document models."""
    global _client

    from ee.cloud.models import ALL_DOCUMENTS

    _client = AsyncMongoClient(mongo_uri)
    db_name = mongo_uri.rsplit("/", 1)[-1].split("?")[0] or "paw-cloud"
    db = _client[db_name]

    await init_beanie(database=db, document_models=ALL_DOCUMENTS)
    logger.info("Cloud DB initialized: %s (%d models)", db_name, len(ALL_DOCUMENTS))


async def close_cloud_db() -> None:
    """Close the MongoDB client."""
    global _client
    if _client:
        _client.close()
        _client = None


def get_client() -> AsyncMongoClient | None:
    """Return the active MongoDB client (for health checks)."""
    return _client
```

**Step 2: Update old db.py to re-export**

```python
# ee/cloud/db.py — backward compat, delegates to shared/db.py
from ee.cloud.shared.db import init_cloud_db, close_cloud_db, get_client  # noqa: F401
```

**Step 3: Commit**

```bash
git add ee/cloud/shared/db.py ee/cloud/db.py
git commit -m "feat(cloud): move db init to shared/db.py with backward compat re-export"
```

---

### Task 5: Create shared/deps.py — FastAPI Dependencies

**Files:**
- Create: `ee/cloud/shared/deps.py`

**Step 1: Write the implementation**

```python
# ee/cloud/shared/deps.py
"""FastAPI dependencies for cloud routers.

Provides:
- current_user: Authenticated User from JWT
- current_user_id: User ID string
- current_workspace_id: Active workspace ID (required)
- optional_workspace_id: Active workspace ID (or None)
- require_role: Dependency factory for workspace role checks
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ee.cloud.auth import current_active_user
from ee.cloud.models.user import User
from ee.cloud.shared.errors import Forbidden
from ee.cloud.shared.permissions import check_workspace_role


async def current_user(user: User = Depends(current_active_user)) -> User:
    """Get the authenticated user from JWT token."""
    return user


async def current_user_id(user: User = Depends(current_active_user)) -> str:
    """Extract user ID string from JWT token."""
    return str(user.id)


async def current_workspace_id(user: User = Depends(current_active_user)) -> str:
    """Extract active workspace ID. Raises 400 if not set."""
    if not user.active_workspace:
        raise HTTPException(400, "No active workspace. Create or join a workspace first.")
    return user.active_workspace


async def optional_workspace_id(user: User = Depends(current_active_user)) -> str | None:
    """Extract workspace ID if set, or None."""
    return user.active_workspace


def require_role(minimum: str):
    """Dependency factory: check user has minimum workspace role.

    Usage: router.get("/admin", dependencies=[Depends(require_role("admin"))])
    """

    async def _check(
        user: User = Depends(current_active_user),
        workspace_id: str = Depends(current_workspace_id),
    ) -> User:
        membership = next(
            (w for w in user.workspaces if w.workspace == workspace_id),
            None,
        )
        if not membership:
            raise Forbidden("workspace.not_member", "Not a member of this workspace")
        check_workspace_role(membership.role, minimum=minimum)
        return user

    return _check
```

**Step 2: Commit**

```bash
git add ee/cloud/shared/deps.py
git commit -m "feat(cloud): add shared FastAPI dependencies with role checking"
```

---

### Task 6: Update Models — Merge Room into Group, Add Fields per Design

**Files:**
- Modify: `ee/cloud/models/group.py`
- Modify: `ee/cloud/models/message.py`
- Modify: `ee/cloud/models/pocket.py`
- Modify: `ee/cloud/models/session.py`
- Modify: `ee/cloud/models/invite.py`
- Modify: `ee/cloud/models/notification.py`
- Modify: `ee/cloud/models/workspace.py`
- Modify: `ee/cloud/models/__init__.py`
- Create: `tests/cloud/test_models.py`

**Step 1: Write the failing test**

```python
# tests/cloud/test_models.py
"""Tests for cloud model changes — pure Pydantic validation, no DB needed."""

from __future__ import annotations

import pytest
from ee.cloud.models.group import Group
from ee.cloud.models.message import Message
from ee.cloud.models.pocket import Pocket
from ee.cloud.models.invite import Invite
from ee.cloud.models.workspace import Workspace


def test_group_supports_dm_type():
    g = Group(workspace="w1", name="DM", type="dm", owner="u1", members=["u1", "u2"])
    assert g.type == "dm"


def test_group_has_last_message_at():
    g = Group(workspace="w1", name="test", owner="u1")
    assert g.last_message_at is None  # None until first message


def test_group_has_message_count():
    g = Group(workspace="w1", name="test", owner="u1")
    assert g.message_count == 0


def test_message_has_edited_at():
    m = Message(group="g1", sender="u1", content="hello")
    assert m.edited_at is None


def test_pocket_sharing_fields():
    p = Pocket(workspace="w1", name="test", owner="u1")
    assert p.share_link_token is None
    assert p.share_link_access == "view"
    assert p.visibility == "private"
    assert p.shared_with == []


def test_pocket_visibility_values():
    for v in ("private", "workspace", "public"):
        p = Pocket(workspace="w1", name="test", owner="u1", visibility=v)
        assert p.visibility == v


def test_invite_has_revoked():
    i = Invite(workspace="w1", email="a@b.com", invited_by="u1", token="tok1")
    assert i.revoked is False


def test_workspace_has_deleted_at():
    w = Workspace(name="test", slug="test", owner="u1")
    assert w.deleted_at is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_models.py -v`
Expected: FAIL — models don't have new fields yet

**Step 3: Update the models**

Update `ee/cloud/models/group.py`:
- Add `type` pattern to include `"dm"`: `Field(default="public", pattern="^(public|private|dm)$")`
- Add `last_message_at: datetime | None = None`
- Add `message_count: int = 0`

Update `ee/cloud/models/message.py`:
- Add `edited_at: datetime | None = None`

Update `ee/cloud/models/pocket.py`:
- Add `share_link_token: str | None = None`
- Add `share_link_access: str = Field(default="view", pattern="^(view|comment|edit)$")`
- Add `shared_with: list[str] = Field(default_factory=list)` (user IDs with explicit grants)
- Ensure `visibility` has pattern: `Field(default="private", pattern="^(private|workspace|public)$")`

Update `ee/cloud/models/session.py`:
- Add `deleted_at: datetime | None = None`

Update `ee/cloud/models/invite.py`:
- Add `revoked: bool = False`

Update `ee/cloud/models/notification.py`:
- Add `expires_at: datetime | None = None`

Update `ee/cloud/models/workspace.py`:
- Add `deleted_at: datetime | None = None`

Update `ee/cloud/models/__init__.py`:
- Remove `Room` from `ALL_DOCUMENTS` (Room is merged into Group)

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cloud/test_models.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add ee/cloud/models/ tests/cloud/test_models.py
git commit -m "feat(cloud): update models — merge Room into Group, add sharing/soft-delete fields"
```

---

## Phase 2: Auth Domain

### Task 7: Create auth/ Domain Package

**Files:**
- Create: `ee/cloud/auth/__init__.py`
- Create: `ee/cloud/auth/router.py`
- Create: `ee/cloud/auth/service.py`
- Create: `ee/cloud/auth/schemas.py`
- Rename: `ee/cloud/auth.py` → `ee/cloud/auth/core.py` (the fastapi-users setup)
- Create: `tests/cloud/test_auth_schemas.py`

**Important:** The existing `ee/cloud/auth.py` is imported everywhere (`from ee.cloud.auth import current_active_user`). Converting it to a package (`ee/cloud/auth/__init__.py`) requires re-exporting from the `__init__.py`.

**Step 1: Write the failing test**

```python
# tests/cloud/test_auth_schemas.py
from __future__ import annotations

from ee.cloud.auth.schemas import ProfileUpdateRequest, SetWorkspaceRequest


def test_profile_update_optional_fields():
    body = ProfileUpdateRequest()
    assert body.full_name is None
    assert body.avatar is None
    assert body.status is None


def test_profile_update_with_values():
    body = ProfileUpdateRequest(full_name="Rohit", avatar="https://example.com/img.png")
    assert body.full_name == "Rohit"


def test_set_workspace_request():
    body = SetWorkspaceRequest(workspace_id="ws123")
    assert body.workspace_id == "ws123"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_auth_schemas.py -v`
Expected: FAIL

**Step 3: Implement the auth domain**

```python
# ee/cloud/auth/schemas.py
"""Request/response schemas for auth endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ProfileUpdateRequest(BaseModel):
    full_name: str | None = None
    avatar: str | None = None
    status: str | None = None


class SetWorkspaceRequest(BaseModel):
    workspace_id: str


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    image: str
    email_verified: bool
    active_workspace: str | None
    workspaces: list[dict]

    model_config = {"from_attributes": True}
```

```python
# ee/cloud/auth/service.py
"""Auth service — profile management, workspace switching."""

from __future__ import annotations

from ee.cloud.models.user import User
from ee.cloud.auth.schemas import ProfileUpdateRequest, UserResponse


class AuthService:
    @staticmethod
    async def get_profile(user: User) -> UserResponse:
        return UserResponse(
            id=str(user.id),
            email=user.email,
            name=user.full_name,
            image=user.avatar,
            email_verified=user.is_verified,
            active_workspace=user.active_workspace,
            workspaces=[
                {"workspace": w.workspace, "role": w.role}
                for w in user.workspaces
            ],
        )

    @staticmethod
    async def update_profile(user: User, body: ProfileUpdateRequest) -> UserResponse:
        if body.full_name is not None:
            user.full_name = body.full_name
        if body.avatar is not None:
            user.avatar = body.avatar
        if body.status is not None:
            user.status = body.status
        await user.save()
        return await AuthService.get_profile(user)

    @staticmethod
    async def set_active_workspace(user: User, workspace_id: str) -> None:
        user.active_workspace = workspace_id
        await user.save()
```

```python
# ee/cloud/auth/core.py
# This is the EXISTING ee/cloud/auth.py content — moved here unchanged.
# Contains: fastapi_users setup, JWT strategy, cookie/bearer backends,
# current_active_user, seed_admin, UserRead, UserCreate
```

```python
# ee/cloud/auth/router.py
"""Auth router — login, register, profile, workspace switching."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ee.cloud.auth.core import (
    cookie_backend,
    bearer_backend,
    fastapi_users,
    current_active_user,
    UserRead,
    UserCreate,
)
from ee.cloud.auth.schemas import ProfileUpdateRequest, SetWorkspaceRequest
from ee.cloud.auth.service import AuthService
from ee.cloud.models.user import User

router = APIRouter(tags=["Auth"])

# fastapi-users auth routes
router.include_router(fastapi_users.get_auth_router(cookie_backend), prefix="/auth")
router.include_router(fastapi_users.get_auth_router(bearer_backend), prefix="/auth/bearer")
router.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth")


@router.get("/auth/me")
async def get_me(user: User = Depends(current_active_user)):
    return await AuthService.get_profile(user)


@router.patch("/auth/me")
async def update_me(body: ProfileUpdateRequest, user: User = Depends(current_active_user)):
    return await AuthService.update_profile(user, body)


@router.post("/auth/set-active-workspace")
async def set_active_workspace(body: SetWorkspaceRequest, user: User = Depends(current_active_user)):
    await AuthService.set_active_workspace(user, body.workspace_id)
    return {"ok": True, "activeWorkspace": body.workspace_id}
```

```python
# ee/cloud/auth/__init__.py
"""Auth domain — re-exports for backward compatibility.

Other modules import: from ee.cloud.auth import current_active_user
This must keep working after the package conversion.
"""

from ee.cloud.auth.core import (  # noqa: F401
    current_active_user,
    current_optional_user,
    fastapi_users,
    get_jwt_strategy,
    get_user_manager,
    get_user_db,
    cookie_backend,
    bearer_backend,
    UserRead,
    UserCreate,
    UserManager,
    seed_admin,
    SECRET,
    TOKEN_LIFETIME,
)
from ee.cloud.auth.router import router  # noqa: F401
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cloud/test_auth_schemas.py -v`
Expected: ALL PASS

**Step 5: Verify existing imports still work**

Run: `uv run python -c "from ee.cloud.auth import current_active_user, router; print('OK')"`
Expected: OK

**Step 6: Commit**

```bash
git add ee/cloud/auth/ tests/cloud/test_auth_schemas.py
git rm ee/cloud/auth.py  # now a package
git commit -m "feat(cloud): restructure auth as domain package with service layer and schemas"
```

---

## Phase 3: Workspace Domain

### Task 8: Create workspace/ Domain Package

**Files:**
- Create: `ee/cloud/workspace/__init__.py`
- Create: `ee/cloud/workspace/schemas.py`
- Create: `ee/cloud/workspace/service.py`
- Create: `ee/cloud/workspace/router.py`
- Create: `tests/cloud/test_workspace_schemas.py`

**Step 1: Write the failing test**

```python
# tests/cloud/test_workspace_schemas.py
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError
from ee.cloud.workspace.schemas import (
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
    CreateInviteRequest,
    WorkspaceResponse,
    MemberResponse,
)


def test_create_workspace_required_fields():
    req = CreateWorkspaceRequest(name="Acme Corp", slug="acme-corp")
    assert req.name == "Acme Corp"
    assert req.slug == "acme-corp"


def test_create_workspace_slug_validation():
    # slugs must be lowercase alphanumeric + hyphens
    with pytest.raises(PydanticValidationError):
        CreateWorkspaceRequest(name="Test", slug="Invalid Slug!")


def test_update_workspace_all_optional():
    req = UpdateWorkspaceRequest()
    assert req.name is None


def test_create_invite_required_fields():
    req = CreateInviteRequest(email="test@example.com")
    assert req.role == "member"  # default


def test_create_invite_role_validation():
    with pytest.raises(PydanticValidationError):
        CreateInviteRequest(email="test@example.com", role="superadmin")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_workspace_schemas.py -v`
Expected: FAIL

**Step 3: Implement workspace domain**

```python
# ee/cloud/workspace/__init__.py
from ee.cloud.workspace.router import router  # noqa: F401

# ee/cloud/workspace/schemas.py
"""Request/response schemas for workspace endpoints."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError("Slug must be lowercase alphanumeric with hyphens, no leading/trailing hyphens")
        return v


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    settings: dict | None = None


class CreateInviteRequest(BaseModel):
    email: str
    role: str = Field(default="member", pattern="^(admin|member)$")
    group_id: str | None = None


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    slug: str
    owner: str
    plan: str
    seats: int
    created_at: datetime
    member_count: int = 0

    model_config = {"from_attributes": True}


class MemberResponse(BaseModel):
    id: str
    email: str
    name: str
    avatar: str
    role: str
    joined_at: datetime

    model_config = {"from_attributes": True}


class InviteResponse(BaseModel):
    id: str
    email: str
    role: str
    invited_by: str
    token: str
    accepted: bool
    revoked: bool
    expired: bool
    expires_at: datetime

    model_config = {"from_attributes": True}
```

```python
# ee/cloud/workspace/service.py
"""Workspace business logic — CRUD, members, invites."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud.models.invite import Invite
from ee.cloud.models.user import User, WorkspaceMembership
from ee.cloud.models.workspace import Workspace, WorkspaceSettings
from ee.cloud.shared.errors import ConflictError, Forbidden, NotFound, SeatLimitError
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.permissions import check_workspace_role
from ee.cloud.workspace.schemas import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    InviteResponse,
    MemberResponse,
    UpdateWorkspaceRequest,
    WorkspaceResponse,
)

logger = logging.getLogger(__name__)


class WorkspaceService:
    # ---- Workspace CRUD ----

    @staticmethod
    async def create(user: User, body: CreateWorkspaceRequest) -> WorkspaceResponse:
        existing = await Workspace.find_one(Workspace.slug == body.slug)
        if existing:
            raise ConflictError("workspace.slug_taken", f"Slug '{body.slug}' is already in use")

        ws = Workspace(
            name=body.name,
            slug=body.slug,
            owner=str(user.id),
            settings=WorkspaceSettings(),
        )
        await ws.insert()

        # Add creator as owner member
        user.workspaces.append(
            WorkspaceMembership(workspace=str(ws.id), role="owner")
        )
        user.active_workspace = str(ws.id)
        await user.save()

        return WorkspaceResponse(
            id=str(ws.id), name=ws.name, slug=ws.slug,
            owner=ws.owner, plan=ws.plan, seats=ws.seats,
            created_at=ws.createdAt, member_count=1,
        )

    @staticmethod
    async def get(workspace_id: str, user: User) -> WorkspaceResponse:
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at:
            raise NotFound("workspace", workspace_id)

        _require_membership(user, workspace_id)

        member_count = sum(
            1 for u_cursor in [None]  # placeholder — counted via aggregation below
        )
        # Count members via User collection
        member_count = await User.find(
            {"workspaces.workspace": workspace_id}
        ).count()

        return WorkspaceResponse(
            id=str(ws.id), name=ws.name, slug=ws.slug,
            owner=ws.owner, plan=ws.plan, seats=ws.seats,
            created_at=ws.createdAt, member_count=member_count,
        )

    @staticmethod
    async def update(workspace_id: str, user: User, body: UpdateWorkspaceRequest) -> WorkspaceResponse:
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at:
            raise NotFound("workspace", workspace_id)

        _require_role(user, workspace_id, "admin")

        if body.name is not None:
            ws.name = body.name
        if body.settings is not None:
            ws.settings = WorkspaceSettings(**body.settings)
        await ws.save()

        return await WorkspaceService.get(workspace_id, user)

    @staticmethod
    async def delete(workspace_id: str, user: User) -> None:
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at:
            raise NotFound("workspace", workspace_id)

        _require_role(user, workspace_id, "owner")
        ws.deleted_at = datetime.now(UTC)
        await ws.save()

    @staticmethod
    async def list_for_user(user: User) -> list[WorkspaceResponse]:
        results = []
        for membership in user.workspaces:
            ws = await Workspace.get(PydanticObjectId(membership.workspace))
            if ws and not ws.deleted_at:
                member_count = await User.find(
                    {"workspaces.workspace": membership.workspace}
                ).count()
                results.append(WorkspaceResponse(
                    id=str(ws.id), name=ws.name, slug=ws.slug,
                    owner=ws.owner, plan=ws.plan, seats=ws.seats,
                    created_at=ws.createdAt, member_count=member_count,
                ))
        return results

    # ---- Members ----

    @staticmethod
    async def list_members(workspace_id: str, user: User) -> list[MemberResponse]:
        _require_membership(user, workspace_id)

        members = await User.find({"workspaces.workspace": workspace_id}).to_list()
        results = []
        for m in members:
            membership = next((w for w in m.workspaces if w.workspace == workspace_id), None)
            if membership:
                results.append(MemberResponse(
                    id=str(m.id), email=m.email, name=m.full_name,
                    avatar=m.avatar, role=membership.role,
                    joined_at=membership.joined_at,
                ))
        return results

    @staticmethod
    async def update_member_role(workspace_id: str, target_user_id: str, role: str, user: User) -> None:
        _require_role(user, workspace_id, "admin")

        target = await User.get(PydanticObjectId(target_user_id))
        if not target:
            raise NotFound("user", target_user_id)

        membership = next((w for w in target.workspaces if w.workspace == workspace_id), None)
        if not membership:
            raise NotFound("member", target_user_id)

        # Can't demote workspace owner
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if ws and ws.owner == target_user_id and role != "owner":
            raise Forbidden("workspace.cannot_demote_owner", "Cannot change the workspace owner's role")

        membership.role = role
        await target.save()

    @staticmethod
    async def remove_member(workspace_id: str, target_user_id: str, user: User) -> None:
        _require_role(user, workspace_id, "admin")

        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if ws and ws.owner == target_user_id:
            raise Forbidden("workspace.cannot_remove_owner", "Cannot remove the workspace owner")

        target = await User.get(PydanticObjectId(target_user_id))
        if not target:
            raise NotFound("user", target_user_id)

        target.workspaces = [w for w in target.workspaces if w.workspace != workspace_id]
        if target.active_workspace == workspace_id:
            target.active_workspace = None
        await target.save()

        await event_bus.emit("member.removed", {
            "workspace_id": workspace_id,
            "user_id": target_user_id,
        })

    # ---- Invites ----

    @staticmethod
    async def create_invite(workspace_id: str, user: User, body: CreateInviteRequest) -> InviteResponse:
        _require_role(user, workspace_id, "admin")

        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at:
            raise NotFound("workspace", workspace_id)

        # Check seat limit
        member_count = await User.find({"workspaces.workspace": workspace_id}).count()
        if member_count >= ws.seats:
            raise SeatLimitError(ws.seats)

        # Check for existing pending invite
        existing = await Invite.find_one(
            Invite.workspace == workspace_id,
            Invite.email == body.email,
            Invite.accepted == False,
            Invite.revoked == False,
        )
        if existing and not existing.expired:
            raise ConflictError("invite.already_pending", f"Invite already pending for {body.email}")

        invite = Invite(
            workspace=workspace_id,
            email=body.email,
            role=body.role,
            invited_by=str(user.id),
            token=secrets.token_urlsafe(32),
            group=body.group_id,
        )
        await invite.insert()

        return _invite_to_response(invite)

    @staticmethod
    async def validate_invite(token: str) -> InviteResponse:
        invite = await Invite.find_one(Invite.token == token)
        if not invite:
            raise NotFound("invite")
        return _invite_to_response(invite)

    @staticmethod
    async def accept_invite(token: str, user: User) -> None:
        invite = await Invite.find_one(Invite.token == token)
        if not invite:
            raise NotFound("invite")
        if invite.accepted:
            raise ConflictError("invite.already_accepted", "Invite already accepted")
        if invite.revoked:
            raise Forbidden("invite.revoked", "Invite has been revoked")
        if invite.expired:
            raise Forbidden("invite.expired", "Invite has expired")

        # Check seat limit
        ws = await Workspace.get(PydanticObjectId(invite.workspace))
        if not ws or ws.deleted_at:
            raise NotFound("workspace", invite.workspace)
        member_count = await User.find({"workspaces.workspace": invite.workspace}).count()
        if member_count >= ws.seats:
            raise SeatLimitError(ws.seats)

        # Add user to workspace
        already_member = any(w.workspace == invite.workspace for w in user.workspaces)
        if not already_member:
            user.workspaces.append(
                WorkspaceMembership(workspace=invite.workspace, role=invite.role)
            )
            user.active_workspace = invite.workspace
            await user.save()

        invite.accepted = True
        await invite.save()

        await event_bus.emit("invite.accepted", {
            "workspace_id": invite.workspace,
            "user_id": str(user.id),
            "group_id": invite.group,
        })

    @staticmethod
    async def revoke_invite(workspace_id: str, invite_id: str, user: User) -> None:
        _require_role(user, workspace_id, "admin")

        invite = await Invite.get(PydanticObjectId(invite_id))
        if not invite or invite.workspace != workspace_id:
            raise NotFound("invite", invite_id)

        invite.revoked = True
        await invite.save()


# ---- Helpers ----

def _require_membership(user: User, workspace_id: str) -> WorkspaceMembership:
    membership = next((w for w in user.workspaces if w.workspace == workspace_id), None)
    if not membership:
        raise Forbidden("workspace.not_member", "Not a member of this workspace")
    return membership


def _require_role(user: User, workspace_id: str, minimum: str) -> None:
    membership = _require_membership(user, workspace_id)
    check_workspace_role(membership.role, minimum=minimum)


def _invite_to_response(invite: Invite) -> InviteResponse:
    return InviteResponse(
        id=str(invite.id), email=invite.email, role=invite.role,
        invited_by=invite.invited_by, token=invite.token,
        accepted=invite.accepted, revoked=invite.revoked,
        expired=invite.expired, expires_at=invite.expires_at,
    )
```

```python
# ee/cloud/workspace/router.py
"""Workspace router — CRUD, members, invites."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ee.cloud.license import require_license
from ee.cloud.models.user import User
from ee.cloud.shared.deps import current_user, current_workspace_id
from ee.cloud.workspace.schemas import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)
from ee.cloud.workspace.service import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["Workspace"], dependencies=[Depends(require_license)])


@router.post("")
async def create_workspace(body: CreateWorkspaceRequest, user: User = Depends(current_user)):
    return await WorkspaceService.create(user, body)


@router.get("")
async def list_workspaces(user: User = Depends(current_user)):
    return await WorkspaceService.list_for_user(user)


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str, user: User = Depends(current_user)):
    return await WorkspaceService.get(workspace_id, user)


@router.patch("/{workspace_id}")
async def update_workspace(workspace_id: str, body: UpdateWorkspaceRequest, user: User = Depends(current_user)):
    return await WorkspaceService.update(workspace_id, user, body)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: str, user: User = Depends(current_user)):
    await WorkspaceService.delete(workspace_id, user)


# ---- Members ----

@router.get("/{workspace_id}/members")
async def list_members(workspace_id: str, user: User = Depends(current_user)):
    return await WorkspaceService.list_members(workspace_id, user)


@router.patch("/{workspace_id}/members/{user_id}")
async def update_member_role(workspace_id: str, user_id: str, body: dict, user: User = Depends(current_user)):
    await WorkspaceService.update_member_role(workspace_id, user_id, body["role"], user)
    return {"ok": True}


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(workspace_id: str, user_id: str, user: User = Depends(current_user)):
    await WorkspaceService.remove_member(workspace_id, user_id, user)


# ---- Invites ----

@router.post("/{workspace_id}/invites")
async def create_invite(workspace_id: str, body: CreateInviteRequest, user: User = Depends(current_user)):
    return await WorkspaceService.create_invite(workspace_id, user, body)


@router.get("/invites/{token}")
async def validate_invite(token: str):
    return await WorkspaceService.validate_invite(token)


@router.post("/invites/{token}/accept")
async def accept_invite(token: str, user: User = Depends(current_user)):
    await WorkspaceService.accept_invite(token, user)
    return {"ok": True}


@router.delete("/{workspace_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(workspace_id: str, invite_id: str, user: User = Depends(current_user)):
    await WorkspaceService.revoke_invite(workspace_id, invite_id, user)
```

**Step 4: Run tests**

Run: `uv run pytest tests/cloud/test_workspace_schemas.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add ee/cloud/workspace/ tests/cloud/test_workspace_schemas.py
git commit -m "feat(cloud): add workspace domain — CRUD, members, invites with service layer"
```

---

## Phase 4: Agents Domain

### Task 9: Create agents/ Domain Package

**Files:**
- Create: `ee/cloud/agents/__init__.py`
- Create: `ee/cloud/agents/schemas.py`
- Create: `ee/cloud/agents/service.py`
- Create: `ee/cloud/agents/router.py`
- Create: `tests/cloud/test_agent_schemas.py`

**Step 1: Write the failing test**

```python
# tests/cloud/test_agent_schemas.py
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError
from ee.cloud.agents.schemas import CreateAgentRequest, UpdateAgentRequest


def test_create_agent_required_fields():
    req = CreateAgentRequest(name="My Agent", slug="my-agent")
    assert req.name == "My Agent"
    assert req.config is None  # optional


def test_create_agent_with_config():
    req = CreateAgentRequest(
        name="My Agent",
        slug="my-agent",
        config={"backend": "claude_agent_sdk", "model": "claude-sonnet-4-5-20250514"},
    )
    assert req.config["backend"] == "claude_agent_sdk"


def test_update_agent_all_optional():
    req = UpdateAgentRequest()
    assert req.name is None
    assert req.config is None
    assert req.visibility is None
```

**Step 2: Run test, verify fails, implement, verify passes**

Implement `agents/schemas.py`, `agents/service.py`, `agents/router.py` following the same pattern as workspace: thin router → service → model.

Service methods: `create`, `list_agents`, `get`, `get_by_slug`, `update`, `delete`, `discover` (paginated search with visibility filter).

Router endpoints as per design doc at `/agents`.

**Step 3: Commit**

```bash
git add ee/cloud/agents/ tests/cloud/test_agent_schemas.py
git commit -m "feat(cloud): add agents domain — CRUD, discovery, visibility filtering"
```

---

## Phase 5: Chat Domain (largest piece)

### Task 10: Create chat/schemas.py — Message & Group Schemas

**Files:**
- Create: `ee/cloud/chat/__init__.py`
- Create: `ee/cloud/chat/schemas.py`
- Create: `tests/cloud/test_chat_schemas.py`

**Step 1: Write the failing test**

```python
# tests/cloud/test_chat_schemas.py
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError
from ee.cloud.chat.schemas import (
    CreateGroupRequest,
    SendMessageRequest,
    EditMessageRequest,
    ReactRequest,
    WsInbound,
    WsOutbound,
)


def test_create_group_defaults():
    req = CreateGroupRequest(name="general")
    assert req.type == "public"
    assert req.description == ""


def test_create_group_dm():
    req = CreateGroupRequest(name="DM", type="dm", member_ids=["u1", "u2"])
    assert req.type == "dm"
    assert len(req.member_ids) == 2


def test_send_message_content_required():
    req = SendMessageRequest(content="hello")
    assert req.content == "hello"
    assert req.reply_to is None
    assert req.mentions == []


def test_send_message_max_length():
    with pytest.raises(PydanticValidationError):
        SendMessageRequest(content="x" * 10_001)


def test_edit_message():
    req = EditMessageRequest(content="updated")
    assert req.content == "updated"


def test_react_request():
    req = ReactRequest(emoji="thumbsup")
    assert req.emoji == "thumbsup"


def test_ws_inbound_message_send():
    msg = WsInbound.model_validate({
        "type": "message.send",
        "group_id": "g1",
        "content": "hello",
    })
    assert msg.type == "message.send"


def test_ws_inbound_typing():
    msg = WsInbound.model_validate({
        "type": "typing.start",
        "group_id": "g1",
    })
    assert msg.type == "typing.start"


def test_ws_inbound_invalid_type():
    with pytest.raises(PydanticValidationError):
        WsInbound.model_validate({"type": "invalid.type"})
```

**Step 2: Run test, verify fails**

Run: `uv run pytest tests/cloud/test_chat_schemas.py -v`

**Step 3: Implement schemas**

```python
# ee/cloud/chat/schemas.py
"""Request/response and WebSocket message schemas for chat."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---- REST Schemas ----

class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    type: Literal["public", "private", "dm"] = "public"
    member_ids: list[str] = Field(default_factory=list)
    icon: str = ""
    color: str = ""


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None


class AddGroupMembersRequest(BaseModel):
    user_ids: list[str]


class AddGroupAgentRequest(BaseModel):
    agent_id: str
    role: str = "assistant"
    respond_mode: str = "mention_only"


class UpdateGroupAgentRequest(BaseModel):
    respond_mode: str


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    reply_to: str | None = None
    mentions: list[dict] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list)


class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)


class ReactRequest(BaseModel):
    emoji: str = Field(min_length=1, max_length=50)


class MessageResponse(BaseModel):
    id: str
    group: str
    sender: str | None
    sender_type: str
    sender_name: str = ""
    content: str
    mentions: list[dict]
    reply_to: str | None
    attachments: list[dict]
    reactions: list[dict]
    edited: bool
    edited_at: datetime | None
    deleted: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class GroupResponse(BaseModel):
    id: str
    workspace: str
    name: str
    slug: str
    description: str
    type: str
    icon: str
    color: str
    owner: str
    members: list[Any]  # User IDs or populated objects
    agents: list[Any]
    pinned_messages: list[str]
    archived: bool
    last_message_at: datetime | None
    message_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- WebSocket Schemas ----

class WsInbound(BaseModel):
    """Validated inbound WebSocket message from client."""

    type: Literal[
        "message.send", "message.edit", "message.delete", "message.react",
        "typing.start", "typing.stop",
        "presence.update",
        "read.ack",
    ]
    group_id: str | None = None
    message_id: str | None = None
    content: str | None = None
    reply_to: str | None = None
    mentions: list[dict] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list)
    emoji: str | None = None
    status: str | None = None


class WsOutbound(BaseModel):
    """Outbound WebSocket message to client."""

    type: str
    data: dict = Field(default_factory=dict)
```

**Step 4: Run test, verify passes, commit**

```bash
git add ee/cloud/chat/ tests/cloud/test_chat_schemas.py
git commit -m "feat(cloud): add chat schemas with WebSocket message validation"
```

---

### Task 11: Create chat/service.py — Group & Message Business Logic

**Files:**
- Create: `ee/cloud/chat/service.py`
- Create: `tests/cloud/test_chat_service.py` (schema-level tests only — DB tests in integration)

Service methods for groups:
- `create_group`, `list_groups`, `get_group`, `update_group`, `archive_group`
- `join_group`, `leave_group`, `add_members`, `remove_member`
- `add_agent`, `update_agent`, `remove_agent`
- `get_or_create_dm` — find existing DM between two users, or create one

Service methods for messages:
- `send_message` — persist + emit event for WebSocket broadcast
- `edit_message` — author only, sets `edited=True`, `edited_at=now`
- `delete_message` — soft-delete (author or group admin)
- `toggle_reaction` — add if not present, remove if already reacted
- `get_messages` — cursor-paginated using `(created_at, _id)`
- `get_thread` — messages where `reply_to == parent_id`
- `pin_message`, `unpin_message`
- `search_messages` — text search within group

All mutations emit events via `event_bus` for WebSocket broadcast and notification creation.

**Commit:**

```bash
git add ee/cloud/chat/service.py tests/cloud/test_chat_service.py
git commit -m "feat(cloud): add chat service — groups, messages, reactions, threading"
```

---

### Task 12: Create chat/ws.py — WebSocket Connection Manager

**Files:**
- Create: `ee/cloud/chat/ws.py`
- Create: `tests/cloud/test_ws.py`

**Step 1: Write the failing test**

```python
# tests/cloud/test_ws.py
from __future__ import annotations

import pytest
from ee.cloud.chat.ws import ConnectionManager


def test_connection_manager_init():
    cm = ConnectionManager()
    assert cm.active_connections == {}


def test_connection_manager_tracking():
    cm = ConnectionManager()
    assert cm.get_user_connections("u1") == set()
```

**Step 2: Implement**

```python
# ee/cloud/chat/ws.py
"""WebSocket connection manager for real-time chat.

Handles:
- Connection lifecycle (connect, authenticate, disconnect)
- User-to-connections mapping (multi-tab/device support)
- Message routing to group members
- Typing indicators with auto-expiry
- Presence tracking with grace period
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from fastapi import WebSocket, WebSocketDisconnect

from ee.cloud.chat.schemas import WsInbound, WsOutbound

logger = logging.getLogger(__name__)

TYPING_TIMEOUT_SECONDS = 5
PRESENCE_GRACE_SECONDS = 30


class ConnectionManager:
    """Manages WebSocket connections, maps users to their active sockets."""

    def __init__(self) -> None:
        # user_id → set of WebSocket connections
        self.active_connections: dict[str, set[WebSocket]] = {}
        # ws → user_id (reverse lookup)
        self._ws_to_user: dict[WebSocket, str] = {}
        # user_id → set of group_ids they've subscribed to
        self._user_groups: dict[str, set[str]] = {}
        # group_id → set of user_ids currently typing
        self._typing: dict[str, dict[str, asyncio.Task]] = {}
        # Pending offline tasks (grace period)
        self._offline_tasks: dict[str, asyncio.Task] = {}

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        """Register an authenticated connection."""
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = set()
        self.active_connections[user_id].add(websocket)
        self._ws_to_user[websocket] = user_id

        # Cancel any pending offline task
        if user_id in self._offline_tasks:
            self._offline_tasks.pop(user_id).cancel()

        logger.info("WS connected: user=%s (total=%d)", user_id, len(self.active_connections[user_id]))

    async def disconnect(self, websocket: WebSocket) -> str | None:
        """Remove a connection. Returns user_id if this was their last connection."""
        user_id = self._ws_to_user.pop(websocket, None)
        if not user_id:
            return None

        conns = self.active_connections.get(user_id, set())
        conns.discard(websocket)

        if not conns:
            # Last connection — start grace period before marking offline
            del self.active_connections[user_id]
            return user_id

        return None

    def get_user_connections(self, user_id: str) -> set[WebSocket]:
        """Get all active WebSocket connections for a user."""
        return self.active_connections.get(user_id, set())

    def is_online(self, user_id: str) -> bool:
        return user_id in self.active_connections and len(self.active_connections[user_id]) > 0

    async def send_to_user(self, user_id: str, message: WsOutbound) -> None:
        """Send a message to all of a user's connections."""
        data = message.model_dump(mode="json")
        for ws in self.get_user_connections(user_id):
            try:
                await ws.send_json(data)
            except Exception:
                logger.debug("Failed to send to user=%s", user_id)

    async def broadcast_to_group(
        self,
        group_id: str,
        member_ids: list[str],
        message: WsOutbound,
        exclude_user: str | None = None,
    ) -> None:
        """Broadcast a message to all online members of a group."""
        data = message.model_dump(mode="json")
        for uid in member_ids:
            if uid == exclude_user:
                continue
            for ws in self.get_user_connections(uid):
                try:
                    await ws.send_json(data)
                except Exception:
                    logger.debug("Failed to broadcast to user=%s in group=%s", uid, group_id)


# Module-level singleton
manager = ConnectionManager()
```

**Step 3: Commit**

```bash
git add ee/cloud/chat/ws.py tests/cloud/test_ws.py
git commit -m "feat(cloud): add WebSocket connection manager with multi-device support"
```

---

### Task 13: Create chat/router.py — Chat REST + WebSocket Endpoints

**Files:**
- Create: `ee/cloud/chat/router.py`

Router wires up all REST chat endpoints from the design doc:
- Groups CRUD, membership, agents
- Messages CRUD, reactions, threading, search, pins
- DM creation
- WebSocket endpoint at `/ws/cloud`

The WebSocket endpoint:
1. Extracts JWT token from query param
2. Validates and gets user
3. Registers with ConnectionManager
4. Loops reading JSON messages, validates via `WsInbound`
5. Dispatches to service methods
6. On disconnect, cleans up

**Commit:**

```bash
git add ee/cloud/chat/router.py
git commit -m "feat(cloud): add chat router — REST endpoints + WebSocket handler"
```

---

## Phase 6: Pockets Domain

### Task 14: Create pockets/ Domain Package

**Files:**
- Create: `ee/cloud/pockets/__init__.py`
- Create: `ee/cloud/pockets/schemas.py`
- Create: `ee/cloud/pockets/service.py`
- Create: `ee/cloud/pockets/router.py`
- Create: `tests/cloud/test_pocket_schemas.py`

**Key implementation details:**

Schemas include:
- `CreatePocketRequest` — name, type, icon, color, visibility, session_id (optional auto-link)
- `UpdatePocketRequest` — all optional fields
- `ShareLinkRequest` — access level (view/comment/edit)
- `PocketResponse` — full pocket with sharing info

Service includes:
- `create` — if `session_id` provided, auto-links session to pocket
- `update` — with ripple spec normalization
- `share` — generates `share_link_token` via `secrets.token_urlsafe(32)`
- `revoke_share` — nulls out token
- `access_via_share_link` — validates token, returns pocket with access level
- `add_collaborator` / `remove_collaborator` — manages `shared_with` list
- Widget management: `add_widget`, `update_widget`, `remove_widget`, `reorder_widgets`
- Sessions under pocket: delegates to sessions service

Router mounts at `/pockets` with all endpoints from design doc. The `/shared/{token}` endpoint does NOT require auth for public pockets.

**Commit:**

```bash
git add ee/cloud/pockets/ tests/cloud/test_pocket_schemas.py
git commit -m "feat(cloud): add pockets domain — CRUD, sharing via links, widgets, collaborators"
```

---

## Phase 7: Sessions Domain

### Task 15: Create sessions/ Domain Package

**Files:**
- Create: `ee/cloud/sessions/__init__.py`
- Create: `ee/cloud/sessions/schemas.py`
- Create: `ee/cloud/sessions/service.py`
- Create: `ee/cloud/sessions/router.py`
- Create: `tests/cloud/test_session_schemas.py`

**Key implementation details:**

Service includes:
- `create` — generates `sessionId`, if `pocket_id` provided, links session
- `update` — can change title, link/unlink pocket
- `delete` — soft-delete via `deleted_at`
- `list_for_user` — excludes soft-deleted, sorted by `lastActivity`
- `list_for_pocket` — sessions where `pocket == pocket_id`
- `get_history` — proxy to Python runtime at `RUNTIME_URL`
- `touch` — increment `messageCount`, update `lastActivity`
- Auto-link: when a pocket is created with `session_id`, the session service sets `session.pocket = pocket_id`

**Commit:**

```bash
git add ee/cloud/sessions/ tests/cloud/test_session_schemas.py
git commit -m "feat(cloud): add sessions domain — CRUD, pocket auto-linking, runtime proxy"
```

---

## Phase 8: Integration & Wiring

### Task 16: Create ee/cloud/__init__.py — Mount All Domain Routers

**Files:**
- Modify: `ee/cloud/__init__.py`

**Implementation:**

```python
# ee/cloud/__init__.py
"""PocketPaw Enterprise Cloud — domain-driven architecture.

Domains: auth, workspace, chat, pockets, sessions, agents.
Each domain has router.py (thin), service.py (logic), schemas.py (validation).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ee.cloud.shared.errors import CloudError


def mount_cloud(app: FastAPI) -> None:
    """Mount all cloud domain routers and the error handler on the app."""

    # Global error handler for CloudError
    @app.exception_handler(CloudError)
    async def cloud_error_handler(request: Request, exc: CloudError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    # Import and mount domain routers
    from ee.cloud.auth.router import router as auth_router
    from ee.cloud.workspace.router import router as workspace_router
    from ee.cloud.agents.router import router as agents_router
    from ee.cloud.chat.router import router as chat_router
    from ee.cloud.pockets.router import router as pockets_router
    from ee.cloud.sessions.router import router as sessions_router
    from ee.cloud.license import get_license_info

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(workspace_router, prefix="/api/v1")
    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(pockets_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")

    # License endpoint (no auth required)
    @app.get("/api/v1/license")
    async def license_info():
        return get_license_info()
```

**Commit:**

```bash
git add ee/cloud/__init__.py
git commit -m "feat(cloud): add mount_cloud() to wire all domain routers with error handler"
```

---

### Task 17: Update serve.py — Replace Old Cloud Mounting

**Files:**
- Modify: `src/pocketpaw/api/v1/__init__.py` — remove old `_CLOUD_ROUTERS` list
- Modify: `src/pocketpaw/api/serve.py` — replace Socket.IO wrap with `mount_cloud()`

**Changes to `v1/__init__.py`:**
- Remove the `_CLOUD_ROUTERS` list (lines 62-70)
- Remove the cloud router mounting loop (lines 87-96)

**Changes to `serve.py`:**
- Replace lines 131-137 (Socket.IO wrap) with:
  ```python
  try:
      from ee.cloud import mount_cloud
      mount_cloud(app)
  except ImportError:
      pass
  ```

**Commit:**

```bash
git add src/pocketpaw/api/v1/__init__.py src/pocketpaw/api/serve.py
git commit -m "feat(cloud): wire new domain architecture into serve.py, remove Socket.IO wrapping"
```

---

### Task 18: Delete Old Router Files

**Files to delete:**
- `ee/cloud/agents_router.py`
- `ee/cloud/groups_router.py`
- `ee/cloud/pockets_router.py`
- `ee/cloud/sessions_router.py`
- `ee/cloud/workspace_router.py`
- `ee/cloud/license_router.py`
- `ee/cloud/socketio_server.py`
- `ee/cloud/deps.py` (replaced by `shared/deps.py`)
- `ee/cloud/models/room.py` (merged into Group)

**Important:** Keep `ee/cloud/db.py` (re-exports from `shared/db.py`) for any external imports.
Keep `ee/cloud/license.py` (used by all domains).
Keep `ee/cloud/ripple_normalizer.py` (used by pockets service).

**Commit:**

```bash
git rm ee/cloud/agents_router.py ee/cloud/groups_router.py ee/cloud/pockets_router.py \
       ee/cloud/sessions_router.py ee/cloud/workspace_router.py ee/cloud/license_router.py \
       ee/cloud/socketio_server.py ee/cloud/deps.py ee/cloud/models/room.py
git commit -m "chore(cloud): remove old flat routers, Socket.IO server, and Room model"
```

---

### Task 19: Register Event Handlers for Cross-Domain Side Effects

**Files:**
- Create: `ee/cloud/shared/event_handlers.py`

**Implementation:**

Wire up the event bus handlers:
- `invite.accepted` → auto-add user to group (if `group_id` in invite), create notification
- `message.sent` → create notifications for mentioned users, update group `last_message_at` and `message_count`
- `pocket.shared` → create notification for recipient
- `member.removed` → remove from groups in workspace, revoke pocket access

Register handlers on app startup (called from `mount_cloud()`).

**Commit:**

```bash
git add ee/cloud/shared/event_handlers.py
git commit -m "feat(cloud): wire cross-domain event handlers for notifications and auto-linking"
```

---

### Task 20: Smoke Test — Full Integration

**Files:**
- Create: `tests/cloud/test_integration.py`

**Step 1: Write integration test**

A test that:
1. Imports `mount_cloud` and creates a test FastAPI app
2. Verifies all routes are mounted (check `app.routes`)
3. Verifies the CloudError handler is registered
4. Verifies the WebSocket endpoint `/ws/cloud` exists

This does NOT require a running MongoDB — it only checks route registration.

```python
# tests/cloud/test_integration.py
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_cloud_routes_mount():
    """Verify all cloud domain routers mount without errors."""
    from ee.cloud import mount_cloud

    app = FastAPI()
    mount_cloud(app)

    routes = [r.path for r in app.routes]

    # Auth
    assert "/api/v1/auth/login" in routes or any("/auth" in r for r in routes)

    # Workspace
    assert any("/workspaces" in r for r in routes)

    # Chat
    assert any("/chat/groups" in r for r in routes)

    # Pockets
    assert any("/pockets" in r for r in routes)

    # Sessions
    assert any("/sessions" in r for r in routes)

    # Agents
    assert any("/agents" in r for r in routes)

    # WebSocket
    assert any("ws/cloud" in r for r in routes)

    # License
    assert "/api/v1/license" in routes
```

**Step 2: Run and verify**

Run: `uv run pytest tests/cloud/test_integration.py -v`

**Step 3: Commit**

```bash
git add tests/cloud/test_integration.py
git commit -m "test(cloud): add integration smoke test for route mounting"
```

---

## Summary

| Phase | Tasks | Description |
|-------|-------|-------------|
| 1 | 1-6 | Foundation: errors, events, permissions, db, deps, model cleanup |
| 2 | 7 | Auth domain package |
| 3 | 8 | Workspace domain package |
| 4 | 9 | Agents domain package |
| 5 | 10-13 | Chat domain: schemas, service, WebSocket manager, router |
| 6 | 14 | Pockets domain package |
| 7 | 15 | Sessions domain package |
| 8 | 16-20 | Integration: mount_cloud, serve.py update, delete old files, events, smoke test |

**Total: 20 tasks, ~60 steps**

Each task produces a working commit. Tests written before or alongside implementation. No task depends on MongoDB running (pure unit tests + route registration tests).
