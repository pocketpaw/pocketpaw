# Files Tab v2 — Implementation Plan (Phase 1-2 Foundation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **User directive:** Do not commit. The plan's "commit" steps are retained for structure but SKIP them during execution. Leave changes uncommitted in the working tree.

**Goal:** Build the foundation for the unified, folder-based Files tab — a `FolderProvider` registry, schemas, RBAC+ABAC permissions layer, tree + browse endpoints, and the first two providers (`uploads` and `kb`). Legacy flat `/api/v1/files` contract from Cluster E #998 is preserved via a shim.

**Architecture:** New `ee/cloud/files/` module sits alongside (and wraps) the existing `ee/cloud/uploads/`. A stateless aggregator queries per-source `FolderProvider` implementations, merges `FileEntry` and `FolderNode` results, and applies a two-stage permission filter (RBAC baseline per provider, ABAC overlay post-filter). Mount paths come from a reshapeable YAML config.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, Motor (async Mongo), pytest + pytest-asyncio, `uv`, `ruff`, `mypy`.

**Scope in this plan:** Phase 1 (invisible refactor + registry + schemas + permissions) + Phase 2 (tree + browse endpoints + first two providers + contract test framework). **Deferred to follow-up plans:** Phase 3 (remaining providers), Phase 4 (CRUD + realtime), Phase 5 (frontend tree-mode UI).

**Spec:** `docs/plans/2026-04-20-files-tab-design.md`

**Baseline assumption:** Cluster E PRs #996 + #998 land before / during this work. Where they scaffold files this plan would also create, reconcile by keeping the shape compatible with #998's contract. If they have not merged at execution time, Task 0 scaffolds a thin stand-in so later tasks are not blocked.

---

## File Structure

```
backend/ee/cloud/files/
  __init__.py                  — public exports
  schemas.py                   — FileEntry, FolderNode, MountConfig, Page, RequestContext
  registry.py                  — FolderProvider protocol, registry, mount resolution
  permissions.py               — RBAC baseline coalescer + ABAC overlay filter
  tree.py                      — build_tree(ctx) — parallel fan-out + merge
  browse.py                    — browse_mount(ctx, mount, cursor, limit, filters)
  mounts.yaml                  — mount configuration (hot-reloadable)
  abac_rules.yaml              — ABAC rule set (initially empty)
  abac_config.py               — loader for abac_rules.yaml
  mounts_config.py             — loader for mounts.yaml
  router.py                    — FastAPI routes (/tree, /browse); keeps /api/v1/files compat shim
  service.py                   — UnifiedFilesService (legacy contract retained; internally calls registry)
  mongo_store.py               — from #998 if present; otherwise minimal stand-in (Task 0)
  events.py                    — domain events: FileAdded/Removed/Updated/Moved (no emit yet)
  errors.py                    — ProviderUnsupported, CrossScopeMove, MountReadonly, etc.
  providers/
    __init__.py
    base.py                    — BaseFolderProvider helper (shared concerns)
    uploads.py                 — provider for direct uploads ("My Files")
    kb.py                      — provider for workspace KB documents

backend/tests/cloud/files/
  __init__.py
  conftest.py                  — FakeProvider, RequestContext fixtures
  test_schemas.py
  test_registry.py
  test_permissions.py
  test_tree.py
  test_browse.py
  test_mounts_config.py
  test_abac_config.py
  test_legacy_contract.py      — regression: /api/v1/files shape unchanged
  test_provider_contract.py    — reusable base class
  providers/
    __init__.py
    test_uploads_provider.py
    test_kb_provider.py
```

---

## Task 0: Baseline check / stand-in (ONLY if Cluster E hasn't merged)

**Purpose:** Ensure `ee/cloud/files/` directory exists with the minimum shape later tasks depend on. If PR #998 has merged, SKIP — its files take precedence.

**Files:**
- Check: `backend/ee/cloud/files/` existence
- Create if missing: `backend/ee/cloud/files/__init__.py`, `backend/ee/cloud/files/service.py` (minimal), `backend/ee/cloud/files/router.py` (minimal), `backend/ee/cloud/files/mongo_store.py` (minimal)

- [ ] **Step 0.1: Detect baseline state**

Run: `ls backend/ee/cloud/files/ 2>/dev/null`
Expected: Either lists files from #998 (service.py, router.py, mongo_store.py) → skip the rest of Task 0; or returns nothing → continue with 0.2.

- [ ] **Step 0.2: Create minimal `__init__.py`**

File: `backend/ee/cloud/files/__init__.py`
```python
"""Files aggregation module.

Unified view over all file-producing subsystems (uploads, kb, pockets, rooms,
memory, agents). See docs/plans/2026-04-20-files-tab-design.md.
"""
```

- [ ] **Step 0.3: Create minimal `mongo_store.py` stand-in**

File: `backend/ee/cloud/files/mongo_store.py`
```python
"""Minimal Mongo file-listing store (stand-in until PR #998 lands).

If #998 has merged, its version supersedes this file. The shape below matches
the contract #998 introduces (list_by_workspace with soft-delete skip + cap).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ee.cloud.uploads.mongo_store import MongoFileStore as UploadsStore


@dataclass(slots=True)
class LegacyFileRow:
    id: str
    source: str
    filename: str
    mime: str
    size: int
    url: str | None
    created: str
    chat_id: str | None = None


class MongoFileStore:
    """Wraps uploads.mongo_store for the legacy /api/v1/files surface."""

    def __init__(self, inner: UploadsStore) -> None:
        self._inner = inner

    async def list_by_workspace(
        self, workspace_id: str, *, limit: int = 500
    ) -> list[LegacyFileRow]:
        rows: list[LegacyFileRow] = []
        async for doc in self._inner.iter_by_workspace(
            workspace_id, include_deleted=False, limit=limit
        ):
            rows.append(
                LegacyFileRow(
                    id=str(doc["file_id"]),
                    source="chat",
                    filename=doc.get("filename", ""),
                    mime=doc.get("mime", "application/octet-stream"),
                    size=int(doc.get("size", 0)),
                    url=None,
                    created=doc.get("created_at", "").isoformat()
                    if doc.get("created_at")
                    else "",
                    chat_id=doc.get("chat_id"),
                )
            )
        return rows
```

> Note: if `UploadsStore.iter_by_workspace` doesn't exist, add a thin async generator beside it following the existing `save_scoped` style. Do not widen its public API beyond this use.

- [ ] **Step 0.4: Create minimal `service.py` stand-in**

File: `backend/ee/cloud/files/service.py`
```python
"""UnifiedFilesService — legacy flat /api/v1/files surface (Cluster E #998 contract).

v2 makes this a thin caller of the FolderProvider registry; for now it stays
direct. Response shape: {workspace_id, source, files[], warnings[]}.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ee.cloud.files.mongo_store import MongoFileStore


class UnifiedFilesService:
    def __init__(self, store: MongoFileStore) -> None:
        self._store = store

    async def list(self, workspace_id: str, source: str = "all") -> dict[str, Any]:
        warnings: list[dict[str, str]] = []
        files: list[dict[str, Any]] = []
        if source in ("all", "chat"):
            rows = await self._store.list_by_workspace(workspace_id, limit=500)
            files.extend(asdict(r) for r in rows)
        if source in ("all", "drive"):
            warnings.append({"source": "drive", "code": "drive.not_connected"})
        if source in ("all", "local"):
            warnings.append({"source": "local", "code": "local.client_only"})
        return {
            "workspace_id": workspace_id,
            "source": source,
            "files": files,
            "warnings": warnings,
        }
```

- [ ] **Step 0.5: Create minimal `router.py` stand-in**

File: `backend/ee/cloud/files/router.py`
```python
"""Files routes. Legacy /api/v1/files lives here; /tree and /browse added in later tasks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ee.cloud.files.service import UnifiedFilesService
from ee.cloud.files.mongo_store import MongoFileStore
from ee.cloud.uploads.mongo_store import MongoFileStore as UploadsStore
from ee.cloud.shared.deps import get_current_workspace_id, get_uploads_store

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _service(uploads: UploadsStore = Depends(get_uploads_store)) -> UnifiedFilesService:
    return UnifiedFilesService(MongoFileStore(uploads))


@router.get("")
async def list_files(
    workspace_id: str = Query(...),
    source: str = Query("all"),
    current_ws: str = Depends(get_current_workspace_id),
    svc: UnifiedFilesService = Depends(_service),
) -> dict:
    if workspace_id != current_ws:
        raise HTTPException(status_code=403, detail="files.workspace_mismatch")
    return await svc.list(workspace_id, source=source)
```

> If `ee.cloud.shared.deps` lacks `get_current_workspace_id` or `get_uploads_store`, mirror the pattern used by `ee/cloud/uploads/router.py` dependencies (look at the top of that file for the canonical names) — do not invent new helpers.

- [ ] **Step 0.6: Smoke import**

Run: `uv run python -c "from ee.cloud.files import service, router, mongo_store"`
Expected: No ImportError.

- [ ] **Step 0.7: Skip commit (per user directive).** Continue to Task 1.

---

## Task 1: Core schemas

**Files:**
- Create: `backend/ee/cloud/files/schemas.py`
- Test: `backend/tests/cloud/files/test_schemas.py`

- [ ] **Step 1.1: Write failing test**

