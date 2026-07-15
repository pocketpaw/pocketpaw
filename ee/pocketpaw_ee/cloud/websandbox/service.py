# service.py — Web Cursor Sandbox Registry business logic (the auth oracle).
# Created 2026-07-15 (WC-1, feat/websandbox-registry).
#
# This module IS the repository (ee/cloud Rule 1) and the ONLY module allowed to
# import its own Beanie doc (Rule 2). Every read carries a tenant filter
# (Rule 7); every mutation validates at entry (Rule 6) and emits an event
# (Rule 9). Errors are CloudError subclasses, never HTTPException (Rule 10).
#
# The point of the slice is ``authorize_sandbox``: fail-closed cross-tenant
# authorization that emits a high-severity audit event BEFORE it raises, so a
# denied access attempt is always on the record. Every later Web Cursor slice
# (session WS, editor RPC, git broker) calls this before touching a sandbox.
#
# Changed 2026-07-15 (WC-2, feat/websandbox-vm-provision): added the two
# system-level reaper support functions ``list_reapable_sandboxes`` (global-read)
# and ``mark_reaped`` (global-write). The idle-TTL reaper in ``provision.py``
# drives them; they live here so the service stays the ONLY module that touches
# the WebSandbox Beanie doc (Rule 2).
from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    WebSandboxRegistered,
    WebSandboxStatusChanged,
)
from pocketpaw_ee.cloud.models.web_sandbox import WebSandbox as _WebSandboxDoc
from pocketpaw_ee.cloud.websandbox.domain import WebSandboxId, WebSandboxView
from pocketpaw_ee.cloud.websandbox.dto import (
    CreateSandboxRequest,
    UpdateStatusRequest,
    WebSandboxResponse,
)

logger = logging.getLogger(__name__)

_FORBIDDEN_CODE = "websandbox.forbidden"
# The action string the denial audit event is recorded under. Later slices and
# any SIEM webhook key off this exact value to alert on cross-tenant probing.
_CROSS_TENANT_ACTION = "vm.cross_tenant_denied"


