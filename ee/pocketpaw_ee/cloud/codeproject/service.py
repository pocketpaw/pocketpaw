# service.py — Code Mode durable-project registry business logic (CM-2a).
# Created 2026-07-16 (feat/code-mode): the DURABLE half of Code Mode's two-
# lifecycle model. This module IS the repository (ee/cloud Rule 1) and the ONLY
# module allowed to import the CodeProject Beanie doc (Rule 2). Every read carries
# a tenant + owner filter (Rule 7); every mutation validates at entry (Rule 6) and
# emits an event or is marked ``# no-event:`` (Rule 9). Errors are CloudError
# subclasses, never HTTPException (Rule 10).
#
# The project is the deep-linkable, reap-surviving identity behind ``/code/<id>``;
# the ephemeral Daytona sandbox (a WebSandbox row) is bound via
# ``current_sandbox_id`` and resolved lazily by ``codeproject/lifecycle.open_project``
# (reuse the bound row if it's live, else provision a fresh one and rebind). The
# orchestration lives in lifecycle.py so this module stays the sole doc writer.
from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import CodeProjectCreated, CodeProjectOpened
from pocketpaw_ee.cloud.codeproject.domain import CodeProjectId, CodeProjectView
from pocketpaw_ee.cloud.codeproject.dto import CodeProjectResponse, CreateProjectRequest
from pocketpaw_ee.cloud.models.code_project import CodeProject as _CodeProjectDoc

logger = logging.getLogger(__name__)


def _short_name(repo: str) -> str:
    """Derive a friendly default display name from a repo URL / "owner/repo".

    Strips a trailing ``.git`` and any path, leaving the last path segment (the
    repo's short name). Falls back to the raw value if there's nothing to strip.
    """
    trimmed = repo.strip().rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    tail = trimmed.rsplit("/", 1)[-1]
    return tail or trimmed


def _doc_to_view(doc: _CodeProjectDoc) -> CodeProjectView:
    """Map a persisted, tenant-checked row to its read model."""
    return CodeProjectView(
        id=CodeProjectId(str(doc.id)),
        workspace_id=doc.workspace_id,
        user_id=doc.user_id,
        name=doc.name,
        provider=doc.provider,
        repo=doc.repo,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        snapshot_file_id=doc.snapshot_file_id,
        current_sandbox_id=doc.current_sandbox_id,
        last_opened_at=doc.last_opened_at,
    )


def view_to_wire(view: CodeProjectView) -> CodeProjectResponse:
    """Map a view to the camelCase wire response (Rule 8 — mapping lives here)."""
    return CodeProjectResponse(
        id=view.id,
        workspaceId=view.workspace_id,
        userId=view.user_id,
        name=view.name,
        provider=view.provider,
        repo=view.repo,
        snapshotFileId=view.snapshot_file_id,
        currentSandboxId=view.current_sandbox_id,
        lastOpenedAt=view.last_opened_at.isoformat() if view.last_opened_at else None,
        createdAt=view.created_at.isoformat(),
        updatedAt=view.updated_at.isoformat(),
    )


async def create_project(
    workspace_id: str,
    user_id: str,
    body: CreateProjectRequest | dict,
) -> CodeProjectView:
    """Register (or return) the durable project for a (workspace, user, provider, repo).

    Idempotent on the registry key: a second create for the same repo returns the
    EXISTING project unchanged rather than minting a duplicate — a returning user
    lands back on the same ``/code/<id>``. Only an actual insert emits
    ``CodeProjectCreated`` (an idempotent hit is not a mutation).
    """
    body = CreateProjectRequest.model_validate(body)

    existing = await _CodeProjectDoc.find_one(
        {  # Rule 7 tenant + owner filter
            "workspace_id": workspace_id,
            "user_id": user_id,
            "provider": body.provider,
            "repo": body.repo,
        }
    )
    if existing is not None:
        # no-event: idempotent hit — nothing was written, so nothing to announce.
        return _doc_to_view(existing)

    doc = _CodeProjectDoc(
        workspace_id=workspace_id,
        user_id=user_id,
        name=body.name or _short_name(body.repo),
        provider=body.provider,
        repo=body.repo,
    )
    await doc.insert()

    await emit(
        CodeProjectCreated(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "repo": doc.repo,
                "provider": doc.provider,
            }
        )
    )
    return _doc_to_view(doc)


async def list_projects(workspace_id: str, user_id: str) -> list[CodeProjectView]:
    """List every project owned by the caller, most-recently-touched first.

    Tenant-filtered by ``workspace_id`` AND owner-filtered by ``user_id`` so one
    user never sees another's projects even within a shared workspace. Sorted by
    ``updated_at`` desc (bumped on every open) — a mongomock-safe find + sort, no
    aggregate.
    """
    docs = (
        await _CodeProjectDoc.find(
            {"workspace_id": workspace_id, "user_id": user_id}  # Rule 7 tenant filter
        )
        .sort([("updated_at", -1)])
        .to_list()
    )
    return [_doc_to_view(d) for d in docs]


async def get_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
) -> CodeProjectView:
    """Read one project by its registry id, tenant- and owner-scoped.

    Raises ``NotFound`` when no row matches the caller's workspace + user — a row
    owned by another tenant is indistinguishable from a missing one.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)
    return _doc_to_view(doc)


async def bind_current_sandbox(
    workspace_id: str,
    user_id: str,
    project_id: str,
    sandbox_row_id: str,
) -> CodeProjectView:
    """Bind the project to its CURRENT ephemeral sandbox and stamp last-opened.

    Tenant- and owner-scoped: a caller can only bind a sandbox onto a project they
    own. ``sandbox_row_id`` is a WebSandbox registry id.
    ``codeproject/lifecycle.open_project`` calls this after resolving (reusing or
    provisioning) the sandbox, so the durable project always points at the latest
    runtime. Emits ``CodeProjectOpened`` — the "project now has a live sandbox"
    transition a projects-grid fan-out reacts to.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)

    now = datetime.now(UTC)
    doc.current_sandbox_id = sandbox_row_id
    doc.last_opened_at = now
    doc.updated_at = now
    await doc.save()

    await emit(
        CodeProjectOpened(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "repo": doc.repo,
                "sandbox_id": sandbox_row_id,
            }
        )
    )
    return _doc_to_view(doc)


async def _read_owned(
    workspace_id: str,
    user_id: str,
    project_id: str,
) -> _CodeProjectDoc | None:
    """Tenant- + owner-scoped fetch by registry id. Returns None if not owned.

    An unparseable ``project_id`` yields None (treated as not-found) rather than
    raising, so a malformed id can't leak a distinct error shape.
    """
    from beanie import PydanticObjectId

    try:
        oid = PydanticObjectId(project_id)
    except Exception:  # noqa: BLE001 — a bad id is simply "no such owned row"
        return None
    return await _CodeProjectDoc.find_one(
        {"_id": oid, "workspace_id": workspace_id, "user_id": user_id}  # Rule 7
    )


__all__ = [
    "bind_current_sandbox",
    "create_project",
    "get_project",
    "list_projects",
    "view_to_wire",
]