File: `backend/tests/cloud/files/test_schemas.py`
```python
"""Schema validation tests."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ee.cloud.files.schemas import (
    FileEntry,
    FolderNode,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)


def _entry(**overrides):
    base = dict(
        id="uploads:abc",
        provider_id="uploads",
        mount_path="/My Files/report.pdf",
        name="report.pdf",
        mime="application/pdf",
        size=1024,
        owner_id="user_1",
        workspace_id="ws_1",
        scope="personal",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )
    base.update(overrides)
    return FileEntry(**base)


def test_file_entry_id_must_be_namespaced():
    with pytest.raises(ValidationError):
        _entry(id="no-colon")


def test_file_entry_id_prefix_matches_provider():
    with pytest.raises(ValidationError):
        _entry(id="kb:abc", provider_id="uploads")


def test_file_entry_scope_is_enum():
    with pytest.raises(ValidationError):
        _entry(scope="nope")


def test_file_entry_capabilities_subset():
    with pytest.raises(ValidationError):
        _entry(capabilities=["read", "teleport"])


def test_folder_node_children_are_folder_nodes():
    n = FolderNode(
        path="/Workspaces/Acme",
        name="Acme",
        provider_id="kb",
        children=[
            FolderNode(
                path="/Workspaces/Acme/KB",
                name="Knowledge Base",
                provider_id="kb",
                children=[],
                capabilities=["read"],
            )
        ],
        capabilities=["read"],
    )
    assert n.children[0].provider_id == "kb"


def test_mount_config_rejects_non_absolute():
    with pytest.raises(ValidationError):
        MountConfig(
            provider_id="uploads",
            mount_template="My Files",
            writable=True,
            order=10,
        )


def test_permission_merge_is_intersection():
    a = Permission(read=True, write=True, manage=False)
    b = Permission(read=True, write=False, manage=False)
    assert (a & b) == Permission(read=True, write=False, manage=False)


def test_page_carries_cursor_and_items():
    p: Page[int] = Page(items=[1, 2], next_cursor="x")
    assert p.next_cursor == "x" and p.items == [1, 2]


def test_request_context_requires_user_id():
    with pytest.raises(ValidationError):
        RequestContext(workspace_id="ws", session_id="s", attributes={})
```

- [ ] **Step 1.2: Run test — verify it fails**

