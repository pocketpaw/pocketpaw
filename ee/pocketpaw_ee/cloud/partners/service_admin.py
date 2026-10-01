# ee/pocketpaw_ee/cloud/partners/service_admin.py — platform-admin partner switch.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1). Sets or clears
# ``Workspace.partner`` on ANY workspace. Reached only through the
# ``require_platform("platform.partners.write")`` route in ``partners/router.py``.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import PartnerProfileSet
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.partners.dto import PartnerProfileIn, PartnerProfileOut
from pocketpaw_ee.cloud.partners.service import _oid


async def set_partner_profile(
    *, workspace_id: str, body: Any | None, operator_id: str
) -> PartnerProfileOut | None:
    """Set (``body``) or clear (``body=None``) a workspace's partner profile."""
    body = PartnerProfileIn.model_validate(body) if body is not None else None
    oid = _oid(workspace_id)
    # admin-cross-tenant: platform operators turn ANY workspace into a partner;
    # workspace_id is a path parameter, guarded by require_platform at the route.
    ws = None if oid is None else await _WorkspaceDoc.find_one({"_id": oid, "deleted_at": None})
    if ws is None:
        raise NotFound("workspace", workspace_id)
    if body is None:
        ws.partner = None
    else:
        data = body.model_dump()
        # Keep the original join date across status changes unless one is sent.
        if data["joined_at"] is None:
            data["joined_at"] = ws.partner.joined_at if ws.partner else datetime.now(UTC)
        ws.partner = PartnerProfile(**data)
    await ws.save()
    await emit(
        PartnerProfileSet(
            data={
                "workspace_id": workspace_id,
                "status": ws.partner.status if ws.partner else None,
                "operator_id": operator_id,
            }
        )
    )
    if ws.partner is None:
        return None
    return PartnerProfileOut.model_validate(ws.partner, from_attributes=True)
