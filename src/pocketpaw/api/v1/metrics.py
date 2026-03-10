# System + usage metrics endpoints.
# Created: 2026-03-09

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from pocketpaw.api.deps import require_scope

router = APIRouter(tags=["Metrics"])


@router.get("/metrics/system", dependencies=[Depends(require_scope("metrics", "admin"))])
async def get_system_metrics():
    """Return system resource metrics (CPU, RAM, disk, uptime, battery)."""
    try:
        import psutil
    except ImportError:
        return {
            "available": False,
            "os": platform.system(),
            "arch": platform.machine(),
            "error": "psutil not installed",
        }

    cpu_percent = psutil.cpu_percent(interval=0)
    cpu_count = psutil.cpu_count()
    cpu_freq = psutil.cpu_freq()

    mem = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage(str(Path.home()))
    except Exception:
        disk = psutil.disk_usage("/")

    boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
    uptime = datetime.now(tz=UTC) - boot_time
    uptime_seconds = int(uptime.total_seconds())

    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {
                "percent": round(b.percent, 1),
                "plugged": b.power_plugged,
                "secs_left": b.secsleft if b.secsleft != psutil.POWER_TIME_UNLIMITED else None,
            }
    except Exception:
        pass

    return {
        "available": True,
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu": {
            "percent": round(cpu_percent, 1),
            "cores": cpu_count,
            "freq_mhz": round(cpu_freq.current, 0) if cpu_freq else None,
        },
        "memory": {
            "used_bytes": mem.used,
            "total_bytes": mem.total,
            "percent": round(mem.percent, 1),
        },
        "disk": {
            "used_bytes": disk.used,
            "total_bytes": disk.total,
            "percent": round(disk.percent, 1),
        },
        "uptime_seconds": uptime_seconds,
        "battery": battery,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }


@router.get("/metrics/usage", dependencies=[Depends(require_scope("metrics", "admin"))])
async def get_usage_summary(since: str = Query("", description="ISO timestamp filter")):
    """Return aggregated token usage and cost summary."""
    from pocketpaw.usage_tracker import get_usage_tracker

    tracker = get_usage_tracker()
    return tracker.get_summary(since=since if since else None)


@router.get("/metrics/usage/recent", dependencies=[Depends(require_scope("metrics", "admin"))])
async def get_recent_usage(limit: int = Query(50, ge=1, le=500)):
    """Return recent usage records."""
    from pocketpaw.usage_tracker import get_usage_tracker

    tracker = get_usage_tracker()
    records = tracker.get_records(limit=limit)
    return [
        {
            "timestamp": r.timestamp,
            "backend": r.backend,
            "model": r.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cached_input_tokens": r.cached_input_tokens,
            "total_tokens": r.total_tokens,
            "cost_usd": r.cost_usd,
            "session_id": r.session_id,
        }
        for r in records
    ]


@router.delete("/metrics/usage", dependencies=[Depends(require_scope("admin"))])
async def clear_usage():
    """Clear all usage records."""
    from pocketpaw.usage_tracker import get_usage_tracker

    get_usage_tracker().clear()
    return {"cleared": True}