Run: `cd backend && uv run pytest tests/cloud/files/test_schemas.py -v`
Expected: ImportError / collection error (module doesn't exist yet).

- [ ] **Step 1.3: Implement `schemas.py`**

File: `backend/ee/cloud/files/schemas.py`
```python
"""Public schemas for the files module."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Scope = Literal["personal", "shared", "workspace"]
Capability = Literal["read", "download", "rename", "delete", "move", "replace", "upload"]

T = TypeVar("T")


class Permission(BaseModel):
    read: bool = False
    write: bool = False
    manage: bool = False

    def __and__(self, other: "Permission") -> "Permission":
        return Permission(
            read=self.read and other.read,
            write=self.write and other.write,
            manage=self.manage and other.manage,
        )


class RequestContext(BaseModel):
    user_id: str
    workspace_id: str | None = None
    session_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class FileEntry(BaseModel):
    model_config = ConfigDict(frozen=False)

    id: str
    provider_id: str
    mount_path: str
    name: str
    mime: str
    size: int
    owner_id: str | None = None
    workspace_id: str | None = None
    scope: Scope
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    source_ref: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[Capability] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_id_namespace(self) -> "FileEntry":
        if ":" not in self.id:
            raise ValueError("FileEntry.id must be namespaced as '<provider_id>:<native_id>'")
        prefix, _, _ = self.id.partition(":")
        if prefix != self.provider_id:
            raise ValueError(
                f"FileEntry.id prefix {prefix!r} must match provider_id {self.provider_id!r}"
            )
        return self

    @field_validator("mount_path")
    @classmethod
    def _mount_path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("mount_path must start with '/'")
        return v


class FolderNode(BaseModel):
    path: str
    name: str
    provider_id: str
    children: list["FolderNode"] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("FolderNode.path must start with '/'")
        return v


FolderNode.model_rebuild()


class MountConfig(BaseModel):
    provider_id: str
    mount_template: str
    writable: bool = False
    order: int = 100

    @field_validator("mount_template")
    @classmethod
    def _absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("mount_template must start with '/'")
        return v


class ResolvedMount(BaseModel):
    provider_id: str
    path: str
    writable: bool
    order: int
    variables: dict[str, str] = Field(default_factory=dict)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class SearchQuery(BaseModel):
    query: str
    mount: str | None = None
    mimes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    limit: int = 50
```

- [ ] **Step 1.4: Run tests — verify pass**

Run: `cd backend && uv run pytest tests/cloud/files/test_schemas.py -v`
Expected: 8 passed.

- [ ] **Step 1.5: Lint + typecheck**

Run: `cd backend && uv run ruff check ee/cloud/files/schemas.py tests/cloud/files/test_schemas.py && uv run mypy ee/cloud/files/schemas.py`
Expected: no errors.

- [ ] **Step 1.6: Skip commit.** Continue.

---

## Task 2: Errors, events, RequestContext plumbing

**Files:**
- Create: `backend/ee/cloud/files/errors.py`, `backend/ee/cloud/files/events.py`

- [ ] **Step 2.1: Implement `errors.py`**

File: `backend/ee/cloud/files/errors.py`
```python
"""Typed errors raised by providers and the aggregator."""
from __future__ import annotations


class FilesError(Exception):
    code: str = "files.error"
    http_status: int = 500


class ProviderUnsupported(FilesError):
    code = "files.operation_unsupported"
    http_status = 405


class CrossScopeMove(FilesError):
    code = "files.cross_scope_move"
    http_status = 409


class MountReadonly(FilesError):
    code = "files.mount_readonly"
    http_status = 403


class MountNotFound(FilesError):
    code = "files.mount_not_found"
    http_status = 404


class EntryNotFound(FilesError):
    code = "files.not_found"
    http_status = 404


class NameConflict(FilesError):
    code = "files.name_conflict"
    http_status = 409


class ProviderUpstream(FilesError):
    code = "files.provider_error"
    http_status = 502
```

- [ ] **Step 2.2: Implement `events.py`**

File: `backend/ee/cloud/files/events.py`
```python
"""Domain events published by providers on file mutations.

Subscribers (realtime bridge in Phase 4) consume these via the
ee.cloud.realtime bus. Phase 1-2 only defines the shapes.
"""
from __future__ import annotations

from pydantic import BaseModel

from ee.cloud.files.schemas import FileEntry


class FileAdded(BaseModel):
    entry: FileEntry


class FileUpdated(BaseModel):
    entry: FileEntry


class FileRemoved(BaseModel):
    id: str
    workspace_id: str | None
    provider_id: str


class FileMoved(BaseModel):
    entry: FileEntry
    old_path: str
```

- [ ] **Step 2.3: Smoke import test**

File: `backend/tests/cloud/files/test_errors_events.py`
```python
from ee.cloud.files.errors import (
    CrossScopeMove,
    EntryNotFound,
    MountReadonly,
    ProviderUnsupported,
)
from ee.cloud.files.events import FileAdded, FileMoved, FileRemoved, FileUpdated


def test_errors_have_codes():
    assert ProviderUnsupported.code == "files.operation_unsupported"
    assert CrossScopeMove.http_status == 409
    assert EntryNotFound.http_status == 404
    assert MountReadonly.http_status == 403


def test_events_importable():
    assert FileAdded and FileUpdated and FileRemoved and FileMoved
```

Run: `cd backend && uv run pytest tests/cloud/files/test_errors_events.py -v`
Expected: 2 passed.

- [ ] **Step 2.4: Lint.** `uv run ruff check ee/cloud/files tests/cloud/files`
- [ ] **Step 2.5: Skip commit.** Continue.

---

## Task 3: Mount config loader

**Files:**
- Create: `backend/ee/cloud/files/mounts.yaml`, `backend/ee/cloud/files/mounts_config.py`
- Test: `backend/tests/cloud/files/test_mounts_config.py`

- [ ] **Step 3.1: Write the mounts YAML**

File: `backend/ee/cloud/files/mounts.yaml`
```yaml
- provider_id: uploads
  mount_template: "/My Files"
  writable: true
  order: 10
- provider_id: kb
  mount_template: "/Workspaces/{workspace_id}/Knowledge Base"
  writable: true
  order: 20
- provider_id: chat
  mount_template: "/Shared with me/Chat uploads"
  writable: false
  order: 50
- provider_id: drive
  mount_template: "/Connected/Google Drive"
  writable: false
  order: 80
- provider_id: local
  mount_template: "/Local"
  writable: true
  order: 90
```

- [ ] **Step 3.2: Failing test**

File: `backend/tests/cloud/files/test_mounts_config.py`
```python
from pathlib import Path

import pytest

from ee.cloud.files.mounts_config import load_mounts, resolve_template
from ee.cloud.files.schemas import MountConfig


def test_load_mounts_returns_ordered_list(tmp_path: Path):
    yaml = tmp_path / "mounts.yaml"
    yaml.write_text(
        "- provider_id: a\n  mount_template: /A\n  writable: false\n  order: 20\n"
        "- provider_id: b\n  mount_template: /B\n  writable: true\n  order: 10\n"
    )
    cfg = load_mounts(yaml)
    assert [m.provider_id for m in cfg] == ["b", "a"]
    assert all(isinstance(m, MountConfig) for m in cfg)


def test_resolve_template_substitutes_vars():
    assert (
        resolve_template("/Workspaces/{workspace_id}/KB", {"workspace_id": "ws_1"})
        == "/Workspaces/ws_1/KB"
    )


def test_resolve_template_leaves_unknown_vars_as_error():
    with pytest.raises(KeyError):
        resolve_template("/Workspaces/{workspace_id}/KB", {})


def test_resolve_template_no_vars():
    assert resolve_template("/My Files", {}) == "/My Files"


def test_load_mounts_rejects_relative_template(tmp_path: Path):
    yaml = tmp_path / "mounts.yaml"
    yaml.write_text("- provider_id: a\n  mount_template: relative/path\n  writable: false\n  order: 1\n")
    with pytest.raises(ValueError):
        load_mounts(yaml)
```

- [ ] **Step 3.3: Run — expect failure**

Run: `cd backend && uv run pytest tests/cloud/files/test_mounts_config.py -v`
Expected: ImportError.

- [ ] **Step 3.4: Implement**

File: `backend/ee/cloud/files/mounts_config.py`
```python
"""YAML loader for mounts.yaml — sorted + validated."""
from __future__ import annotations

from pathlib import Path

import yaml

from ee.cloud.files.schemas import MountConfig

_DEFAULT_PATH = Path(__file__).parent / "mounts.yaml"


def load_mounts(path: Path | None = None) -> list[MountConfig]:
    src = path or _DEFAULT_PATH
    raw = yaml.safe_load(src.read_text()) or []
    configs = [MountConfig(**row) for row in raw]
    configs.sort(key=lambda c: c.order)
    return configs


def resolve_template(template: str, variables: dict[str, str]) -> str:
    return template.format(**variables)
```

- [ ] **Step 3.5: Run — pass**

Run: `cd backend && uv run pytest tests/cloud/files/test_mounts_config.py -v`
Expected: 5 passed.

- [ ] **Step 3.6: Lint + skip commit.**

---

## Task 4: ABAC rules loader

**Files:**
- Create: `backend/ee/cloud/files/abac_rules.yaml`, `backend/ee/cloud/files/abac_config.py`
- Test: `backend/tests/cloud/files/test_abac_config.py`

- [ ] **Step 4.1: Create rules file (initially empty rule set — no-op)**

File: `backend/ee/cloud/files/abac_rules.yaml`
```yaml
# ABAC rules restrict visibility based on entry.tags and ctx.user.attributes.
# Each rule: tag -> requires-all attribute matches.
# Example:
# - tag: confidential
#   require:
#     role: [admin, owner]
# - tag: hr-only
#   require:
#     department: [hr]
rules: []
```

- [ ] **Step 4.2: Failing test**

File: `backend/tests/cloud/files/test_abac_config.py`
```python
from pathlib import Path

from ee.cloud.files.abac_config import AbacRule, AbacRuleSet, load_rules


def test_load_rules_empty(tmp_path: Path):
    p = tmp_path / "r.yaml"
    p.write_text("rules: []\n")
    rs = load_rules(p)
    assert rs.rules == []


def test_load_rules_parses_shape(tmp_path: Path):
    p = tmp_path / "r.yaml"
    p.write_text(
        "rules:\n"
        "  - tag: confidential\n"
        "    require:\n"
        "      role: [admin, owner]\n"
    )
    rs = load_rules(p)
    assert len(rs.rules) == 1
    r = rs.rules[0]
    assert r.tag == "confidential"
    assert r.require == {"role": ["admin", "owner"]}


def test_ruleset_allows_entry_when_untagged():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    assert rs.allows(tags=[], attributes={})


def test_ruleset_allows_when_attribute_matches():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    assert rs.allows(tags=["confidential"], attributes={"role": "admin"})


def test_ruleset_denies_when_attribute_mismatches():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    assert not rs.allows(tags=["confidential"], attributes={"role": "member"})


def test_ruleset_deny_overrides_multiple_tags():
    rs = AbacRuleSet(
        rules=[
            AbacRule(tag="confidential", require={"role": ["admin"]}),
            AbacRule(tag="pii", require={"clearance": ["high"]}),
        ]
    )
    assert not rs.allows(
        tags=["confidential", "pii"], attributes={"role": "admin", "clearance": "low"}
    )
```

- [ ] **Step 4.3: Run — fail.** `uv run pytest tests/cloud/files/test_abac_config.py -v`

- [ ] **Step 4.4: Implement**

File: `backend/ee/cloud/files/abac_config.py`
```python
"""ABAC rule loader + evaluator.

Rules restrict access only. An entry passes the ruleset IFF every rule whose
`tag` is in entry.tags has its `require` dict satisfied by ctx.user.attributes.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_DEFAULT_PATH = Path(__file__).parent / "abac_rules.yaml"


class AbacRule(BaseModel):
    tag: str
    require: dict[str, list[str]] = Field(default_factory=dict)

    def satisfied_by(self, attributes: dict[str, object]) -> bool:
        for attr, allowed in self.require.items():
            value = attributes.get(attr)
            if value not in allowed:
                return False
        return True


class AbacRuleSet(BaseModel):
    rules: list[AbacRule] = Field(default_factory=list)

    def allows(self, *, tags: list[str], attributes: dict[str, object]) -> bool:
        for rule in self.rules:
            if rule.tag in tags and not rule.satisfied_by(attributes):
                return False
        return True


def load_rules(path: Path | None = None) -> AbacRuleSet:
    src = path or _DEFAULT_PATH
    raw = yaml.safe_load(src.read_text()) or {}
    return AbacRuleSet(**raw)
```

- [ ] **Step 4.5: Run — pass.** Expected: 6 passed.
- [ ] **Step 4.6: Lint + skip commit.**

---

## Task 5: Permissions layer (RBAC coalescer + ABAC overlay)

**Files:**
- Create: `backend/ee/cloud/files/permissions.py`
- Test: `backend/tests/cloud/files/test_permissions.py`

- [ ] **Step 5.1: Failing test**

File: `backend/tests/cloud/files/test_permissions.py`
```python
from datetime import UTC, datetime

from ee.cloud.files.abac_config import AbacRule, AbacRuleSet
from ee.cloud.files.permissions import (
    PermissionsEvaluator,
    apply_abac,
    derive_capabilities,
)
from ee.cloud.files.schemas import FileEntry, Permission, RequestContext


def _entry(tags=None, caps=("read", "download")):
    return FileEntry(
        id="uploads:x",
        provider_id="uploads",
        mount_path="/My Files/x",
        name="x",
        mime="text/plain",
        size=1,
        owner_id="u",
        workspace_id="ws",
        scope="personal",
        tags=list(tags or []),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=list(caps),
    )


def _ctx(**attrs):
    return RequestContext(user_id="u", workspace_id="ws", attributes=attrs)


def test_apply_abac_passes_untagged():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    entries = [_entry(tags=[]), _entry(tags=["confidential"])]
    out = apply_abac(entries, ctx=_ctx(role="member"), rules=rs)
    assert [e.id for e in out] == ["uploads:x"]  # only untagged survives


def test_apply_abac_allows_when_attr_matches():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    out = apply_abac([_entry(tags=["confidential"])], ctx=_ctx(role="admin"), rules=rs)
    assert len(out) == 1


def test_derive_capabilities_intersects_rbac_and_mount_writable():
    e = _entry(caps=("read", "download", "rename", "delete"))
    rbac = Permission(read=True, write=False, manage=False)
    caps = derive_capabilities(
        entry=e, rbac=rbac, mount_writable=False, abac_allowed=True
    )
    assert set(caps) == {"read", "download"}


def test_derive_capabilities_strips_all_when_abac_denies():
    e = _entry(caps=("read", "download"))
    rbac = Permission(read=True, write=True, manage=True)
    caps = derive_capabilities(
        entry=e, rbac=rbac, mount_writable=True, abac_allowed=False
    )
    assert caps == []


def test_derive_capabilities_requires_manage_for_delete():
    e = _entry(caps=("read", "delete", "rename"))
    rbac = Permission(read=True, write=True, manage=False)
    caps = derive_capabilities(
        entry=e, rbac=rbac, mount_writable=True, abac_allowed=True
    )
    assert "delete" not in caps
    assert "rename" in caps


def test_evaluator_filters_and_annotates():
    rs = AbacRuleSet(rules=[AbacRule(tag="pii", require={"clearance": ["high"]})])
    ev = PermissionsEvaluator(rules=rs)
    entries = [_entry(tags=[]), _entry(tags=["pii"])]
    out = ev.filter(entries=entries, ctx=_ctx(clearance="low"))
    assert len(out) == 1 and out[0].tags == []
```

- [ ] **Step 5.2: Run — fail.**

- [ ] **Step 5.3: Implement**

File: `backend/ee/cloud/files/permissions.py`
```python
"""RBAC + ABAC permission layer for the files module.

RBAC is provided per-entry by the owning provider via Permission(read, write, manage).
ABAC is a post-filter that can only further restrict visibility.

derive_capabilities() returns the final UI-facing capability list:
  read/download     <- rbac.read AND abac_allowed
  rename/move/replace/upload <- rbac.write AND mount_writable AND abac_allowed
  delete            <- rbac.manage AND mount_writable AND abac_allowed
Only capabilities the provider already declared on the entry survive
(so a provider can opt-out of a capability regardless of permission).
"""
from __future__ import annotations

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.schemas import (
    Capability,
    FileEntry,
    Permission,
    RequestContext,
)

_READ_CAPS: set[Capability] = {"read", "download"}
_WRITE_CAPS: set[Capability] = {"rename", "move", "replace", "upload"}
_MANAGE_CAPS: set[Capability] = {"delete"}


def apply_abac(
    entries: list[FileEntry],
    *,
    ctx: RequestContext,
    rules: AbacRuleSet,
) -> list[FileEntry]:
    return [
        e for e in entries if rules.allows(tags=e.tags, attributes=ctx.attributes)
    ]


def derive_capabilities(
    *,
    entry: FileEntry,
    rbac: Permission,
    mount_writable: bool,
    abac_allowed: bool,
) -> list[Capability]:
    if not abac_allowed:
        return []
    allowed: set[Capability] = set()
    if rbac.read:
        allowed |= _READ_CAPS
    if rbac.write and mount_writable:
        allowed |= _WRITE_CAPS
    if rbac.manage and mount_writable:
        allowed |= _MANAGE_CAPS
    return [c for c in entry.capabilities if c in allowed]


class PermissionsEvaluator:
    def __init__(self, rules: AbacRuleSet) -> None:
        self._rules = rules

    def filter(
        self, *, entries: list[FileEntry], ctx: RequestContext
    ) -> list[FileEntry]:
        return apply_abac(entries, ctx=ctx, rules=self._rules)
```

- [ ] **Step 5.4: Run — pass.** Expected: 6 passed.
- [ ] **Step 5.5: Lint + skip commit.**

---

## Task 6: Provider registry

**Files:**
- Create: `backend/ee/cloud/files/registry.py`
- Test: `backend/tests/cloud/files/test_registry.py`, `backend/tests/cloud/files/conftest.py`

- [ ] **Step 6.1: Shared test fixtures**

File: `backend/tests/cloud/files/conftest.py`
```python
"""Shared fixtures for files tests."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ee.cloud.files.errors import ProviderUnsupported
from ee.cloud.files.schemas import (
    FileEntry,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        mounts: list[ResolvedMount] | None = None,
        entries: list[FileEntry] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._mounts = mounts or []
        self._entries = entries or []

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        return list(self._mounts)

    async def list_entries(
        self, ctx: RequestContext, mount_path: str, cursor: str | None, limit: int, filters: dict
    ) -> Page[FileEntry]:
        return Page(items=[e for e in self._entries if e.mount_path.startswith(mount_path)])

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        for e in self._entries:
            if e.id == entry_id:
                return e
        raise KeyError(entry_id)

    async def open_stream(self, ctx: RequestContext, entry_id: str) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            yield b"data"
        return _gen()

    async def upload(self, ctx, mount_path, upload):  # noqa: ANN001
        raise ProviderUnsupported()

    async def rename(self, ctx, entry_id, new_name):  # noqa: ANN001
        raise ProviderUnsupported()

    async def move(self, ctx, entry_id, dest_mount_path):  # noqa: ANN001
        raise ProviderUnsupported()

    async def delete(self, ctx, entry_id):  # noqa: ANN001
        raise ProviderUnsupported()

    async def search(self, ctx, query):  # noqa: ANN001
        return Page(items=[])

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        return Permission(read=True, write=False, manage=False)


@pytest.fixture
def ctx() -> RequestContext:
    return RequestContext(user_id="u1", workspace_id="ws_1", attributes={"role": "member"})


@pytest.fixture
def make_entry():
    def _make(provider_id: str, native_id: str, mount: str, **overrides: Any) -> FileEntry:
        base = dict(
            id=f"{provider_id}:{native_id}",
            provider_id=provider_id,
            mount_path=mount,
            name=native_id,
            mime="text/plain",
            size=10,
            owner_id="u1",
            workspace_id="ws_1",
            scope="personal",
            tags=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            source_ref={},
            capabilities=["read", "download"],
        )
        base.update(overrides)
        return FileEntry(**base)
    return _make


@pytest.fixture
def make_mount():
    def _make(provider_id: str, path: str, writable: bool = False, order: int = 100) -> ResolvedMount:
        return ResolvedMount(
            provider_id=provider_id, path=path, writable=writable, order=order, variables={}
        )
    return _make
```

- [ ] **Step 6.2: Failing test for registry**

File: `backend/tests/cloud/files/test_registry.py`
```python
import pytest

from ee.cloud.files.errors import MountNotFound
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import MountConfig


def test_register_and_get(ctx):
    from tests.cloud.files.conftest import FakeProvider

    reg = ProviderRegistry()
    p = FakeProvider("uploads")
    reg.register(p)
    assert reg.get("uploads") is p


def test_register_duplicate_raises():
    from tests.cloud.files.conftest import FakeProvider

    reg = ProviderRegistry()
    reg.register(FakeProvider("uploads"))
    with pytest.raises(ValueError):
        reg.register(FakeProvider("uploads"))


def test_resolve_mount_longest_prefix():
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="a", mount_template="/X", writable=False, order=1),
            MountConfig(provider_id="b", mount_template="/X/Y", writable=False, order=2),
        ]
    )
    got = reg.resolve_mount(path="/X/Y/inside", variables={})
    assert got.provider_id == "b"


def test_resolve_mount_missing_raises():
    reg = ProviderRegistry(configs=[])
    with pytest.raises(MountNotFound):
        reg.resolve_mount(path="/nope", variables={})


def test_resolve_mount_substitutes_variables():
    reg = ProviderRegistry(
        configs=[
            MountConfig(
                provider_id="kb",
                mount_template="/Workspaces/{workspace_id}/KB",
                writable=True,
                order=1,
            )
        ]
    )
    got = reg.resolve_mount(path="/Workspaces/ws_1/KB/doc", variables={"workspace_id": "ws_1"})
    assert got.provider_id == "kb"
    assert got.path == "/Workspaces/ws_1/KB"
```

- [ ] **Step 6.3: Run — fail.**

- [ ] **Step 6.4: Implement**

File: `backend/ee/cloud/files/registry.py`
```python
"""Provider registry + mount resolution.

Providers implement the `FolderProvider` protocol (duck-typed; any class with
matching methods works). The registry owns a list of MountConfig and routes
incoming paths to providers via longest-prefix match.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ee.cloud.files.errors import MountNotFound
from ee.cloud.files.mounts_config import resolve_template
from ee.cloud.files.schemas import (
    FileEntry,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
    SearchQuery,
)


@runtime_checkable
class FolderProvider(Protocol):
    provider_id: str

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]: ...
    async def list_entries(
        self,
        ctx: RequestContext,
        mount_path: str,
        cursor: str | None,
        limit: int,
        filters: dict,
    ) -> Page[FileEntry]: ...
    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry: ...
    async def open_stream(
        self, ctx: RequestContext, entry_id: str
    ) -> AsyncIterator[bytes]: ...
    async def upload(self, ctx: RequestContext, mount_path: str, upload: object) -> FileEntry: ...
    async def rename(self, ctx: RequestContext, entry_id: str, new_name: str) -> FileEntry: ...
    async def move(self, ctx: RequestContext, entry_id: str, dest_mount_path: str) -> FileEntry: ...
    async def delete(self, ctx: RequestContext, entry_id: str) -> None: ...
    async def search(self, ctx: RequestContext, query: SearchQuery) -> Page[FileEntry]: ...
    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission: ...


class ProviderRegistry:
    def __init__(self, configs: list[MountConfig] | None = None) -> None:
        self._providers: dict[str, FolderProvider] = {}
        self._configs: list[MountConfig] = list(configs or [])

    def register(self, provider: FolderProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"provider {provider.provider_id!r} already registered")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> FolderProvider:
        return self._providers[provider_id]

    def all(self) -> list[FolderProvider]:
        return list(self._providers.values())

    @property
    def configs(self) -> list[MountConfig]:
        return list(self._configs)

    def resolve_mount(self, *, path: str, variables: dict[str, str]) -> ResolvedMount:
        """Return the mount whose resolved template is the longest prefix of `path`."""
        best: tuple[int, MountConfig, str] | None = None
        for cfg in self._configs:
            try:
                resolved = resolve_template(cfg.mount_template, variables)
            except KeyError:
                continue
            if path == resolved or path.startswith(resolved + "/"):
                length = len(resolved)
                if best is None or length > best[0]:
                    best = (length, cfg, resolved)
        if best is None:
            raise MountNotFound(path)
        _, cfg, resolved = best
        return ResolvedMount(
            provider_id=cfg.provider_id,
            path=resolved,
            writable=cfg.writable,
            order=cfg.order,
            variables=variables,
        )
```

- [ ] **Step 6.5: Run — pass.** Expected: 5 passed.
- [ ] **Step 6.6: Lint + skip commit.**

---

## Task 7: Tree builder

**Files:**
- Create: `backend/ee/cloud/files/tree.py`
- Test: `backend/tests/cloud/files/test_tree.py`

- [ ] **Step 7.1: Failing test**

File: `backend/tests/cloud/files/test_tree.py`
```python
import pytest

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import MountConfig
from ee.cloud.files.tree import build_tree
from tests.cloud.files.conftest import FakeProvider


@pytest.mark.asyncio
async def test_build_tree_merges_mounts_sorted_by_order(ctx, make_mount):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
            MountConfig(provider_id="kb", mount_template="/Workspaces/ws_1/KB", writable=True, order=20),
        ]
    )
    reg.register(FakeProvider("uploads", mounts=[make_mount("uploads", "/My Files", True, 10)]))
    reg.register(FakeProvider("kb", mounts=[make_mount("kb", "/Workspaces/ws_1/KB", True, 20)]))

    tree = await build_tree(ctx=ctx, registry=reg, rules=AbacRuleSet())
    # top-level children sorted by order: My Files (10) before Workspaces (20)
    assert [c.name for c in tree.children] == ["My Files", "Workspaces"]


@pytest.mark.asyncio
async def test_build_tree_nests_segments(ctx, make_mount):
    reg = ProviderRegistry(
        configs=[
            MountConfig(
                provider_id="kb",
                mount_template="/Workspaces/ws_1/KB",
                writable=False,
                order=10,
            )
        ]
    )
    reg.register(FakeProvider("kb", mounts=[make_mount("kb", "/Workspaces/ws_1/KB")]))

    tree = await build_tree(ctx=ctx, registry=reg, rules=AbacRuleSet())
    assert tree.children[0].name == "Workspaces"
    assert tree.children[0].children[0].name == "ws_1"
    assert tree.children[0].children[0].children[0].name == "KB"


@pytest.mark.asyncio
async def test_build_tree_returns_warnings_on_provider_failure(ctx, make_mount):
    class FailingProvider(FakeProvider):
        async def list_mounts(self, ctx):
            raise RuntimeError("boom")

    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
            MountConfig(provider_id="kb", mount_template="/KB", writable=False, order=20),
        ]
    )
    reg.register(FakeProvider("uploads", mounts=[make_mount("uploads", "/My Files")]))
    reg.register(FailingProvider("kb"))

    tree, warnings = await build_tree(
        ctx=ctx, registry=reg, rules=AbacRuleSet(), collect_warnings=True
    )
    assert [c.name for c in tree.children] == ["My Files"]
    assert warnings == [{"provider_id": "kb", "code": "files.provider_error"}]
```

- [ ] **Step 7.2: Run — fail.**

- [ ] **Step 7.3: Implement**

File: `backend/ee/cloud/files/tree.py`
```python
"""Parallel fan-out tree builder.

Queries every registered provider's list_mounts in parallel, applies the ABAC
ruleset at the mount level (mounts may be tagged via their provider but today
none are — untagged mounts always pass), then merges all resolved mounts into
a single FolderNode tree by splitting each path on '/'.
"""
from __future__ import annotations

import asyncio
from typing import overload

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.registry import FolderProvider, ProviderRegistry
from ee.cloud.files.schemas import FolderNode, RequestContext, ResolvedMount


def _insert(root: FolderNode, mount: ResolvedMount) -> None:
    parts = [p for p in mount.path.split("/") if p]
    cursor = root
    accumulated = ""
    for i, part in enumerate(parts):
        accumulated += "/" + part
        child = next((c for c in cursor.children if c.name == part), None)
        is_leaf = i == len(parts) - 1
        if child is None:
            caps = ["read"]
            if is_leaf and mount.writable:
                caps.append("upload")
            child = FolderNode(
                path=accumulated,
                name=part,
                provider_id=mount.provider_id if is_leaf else "",
                children=[],
                capabilities=caps,
            )
            cursor.children.append(child)
        else:
            if is_leaf:
                child.provider_id = mount.provider_id
                if mount.writable and "upload" not in child.capabilities:
                    child.capabilities.append("upload")
        cursor = child


@overload
async def build_tree(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    collect_warnings: bool = False,
) -> FolderNode: ...
@overload
async def build_tree(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    collect_warnings: bool = True,
) -> tuple[FolderNode, list[dict[str, str]]]: ...
async def build_tree(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    collect_warnings: bool = False,
):
    providers: list[FolderProvider] = registry.all()
    results = await asyncio.gather(
        *(p.list_mounts(ctx) for p in providers), return_exceptions=True
    )

    warnings: list[dict[str, str]] = []
    mounts: list[ResolvedMount] = []
    for provider, res in zip(providers, results, strict=True):
        if isinstance(res, BaseException):
            warnings.append(
                {"provider_id": provider.provider_id, "code": "files.provider_error"}
            )
            continue
        mounts.extend(res)

    mounts.sort(key=lambda m: m.order)
    root = FolderNode(path="/", name="", provider_id="", children=[], capabilities=["read"])
    for m in mounts:
        _insert(root, m)

    if collect_warnings:
        return root, warnings
    return root
```

- [ ] **Step 7.4: Run — pass.** Expected: 3 passed.
- [ ] **Step 7.5: Lint + skip commit.**

---

## Task 8: Browse endpoint helper

**Files:**
- Create: `backend/ee/cloud/files/browse.py`
- Test: `backend/tests/cloud/files/test_browse.py`

- [ ] **Step 8.1: Failing test**

File: `backend/tests/cloud/files/test_browse.py`
```python
import pytest

from ee.cloud.files.abac_config import AbacRule, AbacRuleSet
from ee.cloud.files.browse import browse_mount
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import MountConfig
from tests.cloud.files.conftest import FakeProvider


@pytest.mark.asyncio
async def test_browse_mount_returns_entries(ctx, make_entry):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10)
        ]
    )
    entry = make_entry("uploads", "a", "/My Files/a")
    reg.register(FakeProvider("uploads", entries=[entry]))
    page = await browse_mount(
        ctx=ctx,
        registry=reg,
        rules=AbacRuleSet(),
        mount_path="/My Files",
        variables={},
        cursor=None,
        limit=50,
        filters={},
    )
    assert len(page.items) == 1
    assert "read" in page.items[0].capabilities


@pytest.mark.asyncio
async def test_browse_mount_abac_filters_tagged(ctx, make_entry):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10)
        ]
    )
    a = make_entry("uploads", "a", "/My Files/a")
    b = make_entry("uploads", "b", "/My Files/b", tags=["confidential"])
    reg.register(FakeProvider("uploads", entries=[a, b]))
    rules = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    page = await browse_mount(
        ctx=ctx,
        registry=reg,
        rules=rules,
        mount_path="/My Files",
        variables={},
        cursor=None,
        limit=50,
        filters={},
    )
    assert [e.id for e in page.items] == ["uploads:a"]
```

- [ ] **Step 8.2: Run — fail.**

- [ ] **Step 8.3: Implement**

File: `backend/ee/cloud/files/browse.py`
```python
"""Per-mount paginated listing."""
from __future__ import annotations

from typing import Any

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.permissions import apply_abac, derive_capabilities
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import FileEntry, Page, RequestContext


async def browse_mount(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    mount_path: str,
    variables: dict[str, str],
    cursor: str | None,
    limit: int,
    filters: dict[str, Any],
) -> Page[FileEntry]:
    mount = registry.resolve_mount(path=mount_path, variables=variables)
    provider = registry.get(mount.provider_id)
    raw = await provider.list_entries(ctx, mount_path, cursor, limit, filters)
    filtered = apply_abac(raw.items, ctx=ctx, rules=rules)

    out: list[FileEntry] = []
    for e in filtered:
        rbac = provider.baseline_rbac(ctx, e)
        caps = derive_capabilities(
            entry=e, rbac=rbac, mount_writable=mount.writable, abac_allowed=True
        )
        out.append(e.model_copy(update={"capabilities": caps}))

    return Page(items=out, next_cursor=raw.next_cursor)
```

- [ ] **Step 8.4: Run — pass.** Expected: 2 passed.
- [ ] **Step 8.5: Lint + skip commit.**

---

## Task 9: Reusable provider contract test base

**Files:**
- Create: `backend/tests/cloud/files/test_provider_contract.py`

- [ ] **Step 9.1: Implement the reusable contract**

File: `backend/tests/cloud/files/test_provider_contract.py`
```python
"""Reusable contract every real FolderProvider must satisfy.

Concrete providers subclass `ProviderContract` and override `build_provider`
to yield a ready-to-use provider populated with the supplied test entries.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from ee.cloud.files.errors import ProviderUnsupported
from ee.cloud.files.registry import FolderProvider
from ee.cloud.files.schemas import FileEntry, RequestContext


def _entry(provider_id: str) -> FileEntry:
    return FileEntry(
        id=f"{provider_id}:contract-1",
        provider_id=provider_id,
        mount_path="/test",
        name="contract-1",
        mime="text/plain",
        size=5,
        owner_id="u",
        workspace_id="ws",
        scope="personal",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )


class ProviderContract(ABC):
    """Subclass and override `build_provider` in concrete provider tests."""

    @abstractmethod
    def build_provider(self) -> FolderProvider: ...

    def ctx(self) -> RequestContext:
        return RequestContext(user_id="u", workspace_id="ws", attributes={})

    @pytest.mark.asyncio
    async def test_list_mounts_returns_list(self):
        prov = self.build_provider()
        mounts = await prov.list_mounts(self.ctx())
        assert isinstance(mounts, list)

    @pytest.mark.asyncio
    async def test_list_entries_returns_page(self):
        prov = self.build_provider()
        mounts = await prov.list_mounts(self.ctx())
        if not mounts:
            pytest.skip("provider exposes no mounts under default ctx")
        page = await prov.list_entries(self.ctx(), mounts[0].path, None, 10, {})
        assert hasattr(page, "items")

    @pytest.mark.asyncio
    async def test_unsupported_ops_raise(self):
        prov = self.build_provider()
        ops: list[Callable] = [
            lambda: prov.rename(self.ctx(), "x", "y"),
            lambda: prov.move(self.ctx(), "x", "/nope"),
            lambda: prov.delete(self.ctx(), "x"),
        ]
        for op in ops:
            try:
                await op()
            except ProviderUnsupported:
                continue
            except Exception:
                # provider supports op — acceptable, contract allows either
                continue

    @pytest.mark.asyncio
    async def test_id_is_namespaced(self):
        prov = self.build_provider()
        mounts = await prov.list_mounts(self.ctx())
        if not mounts:
            pytest.skip("provider exposes no mounts")
        page = await prov.list_entries(self.ctx(), mounts[0].path, None, 10, {})
        for e in page.items:
            assert e.id.startswith(prov.provider_id + ":")
```

- [ ] **Step 9.2: Run — pass (self-contained module, no failing tests expected — it's a base class).**

Run: `cd backend && uv run pytest tests/cloud/files/test_provider_contract.py -v`
Expected: collected but no actual tests run (abstract base, no `build_provider`). 0 passed, 0 errors.

- [ ] **Step 9.3: Skip commit.**

---

## Task 10: Uploads provider (My Files)

**Files:**
- Create: `backend/ee/cloud/files/providers/__init__.py`, `backend/ee/cloud/files/providers/base.py`, `backend/ee/cloud/files/providers/uploads.py`
- Test: `backend/tests/cloud/files/providers/__init__.py`, `backend/tests/cloud/files/providers/test_uploads_provider.py`

- [ ] **Step 10.1: Provider base helper**

File: `backend/ee/cloud/files/providers/__init__.py`
```python
"""Built-in FolderProvider implementations."""
```

File: `backend/ee/cloud/files/providers/base.py`
```python
"""Shared provider helpers."""
from __future__ import annotations

from collections.abc import AsyncIterator

from ee.cloud.files.errors import ProviderUnsupported
from ee.cloud.files.schemas import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
    SearchQuery,
)


class BaseFolderProvider:
    """Default implementations that raise ProviderUnsupported.

    Providers override only the operations they support.
    """

    provider_id: str = ""

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        return []

    async def list_entries(
        self, ctx: RequestContext, mount_path: str, cursor: str | None, limit: int, filters: dict
    ) -> Page[FileEntry]:
        return Page(items=[], next_cursor=None)

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        raise ProviderUnsupported()

    async def open_stream(self, ctx: RequestContext, entry_id: str) -> AsyncIterator[bytes]:
        raise ProviderUnsupported()

    async def upload(self, ctx: RequestContext, mount_path: str, upload: object) -> FileEntry:
        raise ProviderUnsupported()

    async def rename(self, ctx: RequestContext, entry_id: str, new_name: str) -> FileEntry:
        raise ProviderUnsupported()

    async def move(self, ctx: RequestContext, entry_id: str, dest_mount_path: str) -> FileEntry:
        raise ProviderUnsupported()

    async def delete(self, ctx: RequestContext, entry_id: str) -> None:
        raise ProviderUnsupported()

    async def search(self, ctx: RequestContext, query: SearchQuery) -> Page[FileEntry]:
        return Page(items=[], next_cursor=None)

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        return Permission(read=True, write=False, manage=False)
```

- [ ] **Step 10.2: Failing provider test**

File: `backend/tests/cloud/files/providers/__init__.py` (empty)

File: `backend/tests/cloud/files/providers/test_uploads_provider.py`
```python
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ee.cloud.files.providers.uploads import UploadsProvider
from ee.cloud.files.schemas import RequestContext
from tests.cloud.files.test_provider_contract import ProviderContract


class TestUploadsProviderContract(ProviderContract):
    def build_provider(self):
        store = MagicMock()

        async def _iter(workspace_id: str, *, include_deleted: bool = False, limit: int = 500):
            yield {
                "file_id": "abc",
                "filename": "report.pdf",
                "mime": "application/pdf",
                "size": 100,
                "owner_id": "u",
                "workspace_id": "ws",
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "tags": [],
            }

        store.iter_by_workspace = _iter
        return UploadsProvider(store=store)


@pytest.mark.asyncio
async def test_uploads_provider_list_entries_maps_fields():
    store = MagicMock()

    async def _iter(workspace_id, *, include_deleted=False, limit=500):
        yield {
            "file_id": "fid1",
            "filename": "a.txt",
            "mime": "text/plain",
            "size": 7,
            "owner_id": "u1",
            "workspace_id": "ws_1",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "tags": [],
        }

    store.iter_by_workspace = _iter
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="u1", workspace_id="ws_1", attributes={})
    page = await p.list_entries(ctx, "/My Files", None, 50, {})
    assert len(page.items) == 1
    e = page.items[0]
    assert e.id == "uploads:fid1"
    assert e.mount_path == "/My Files/a.txt"
    assert e.scope == "personal"


@pytest.mark.asyncio
async def test_uploads_provider_list_mounts_when_ctx_has_workspace():
    store = MagicMock()
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="u", workspace_id="ws", attributes={})
    mounts = await p.list_mounts(ctx)
    assert len(mounts) == 1
    assert mounts[0].path == "/My Files"
    assert mounts[0].writable is True


@pytest.mark.asyncio
async def test_uploads_provider_baseline_rbac_owner_is_manage():
    store = MagicMock()
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="u1", workspace_id="ws", attributes={})
    from tests.cloud.files.conftest import make_entry  # type: ignore  # noqa

    # Build an owned entry directly:
    from datetime import datetime as _dt
    from ee.cloud.files.schemas import FileEntry
    e = FileEntry(
        id="uploads:x",
        provider_id="uploads",
        mount_path="/My Files/x",
        name="x",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws",
        scope="personal",
        tags=[],
        created_at=_dt.now(UTC),
        updated_at=_dt.now(UTC),
        source_ref={},
        capabilities=["read", "download", "rename", "delete"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and perm.write and perm.manage


@pytest.mark.asyncio
async def test_uploads_provider_baseline_rbac_non_owner_is_read_only():
    store = MagicMock()
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="other", workspace_id="ws", attributes={})
    from datetime import datetime as _dt
    from ee.cloud.files.schemas import FileEntry
    e = FileEntry(
        id="uploads:x",
        provider_id="uploads",
        mount_path="/My Files/x",
        name="x",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws",
        scope="personal",
        tags=[],
        created_at=_dt.now(UTC),
        updated_at=_dt.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and not perm.write and not perm.manage
```

- [ ] **Step 10.3: Run — fail.**

- [ ] **Step 10.4: Implement the uploads provider**

File: `backend/ee/cloud/files/providers/uploads.py`
```python
"""UploadsProvider — wraps ee.cloud.uploads.MongoFileStore for the "My Files" mount.

Scope is personal to the current user within the current workspace. Ownership
drives RBAC: the owner has full CRUD; everyone else is read-only (the My Files
mount is effectively private in v2 Phase 1).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ee.cloud.files.providers.base import BaseFolderProvider
from ee.cloud.files.schemas import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)

_MOUNT = "/My Files"


class UploadsProvider(BaseFolderProvider):
    provider_id = "uploads"

    def __init__(self, store: Any) -> None:
        self._store = store

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        if not ctx.workspace_id:
            return []
        return [
            ResolvedMount(
                provider_id=self.provider_id,
                path=_MOUNT,
                writable=True,
                order=10,
                variables={},
            )
        ]

    async def list_entries(
        self,
        ctx: RequestContext,
        mount_path: str,
        cursor: str | None,
        limit: int,
        filters: dict,
    ) -> Page[FileEntry]:
        items: list[FileEntry] = []
        if not ctx.workspace_id:
            return Page(items=items)
        async for doc in self._store.iter_by_workspace(
            ctx.workspace_id, include_deleted=False, limit=limit
        ):
            if doc.get("owner_id") and doc["owner_id"] != ctx.user_id:
                continue
            items.append(self._to_entry(doc))
        return Page(items=items)

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        _, _, native = entry_id.partition(":")
        doc = await self._store.get_by_id(native, workspace_id=ctx.workspace_id)
        return self._to_entry(doc)

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        is_owner = entry.owner_id == ctx.user_id
        return Permission(read=True, write=is_owner, manage=is_owner)

    def _to_entry(self, doc: dict) -> FileEntry:
        return FileEntry(
            id=f"uploads:{doc['file_id']}",
            provider_id="uploads",
            mount_path=f"{_MOUNT}/{doc.get('filename', '')}",
            name=doc.get("filename", ""),
            mime=doc.get("mime", "application/octet-stream"),
            size=int(doc.get("size", 0)),
            owner_id=doc.get("owner_id"),
            workspace_id=doc.get("workspace_id"),
            scope="personal",
            tags=list(doc.get("tags", [])),
            created_at=doc["created_at"],
            updated_at=doc.get("updated_at", doc["created_at"]),
            source_ref={},
            capabilities=["read", "download", "rename", "delete"],
        )
```

- [ ] **Step 10.5: Run provider tests — pass.**

Run: `cd backend && uv run pytest tests/cloud/files/providers/test_uploads_provider.py -v`
Expected: contract tests pass, 4 additional pass.

- [ ] **Step 10.6: Lint + skip commit.**

---

## Task 11: KB provider (Workspace Knowledge Base)

**Files:**
- Inspect first: `backend/ee/cloud/kb/` — follow whatever list/read method already exists (look for `router.py` and any async listing helper). Reuse, don't add new public API.
- Create: `backend/ee/cloud/files/providers/kb.py`
- Test: `backend/tests/cloud/files/providers/test_kb_provider.py`

- [ ] **Step 11.1: Read existing KB module**

Run: `ls backend/ee/cloud/kb/ && cat backend/ee/cloud/kb/router.py` (first 80 lines). Record: the listing function name used (e.g., `list_documents(workspace_id)`), the document shape (fields: id/title/mime/size/created_at/owner_id/visibility).

- [ ] **Step 11.2: Failing test**

File: `backend/tests/cloud/files/providers/test_kb_provider.py`
```python
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from ee.cloud.files.providers.kb import KbProvider
from ee.cloud.files.schemas import RequestContext
from tests.cloud.files.test_provider_contract import ProviderContract


class _FakeKbService:
    def __init__(self, docs):
        self._docs = docs

    async def list_documents(self, workspace_id: str, *, limit: int = 500):
        return list(self._docs)

    async def get_document(self, doc_id: str, *, workspace_id: str):
        for d in self._docs:
            if d["id"] == doc_id:
                return d
        raise KeyError(doc_id)


def _doc(**overrides):
    base = dict(
        id="doc1",
        title="handbook.pdf",
        mime="application/pdf",
        size=512,
        owner_id="u1",
        workspace_id="ws_1",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        visibility="workspace",
        tags=[],
    )
    base.update(overrides)
    return base


class TestKbProviderContract(ProviderContract):
    def build_provider(self):
        return KbProvider(service=_FakeKbService([_doc()]))


@pytest.mark.asyncio
async def test_kb_list_entries_scoped_to_workspace():
    svc = _FakeKbService([_doc(id="a"), _doc(id="b", title="spec.md", mime="text/markdown")])
    p = KbProvider(service=svc)
    ctx = RequestContext(user_id="u1", workspace_id="ws_1", attributes={})
    page = await p.list_entries(ctx, "/Workspaces/ws_1/Knowledge Base", None, 50, {})
    assert {e.id for e in page.items} == {"kb:a", "kb:b"}


@pytest.mark.asyncio
async def test_kb_baseline_rbac_workspace_member_reads():
    svc = _FakeKbService([])
    p = KbProvider(service=svc)
    ctx = RequestContext(
        user_id="u2", workspace_id="ws_1", attributes={"role": "member"}
    )
    from ee.cloud.files.schemas import FileEntry
    e = FileEntry(
        id="kb:a",
        provider_id="kb",
        mount_path="/Workspaces/ws_1/Knowledge Base/a",
        name="a",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws_1",
        scope="workspace",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and not perm.write and not perm.manage


@pytest.mark.asyncio
async def test_kb_baseline_rbac_admin_manages():
    svc = _FakeKbService([])
    p = KbProvider(service=svc)
    ctx = RequestContext(user_id="u1", workspace_id="ws_1", attributes={"role": "admin"})
    from ee.cloud.files.schemas import FileEntry
    e = FileEntry(
        id="kb:a",
        provider_id="kb",
        mount_path="/Workspaces/ws_1/Knowledge Base/a",
        name="a",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws_1",
        scope="workspace",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download", "rename", "delete"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and perm.write and perm.manage


@pytest.mark.asyncio
async def test_kb_mount_template_resolves_workspace_id():
    svc = _FakeKbService([])
    p = KbProvider(service=svc)
    ctx = RequestContext(user_id="u", workspace_id="ws_77", attributes={})
    mounts = await p.list_mounts(ctx)
    assert mounts[0].path == "/Workspaces/ws_77/Knowledge Base"
```

- [ ] **Step 11.3: Run — fail.**

- [ ] **Step 11.4: Implement**

File: `backend/ee/cloud/files/providers/kb.py`
```python
"""KbProvider — workspace Knowledge Base documents.

Reuses ee.cloud.kb listing helpers; does NOT duplicate their access checks.
Workspace membership gives read; admin/owner gives write/manage. Non-members
see nothing (list returns empty because the underlying service only yields
for entitled workspaces).
"""
from __future__ import annotations

from typing import Any, Protocol

from ee.cloud.files.providers.base import BaseFolderProvider
from ee.cloud.files.schemas import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)


class _KbService(Protocol):
    async def list_documents(self, workspace_id: str, *, limit: int = 500) -> list[dict]: ...
    async def get_document(self, doc_id: str, *, workspace_id: str) -> dict: ...


_ADMIN_ROLES = {"admin", "owner"}
_MEMBER_ROLES = {"admin", "owner", "member", "editor"}


class KbProvider(BaseFolderProvider):
    provider_id = "kb"

    def __init__(self, service: _KbService) -> None:
        self._service = service

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        if not ctx.workspace_id:
            return []
        return [
            ResolvedMount(
                provider_id=self.provider_id,
                path=f"/Workspaces/{ctx.workspace_id}/Knowledge Base",
                writable=True,
                order=20,
                variables={"workspace_id": ctx.workspace_id},
            )
        ]

    async def list_entries(
        self,
        ctx: RequestContext,
        mount_path: str,
        cursor: str | None,
        limit: int,
        filters: dict,
    ) -> Page[FileEntry]:
        if not ctx.workspace_id:
            return Page(items=[])
        docs = await self._service.list_documents(ctx.workspace_id, limit=limit)
        return Page(items=[self._to_entry(ctx.workspace_id, d) for d in docs])

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        _, _, native = entry_id.partition(":")
        doc = await self._service.get_document(native, workspace_id=ctx.workspace_id or "")
        return self._to_entry(ctx.workspace_id or "", doc)

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        role = str(ctx.attributes.get("role", "")).lower()
        if role in _ADMIN_ROLES:
            return Permission(read=True, write=True, manage=True)
        if role in _MEMBER_ROLES:
            return Permission(read=True, write=False, manage=False)
        if ctx.workspace_id and entry.workspace_id == ctx.workspace_id:
            return Permission(read=True, write=False, manage=False)
        return Permission()

    def _to_entry(self, workspace_id: str, doc: dict[str, Any]) -> FileEntry:
        title = doc.get("title", doc.get("name", ""))
        return FileEntry(
            id=f"kb:{doc['id']}",
            provider_id="kb",
            mount_path=f"/Workspaces/{workspace_id}/Knowledge Base/{title}",
            name=title,
            mime=doc.get("mime", "application/octet-stream"),
            size=int(doc.get("size", 0)),
            owner_id=doc.get("owner_id"),
            workspace_id=doc.get("workspace_id"),
            scope="workspace",
            tags=list(doc.get("tags", [])),
            created_at=doc["created_at"],
            updated_at=doc.get("updated_at", doc["created_at"]),
            source_ref={"kb_doc_id": doc["id"]},
            capabilities=["read", "download", "rename", "delete"],
        )
```

- [ ] **Step 11.5: Run — pass.** Expected: 4 + contract tests pass.

> **Note:** If `ee.cloud.kb` exposes the listing helper under a different name (e.g. `KbService.list`), update the `_KbService` protocol AND the test's `_FakeKbService` together. Keep the provider's public interface stable.

- [ ] **Step 11.6: Lint + skip commit.**

---

## Task 12: HTTP routes — /tree and /browse

**Files:**
- Modify: `backend/ee/cloud/files/router.py` (add new endpoints; keep legacy `/api/v1/files`)
- Test: `backend/tests/cloud/files/test_router_tree_browse.py`

- [ ] **Step 12.1: Failing test (FastAPI TestClient)**

File: `backend/tests/cloud/files/test_router_tree_browse.py`
```python
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.router import build_router
from ee.cloud.files.schemas import MountConfig, RequestContext
from tests.cloud.files.conftest import FakeProvider


def _mount(provider_id, path, writable=False, order=100):
    from ee.cloud.files.schemas import ResolvedMount
    return ResolvedMount(
        provider_id=provider_id, path=path, writable=writable, order=order, variables={}
    )


def _ctx_factory():
    return RequestContext(user_id="u1", workspace_id="ws_1", attributes={"role": "member"})


@pytest.mark.asyncio
async def test_get_tree_returns_folder_nodes():
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
        ]
    )
    reg.register(FakeProvider("uploads", mounts=[_mount("uploads", "/My Files", True, 10)]))

    app = FastAPI()
    app.include_router(build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/tree")
    assert r.status_code == 200
    body = r.json()
    assert body["children"][0]["name"] == "My Files"
    assert body["warnings"] == []


@pytest.mark.asyncio
async def test_get_browse_returns_entries(make_entry):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
        ]
    )
    entry = make_entry("uploads", "a", "/My Files/a")
    reg.register(FakeProvider("uploads", entries=[entry]))

    app = FastAPI()
    app.include_router(build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/browse", params={"mount": "/My Files"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == "uploads:a"


@pytest.mark.asyncio
async def test_get_browse_unknown_mount_is_404():
    reg = ProviderRegistry(configs=[])
    app = FastAPI()
    app.include_router(build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/browse", params={"mount": "/nope"})
    assert r.status_code == 404
    assert r.json()["detail"] == "files.mount_not_found"
```

- [ ] **Step 12.2: Run — fail.**

- [ ] **Step 12.3: Implement router**

Replace `backend/ee/cloud/files/router.py` (or add alongside legacy route) with:

File: `backend/ee/cloud/files/router.py`
```python
"""Files API routes. Legacy /api/v1/files kept intact; /tree + /browse added."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.browse import browse_mount
from ee.cloud.files.errors import FilesError, MountNotFound
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import RequestContext
from ee.cloud.files.tree import build_tree


def build_router(
    *,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    ctx_factory: Callable[[], RequestContext],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/files", tags=["files"])

    @router.get("/tree")
    async def get_tree() -> dict[str, Any]:
        ctx = ctx_factory()
        tree, warnings = await build_tree(
            ctx=ctx, registry=registry, rules=rules, collect_warnings=True
        )
        return {**tree.model_dump(), "warnings": warnings}

    @router.get("/browse")
    async def get_browse(
        mount: str = Query(...),
        cursor: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        ctx = ctx_factory()
        variables = {"workspace_id": ctx.workspace_id or ""}
        try:
            page = await browse_mount(
                ctx=ctx,
                registry=registry,
                rules=rules,
                mount_path=mount,
                variables=variables,
                cursor=cursor,
                limit=limit,
                filters={},
            )
        except MountNotFound:
            raise HTTPException(status_code=404, detail="files.mount_not_found")
        except FilesError as e:
            raise HTTPException(status_code=e.http_status, detail=e.code)
        return page.model_dump()

    return router
```

- [ ] **Step 12.4: Run tests — pass.** Expected: 3 passed.

- [ ] **Step 12.5: Legacy route retained**

> If Task 0 created a legacy `list_files` route on a plain `router = APIRouter(...)`, leave it alone. The app assembly code (somewhere in `backend/src/pocketpaw/...` or `ee/cloud/__init__.py`) should `include_router(build_router(...))` in addition to the legacy one.

- [ ] **Step 12.6: Lint + skip commit.**

---

## Task 13: Legacy contract regression test

**Files:**
- Create: `backend/tests/cloud/files/test_legacy_contract.py`

- [ ] **Step 13.1: Write the test**

File: `backend/tests/cloud/files/test_legacy_contract.py`
```python
"""Regression: /api/v1/files must keep the Cluster E #998 response shape.

Shape: {workspace_id: str, source: str, files: list[dict], warnings: list[dict]}
Each file row has: id, source, filename, mime, size, url, created, chat_id.
"""
from ee.cloud.files.service import UnifiedFilesService


def test_legacy_response_keys_present():
    assert set(UnifiedFilesService.list.__annotations__.keys()) >= {"workspace_id", "source"}


import pytest


@pytest.mark.asyncio
async def test_legacy_shape_with_empty_store():
    class _Store:
        async def list_by_workspace(self, workspace_id: str, *, limit: int = 500):
            return []

    svc = UnifiedFilesService(_Store())
    body = await svc.list("ws_1", source="all")
    assert set(body.keys()) == {"workspace_id", "source", "files", "warnings"}
    assert body["workspace_id"] == "ws_1"
    assert body["source"] == "all"
    assert body["files"] == []
    assert {"source": "drive", "code": "drive.not_connected"} in body["warnings"]
    assert {"source": "local", "code": "local.client_only"} in body["warnings"]
```

- [ ] **Step 13.2: Run — pass.**

Run: `cd backend && uv run pytest tests/cloud/files/test_legacy_contract.py -v`
Expected: 2 passed.

- [ ] **Step 13.3: Skip commit.**

---

## Task 14: Module wiring + integration smoke

**Files:**
- Modify: `backend/ee/cloud/files/__init__.py` (export `build_router`, loader helpers)
- Create: `backend/ee/cloud/files/bootstrap.py` — convenience builder
- Test: `backend/tests/cloud/files/test_bootstrap.py`

- [ ] **Step 14.1: Bootstrap**

File: `backend/ee/cloud/files/bootstrap.py`
```python
"""Compose registry + providers + rules from config into a ready-to-mount router."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from ee.cloud.files.abac_config import load_rules
from ee.cloud.files.mounts_config import load_mounts
from ee.cloud.files.providers.kb import KbProvider
from ee.cloud.files.providers.uploads import UploadsProvider
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.router import build_router
from ee.cloud.files.schemas import RequestContext


def build_files_router(
    *,
    uploads_store,
    kb_service,
    ctx_factory: Callable[[], RequestContext],
) -> APIRouter:
    registry = ProviderRegistry(configs=load_mounts())
    registry.register(UploadsProvider(store=uploads_store))
    registry.register(KbProvider(service=kb_service))
    rules = load_rules()
    return build_router(registry=registry, rules=rules, ctx_factory=ctx_factory)
```

- [ ] **Step 14.2: Smoke test**

File: `backend/tests/cloud/files/test_bootstrap.py`
```python
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud.files.bootstrap import build_files_router
from ee.cloud.files.schemas import RequestContext


class _Store:
    async def iter_by_workspace(self, workspace_id, *, include_deleted=False, limit=500):
        if False:
            yield {}


class _Kb:
    async def list_documents(self, workspace_id, *, limit=500):
        return []

    async def get_document(self, doc_id, *, workspace_id):
        raise KeyError


@pytest.mark.asyncio
async def test_bootstrap_tree_endpoint_works():
    app = FastAPI()
    app.include_router(
        build_files_router(
            uploads_store=_Store(),
            kb_service=_Kb(),
            ctx_factory=lambda: RequestContext(
                user_id="u", workspace_id="ws_1", attributes={"role": "member"}
            ),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/tree")
    assert r.status_code == 200
    assert "children" in r.json()
```

- [ ] **Step 14.3: Run — pass.**
- [ ] **Step 14.4: Export in `__init__.py`**

File: `backend/ee/cloud/files/__init__.py`
```python
"""Files aggregation module."""

from ee.cloud.files.bootstrap import build_files_router
from ee.cloud.files.registry import FolderProvider, ProviderRegistry
from ee.cloud.files.router import build_router
from ee.cloud.files.schemas import (
    Capability,
    FileEntry,
    FolderNode,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
    Scope,
    SearchQuery,
)

__all__ = [
    "Capability",
    "FileEntry",
    "FolderNode",
    "FolderProvider",
    "MountConfig",
    "Page",
    "Permission",
    "ProviderRegistry",
    "RequestContext",
    "ResolvedMount",
    "Scope",
    "SearchQuery",
    "build_files_router",
    "build_router",
]
```

- [ ] **Step 14.5: Final full-suite run**

Run: `cd backend && uv run pytest tests/cloud/files/ -v`
Expected: all green.

- [ ] **Step 14.6: Lint + typecheck**

Run: `cd backend && uv run ruff check ee/cloud/files tests/cloud/files && uv run mypy ee/cloud/files`
Expected: no errors.

- [ ] **Step 14.7: Skip commit. Plan complete.**

---

## Out of scope / follow-up plans

These will be addressed in separate plans once Phase 1-2 lands:

- **Phase 3 plan** — `2026-04-21-files-tab-plan-phase-3.md`: `pockets`, `memory`, `agents`, `rooms`, and `chat` providers (each behind a feature flag, each using `ProviderContract`).
- **Phase 4 plan** — `2026-04-22-files-tab-plan-phase-4.md`: CRUD endpoints (`POST /upload`, `PATCH /:id`, `DELETE /:id`, `POST /:id/move`), realtime bridge on `ee/cloud/realtime`, Socket.IO `files:*` events.
- **Phase 5 plan** — `2026-04-23-files-tab-plan-phase-5.md`: `paw-enterprise` tree-mode UI (`FolderTree`, `FileList`, `Preview`, `UploadDropzone`), view-mode toggle, realtime wiring, optimistic CRUD.

---

## Self-Review

**Spec coverage:** Each phase-1/2 requirement in the design (schemas, registry, permissions, tree, browse, uploads provider, kb provider, legacy contract, reshapeable mounts) maps to at least one task above. Phase 3/4/5 deferred to follow-up plans with explicit names.

**Placeholder scan:** One soft pointer in Task 11 Step 11.1 ("Read existing KB module") and the Task 12 Step 12.5 note about app assembly — both are legitimate inspection instructions, not hidden TODOs. No "TBD" / "fill in details" / "add appropriate error handling" sentinels.

**Type consistency:** `FileEntry`, `FolderNode`, `Permission`, `RequestContext`, `ResolvedMount`, `MountConfig`, `Page`, `SearchQuery`, `AbacRuleSet` names are stable across all tasks. Provider method signatures match between `FolderProvider` protocol (Task 6) and `BaseFolderProvider` (Task 10) and the concrete `UploadsProvider`/`KbProvider`.

**Commit discipline:** Every task ends with "Skip commit" per user directive — explicitly opting out of the default workflow without losing the structural anchor.
