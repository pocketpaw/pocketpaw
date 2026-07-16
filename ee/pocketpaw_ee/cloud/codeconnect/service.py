# service.py — Code Mode GitHub connection registry business logic (CM-3).
# Created 2026-07-16 (feat/code-mode): this module IS the repository (ee/cloud
# Rule 1) and the ONLY module that imports the CodeConnection Beanie doc (Rule 2).
# Every read carries a tenant + owner filter (Rule 7); every mutation validates its
# inputs and emits an event or is marked ``# no-event:`` (Rule 9). Errors are
# CloudError subclasses, never HTTPException (Rule 10).
#
# The GitHub-touching orchestration (install-URL building, callback handling, repo
# listing via the App client) lives in ``codeconnect/connect.py`` so this module
# stays the sole doc writer and never imports an HTTP client.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import CodeConnectionCreated
from pocketpaw_ee.cloud.codeconnect.domain import CodeConnectionId, CodeConnectionView
from pocketpaw_ee.cloud.codeconnect.dto import CodeConnectionResponse
from pocketpaw_ee.cloud.models.code_connection import CodeConnection as _CodeConnectionDoc

logger = logging.getLogger(__name__)

_PROVIDER = "github"


def _doc_to_view(doc: _CodeConnectionDoc) -> CodeConnectionView:
    """Map a persisted, tenant-checked row to its read model."""
    return CodeConnectionView(
        id=CodeConnectionId(str(doc.id)),
        workspace_id=doc.workspace_id,
        user_id=doc.user_id,
        provider=doc.provider,
        installation_id=doc.installation_id,
        account_login=doc.account_login,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def view_to_wire(view: CodeConnectionView) -> CodeConnectionResponse:
    """Map a view to the camelCase wire response (Rule 8 — mapping lives here)."""
    return CodeConnectionResponse(
        id=view.id,
        workspaceId=view.workspace_id,
        userId=view.user_id,
        provider=view.provider,
        installationId=view.installation_id,
        accountLogin=view.account_login,
        createdAt=view.created_at.isoformat(),
        updatedAt=view.updated_at.isoformat(),
    )


async def save_connection(
    workspace_id: str,
    user_id: str,
    installation_id: str,
    account_login: str | None = None,
    provider: str = _PROVIDER,
) -> CodeConnectionView:
    """Persist (or refresh) a GitHub connection for a (workspace, user, installation).

    Idempotent on the registry key: re-installing / a repeated callback for the
    same installation refreshes the existing row (and any newly-known
    ``account_login``) rather than minting a duplicate. Only an actual insert emits
    ``CodeConnectionCreated`` (an idempotent refresh is not a new connection).
    """
    installation_id = (installation_id or "").strip()
    if not installation_id:
        # Defensive: the router validates, but a bus/internal caller might not.
        raise NotFound("code_connection", installation_id)

    existing = await _CodeConnectionDoc.find_one(
        {  # Rule 7 tenant + owner filter
            "workspace_id": workspace_id,
            "user_id": user_id,
            "provider": provider,
            "installation_id": installation_id,
        }
    )
    if existing is not None:
        if account_login and existing.account_login != account_login:
            existing.account_login = account_login
            existing.updated_at = datetime.now(UTC)
            await existing.save()
        # no-event: an idempotent refresh is not a new connection.
        return _doc_to_view(existing)

    doc = _CodeConnectionDoc(
        workspace_id=workspace_id,
        user_id=user_id,
        provider=provider,
        installation_id=installation_id,
        account_login=account_login,
    )
    await doc.insert()

    await emit(
        CodeConnectionCreated(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "provider": provider,
                "installation_id": installation_id,
            }
        )
    )
    return _doc_to_view(doc)


async def list_connections(workspace_id: str, user_id: str) -> list[CodeConnectionView]:
    """List every connection owned by the caller, newest first.

    Tenant-filtered by ``workspace_id`` AND owner-filtered by ``user_id`` so one
    user never sees another's GitHub connections even within a shared workspace.
    """
    docs = (
        await _CodeConnectionDoc.find(
            {"workspace_id": workspace_id, "user_id": user_id}  # Rule 7 tenant filter
        )
        .sort([("created_at", -1)])
        .to_list()
    )
    return [_doc_to_view(d) for d in docs]


__all__ = [
    "list_connections",
    "save_connection",
    "view_to_wire",
]
