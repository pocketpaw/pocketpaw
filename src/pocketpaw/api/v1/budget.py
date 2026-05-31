# Budget status and override endpoints.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from pocketpaw.api.deps import require_scope
from pocketpaw.budget import (
    clear_budget_override,
    set_budget_override_until_window_end,
    sync_budget_state,
)
from pocketpaw.config import Settings, get_settings

router = APIRouter(tags=["Budget"])

_budget_lock = asyncio.Lock()


class BudgetOverrideRequest(BaseModel):
    """Temporary cap override request."""

    cap_usd: float = Field(..., gt=0)
    reason: str = Field(default="", max_length=240)


def _status_payload(settings: Settings, snapshot: dict[str, object]) -> dict[str, object]:
    """Build API payload shape for current budget state."""
    return {
        "now": datetime.now(UTC).isoformat(),
        "paused": bool(getattr(settings, "budget_paused", False)),
        "auto_pause": bool(getattr(settings, "budget_auto_pause", True)),
        "reset_day": int(getattr(settings, "budget_reset_day", 1) or 1),
        "configured_cap_usd": float(getattr(settings, "budget_monthly_usd", 20.0) or 20.0),
        "warning_threshold": float(getattr(settings, "budget_warning_threshold", 0.8) or 0.8),
        "override": {
            "cap_usd": getattr(settings, "budget_override_usd", None),
            "reason": getattr(settings, "budget_override_reason", "") or "",
            "expires_at": getattr(settings, "budget_override_expires_at", None),
        },
        "budget": snapshot,
    }


async def _sync_and_persist_if_needed(settings: Settings) -> dict[str, object]:
    """Normalize budget settings and persist changes (override expiry/pause state)."""
    snapshot, changed = sync_budget_state(settings)
    if changed:
        settings.save()
        get_settings.cache_clear()
    return snapshot.to_dict()


@router.get(
    "/budget/status",
    dependencies=[Depends(require_scope("metrics", "admin", "settings:read", "settings:write"))],
)
async def get_budget_status() -> dict[str, object]:
    """Return current budget configuration and computed window status."""
    async with _budget_lock:
        settings = Settings.load()
        snapshot = await _sync_and_persist_if_needed(settings)
    return _status_payload(settings, snapshot)


@router.post(
    "/budget/override",
    dependencies=[Depends(require_scope("settings:write", "admin"))],
)
async def set_budget_override(payload: BudgetOverrideRequest) -> dict[str, object]:
    """Set a temporary override cap active until the next reset-day boundary."""
    clean_reason = payload.reason.strip()
    async with _budget_lock:
        settings = Settings.load()
        if payload.cap_usd <= 0:
            raise HTTPException(status_code=400, detail="cap_usd must be greater than 0")

        window = set_budget_override_until_window_end(
            settings,
            cap_usd=float(payload.cap_usd),
            reason=clean_reason,
        )
        snapshot = await _sync_and_persist_if_needed(settings)

    return {
        "status": "ok",
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "override_expires_at": getattr(settings, "budget_override_expires_at", None),
        "budget": snapshot,
    }


@router.delete(
    "/budget/override",
    dependencies=[Depends(require_scope("settings:write", "admin"))],
)
async def clear_budget_override_route() -> dict[str, object]:
    """Clear temporary budget override and recompute active budget status."""
    async with _budget_lock:
        settings = Settings.load()
        clear_budget_override(settings)
        snapshot = await _sync_and_persist_if_needed(settings)

    return {
        "status": "ok",
        "budget": snapshot,
    }