def _doc_to_view(doc: _WebSandboxDoc) -> WebSandboxView:
    """Map a persisted, tenant-checked row to its read model."""
    return WebSandboxView(
        id=WebSandboxId(str(doc.id)),
        workspace_id=doc.workspace_id,
        user_id=doc.user_id,
        repo=doc.repo,
        status=doc.status,
        sandbox_id=doc.sandbox_id,
        installation_id=doc.installation_id,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def view_to_wire(view: WebSandboxView) -> WebSandboxResponse:
    """Map a view to the camelCase wire response (Rule 8 — mapping lives here)."""
    return WebSandboxResponse(
        id=view.id,
        workspaceId=view.workspace_id,
        userId=view.user_id,
        repo=view.repo,
        status=view.status,
        sandboxId=view.sandbox_id,
        installationId=view.installation_id,
        createdAt=view.created_at.isoformat(),
        updatedAt=view.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# The security core.
# ---------------------------------------------------------------------------


async def authorize_sandbox(
    workspace_id: str,
    user_id: str,
    sandbox_id: str,
) -> WebSandboxView:
    """Fail-closed authorization for access to a provisioned sandbox.

    Looks the sandbox up by its Daytona ``sandbox_id``, tenant-filtered to the
    caller's ``workspace_id`` (Rule 7). Access is granted ONLY when a row
    exists in the caller's workspace AND is owned by the caller's ``user_id``.

    On any denial — the sandbox belongs to another workspace (the tenant
    filter returns nothing), or another user in the same workspace — a
    high-severity ``vm.cross_tenant_denied`` audit event is emitted BEFORE the
    ``Forbidden`` is raised. Non-existence and wrong-owner both raise the same
    ``Forbidden``, so existence never leaks through a differentiated error.
    """
    doc = await _WebSandboxDoc.find_one(
        {"sandbox_id": sandbox_id, "workspace_id": workspace_id}  # Rule 7 tenant filter
    )
    if doc is None or doc.user_id != user_id:
        await _record_cross_tenant_denial(
            workspace_id=workspace_id,
            user_id=user_id,
            sandbox_id=sandbox_id,
            found=doc is not None,
        )
        raise Forbidden(_FORBIDDEN_CODE, "You do not have access to this sandbox")
    return _doc_to_view(doc)


async def _record_cross_tenant_denial(
    *,
    workspace_id: str,
    user_id: str,
    sandbox_id: str,
    found: bool,
) -> None:
    """Emit the fail-closed denial audit event (fire-and-forget, never raises).

    Mirrors the ART-2 fail-closed pattern: ``audit_service.record`` swallows its
    own write failures, so an audit outage can never turn a denial into a leak.
    """
    from pocketpaw_ee.cloud.audit import service as audit_service

    await audit_service.record(
        workspace_id=workspace_id,
        actor_id=user_id,
        action=_CROSS_TENANT_ACTION,
        target_type="web_sandbox",
        target_id=sandbox_id,
        metadata={
            # ``found`` distinguishes "wrong owner, same workspace" (True) from
            # "not in this workspace at all" (False) for the forensic trail —
            # the caller still gets the same opaque Forbidden either way.
            "reason": "wrong_owner" if found else "cross_workspace",
            "sandbox_id": sandbox_id,
        },
    )


# ---------------------------------------------------------------------------
# CRUD the later slices authorize against. Every read is tenant-filtered.
# ---------------------------------------------------------------------------


async def create_sandbox(
    workspace_id: str,
    user_id: str,
    body: CreateSandboxRequest | dict,
) -> WebSandboxView:
    """Register (or re-register) the sandbox for a (workspace, user, repo).

    Idempotent on the registry key: a second call for the same
    (workspace, user, repo) updates the existing row rather than minting a
    duplicate, keeping the one-sandbox-per-key invariant even where the unique
    index isn't enforced (e.g. mongomock).
    """
    body = CreateSandboxRequest.model_validate(body)

    existing = await _WebSandboxDoc.find_one(
        {"workspace_id": workspace_id, "user_id": user_id, "repo": body.repo}  # Rule 7
    )
    if existing is not None:
        existing.status = body.status
        if body.sandbox_id is not None:
            existing.sandbox_id = body.sandbox_id
        if body.installation_id is not None:
            existing.installation_id = body.installation_id
        existing.updated_at = datetime.now(UTC)
        await existing.save()
        doc = existing
    else:
        doc = _WebSandboxDoc(
            workspace_id=workspace_id,
            user_id=user_id,
            repo=body.repo,
            sandbox_id=body.sandbox_id,
            status=body.status,
            installation_id=body.installation_id,
        )
        await doc.insert()

    await emit(
        WebSandboxRegistered(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "repo": doc.repo,
                "status": doc.status,
            }
        )
    )
    return _doc_to_view(doc)


async def get_sandbox(
    workspace_id: str,
    user_id: str,
    row_id: str,
) -> WebSandboxView:
    """Read one sandbox row by its registry id, tenant- and owner-scoped.

    Raises ``NotFound`` when no row matches the caller's workspace + user — a
    row owned by another tenant is indistinguishable from a missing one.
    """
    doc = await _read_owned(workspace_id, user_id, row_id)
    if doc is None:
        raise NotFound("web_sandbox", row_id)
    return _doc_to_view(doc)


async def update_status(
    workspace_id: str,
    user_id: str,
    row_id: str,
    body: UpdateStatusRequest | dict,
) -> WebSandboxView:
    """Advance a sandbox's lifecycle state (optionally binding its Daytona id).

    Tenant- and owner-scoped: a caller can only move a sandbox they own.
    """
    body = UpdateStatusRequest.model_validate(body)

    doc = await _read_owned(workspace_id, user_id, row_id)
    if doc is None:
        raise NotFound("web_sandbox", row_id)

    doc.status = body.status
    if body.sandbox_id is not None:
        doc.sandbox_id = body.sandbox_id
    doc.updated_at = datetime.now(UTC)
    await doc.save()

    await emit(
        WebSandboxStatusChanged(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "repo": doc.repo,
                "status": doc.status,
            }
        )
    )
    return _doc_to_view(doc)


