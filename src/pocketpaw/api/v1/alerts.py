# Alert API router — list and acknowledge alerts.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw.alert_manager import get_alert_manager
from pocketpaw.api.deps import require_scope

router = APIRouter(tags=["Alerts"])


@router.get("/alerts", dependencies=[Depends(require_scope("metrics", "admin"))])
async def list_alerts(
    limit: int = Query(50, ge=1, le=200),
    unread_only: bool = Query(False),
    since: str = Query("", description="ISO timestamp — return only alerts after this time"),
) -> dict:
    """Return recent alerts from the AlertManager ring buffer."""
    manager = get_alert_manager()
    alerts = manager.list_alerts(
        limit=limit,
        unread_only=unread_only,
        since=since or None,
    )
    return {
        "unread_count": manager.unread_count,
        "alerts": alerts,
    }


@router.post("/alerts/mark-read", dependencies=[Depends(require_scope("metrics", "admin"))])
async def mark_alerts_read() -> dict:
    """Reset the unread alert counter."""
    manager = get_alert_manager()
    manager.mark_read()
    return {"status": "ok", "unread_count": manager.unread_count}