async def list_sandboxes(workspace_id: str, user_id: str) -> list[WebSandboxView]:
    """List every sandbox owned by the caller, newest first.

    Tenant-filtered by ``workspace_id`` AND owner-filtered by ``user_id`` so
    one user never sees another's sandboxes even within a shared workspace.
    """
    docs = (
        await _WebSandboxDoc.find(
            {"workspace_id": workspace_id, "user_id": user_id}  # Rule 7 tenant filter
        )
        .sort([("created_at", -1)])  # mongomock-safe: straight find + sort, no aggregate
        .to_list()
    )
    return [_doc_to_view(d) for d in docs]


# ---------------------------------------------------------------------------
# System-level reaper support (WC-2). The idle-TTL reaper is a background sweep
# that acts ACROSS tenants, so these two functions are deliberately NOT
# tenant-scoped — they carry an explicit ``# global-read`` / ``# global-write``
# marker per Rule 7. They stay HERE (not in provision.py) so the service remains
# the only module that touches the Beanie doc (Rule 2).
# ---------------------------------------------------------------------------


async def list_reapable_sandboxes(
    cutoff: datetime,
    statuses: tuple[str, ...] = ("ready", "opening"),
) -> list[WebSandboxView]:
    """List sandboxes idle since before ``cutoff`` (system reaper candidate set).

    "Idle" == ``updated_at`` older than the cutoff, in a still-live state
    (``ready`` / ``opening``). Not tenant-scoped: the reaper reclaims leaked VMs
    for every tenant. WC-3 will refresh ``updated_at`` on live WebSocket traffic;
    until then age alone marks a session as dropped.
    """
    docs = await _WebSandboxDoc.find(
        # global-read: the idle-TTL reaper sweeps every tenant's sandboxes; this
        # is a system sweep, not a per-request read, so no workspace filter.
        {"status": {"$in": list(statuses)}, "updated_at": {"$lt": cutoff}}
    ).to_list()
    return [_doc_to_view(d) for d in docs]


async def mark_reaped(row_id: str) -> WebSandboxView | None:
    """Flip a sandbox row to ``reaped`` (system reaper terminal), returning the
    updated view — or ``None`` if the row vanished mid-sweep.

    Resolves the row by id ONLY (no owner filter) because the reaper is a system
    actor, not the owning user. The emitted ``WebSandboxStatusChanged`` still
    carries the row's own tenancy so downstream fan-out stays scoped.
    """
    from beanie import PydanticObjectId

    try:
        oid = PydanticObjectId(row_id)
    except Exception:  # noqa: BLE001 — a bad id is simply "nothing to reap"
        return None
    # global-write: system reaper terminal state; resolve by id across tenants.
    doc = await _WebSandboxDoc.find_one({"_id": oid})
    if doc is None:
        return None
    doc.status = "reaped"
    doc.updated_at = datetime.now(UTC)
    await doc.save()

    await emit(
        WebSandboxStatusChanged(
            data={
                "id": str(doc.id),
                "workspace_id": doc.workspace_id,
                "user_id": doc.user_id,
                "repo": doc.repo,
                "status": doc.status,
            }
        )
    )
    return _doc_to_view(doc)


async def _read_owned(
    workspace_id: str,
    user_id: str,
    row_id: str,
) -> _WebSandboxDoc | None:
    """Tenant- + owner-scoped fetch by registry id. Returns None if not owned.

    An unparseable ``row_id`` yields None (treated as not-found) rather than
    raising, so a malformed id can't leak a distinct error shape.
    """
    from beanie import PydanticObjectId

    try:
        oid = PydanticObjectId(row_id)
    except Exception:  # noqa: BLE001 — a bad id is simply "no such owned row"
        return None
    return await _WebSandboxDoc.find_one(
        {"_id": oid, "workspace_id": workspace_id, "user_id": user_id}  # Rule 7
    )


__all__ = [
    "authorize_sandbox",
    "create_sandbox",
    "get_sandbox",
    "list_reapable_sandboxes",
    "list_sandboxes",
    "mark_reaped",
    "update_status",
    "view_to_wire",
]
