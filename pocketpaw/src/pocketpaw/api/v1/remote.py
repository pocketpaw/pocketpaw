# Remote tunnel router — status, start, stop.
# Created: 2026-02-20

from __future__ import annotations

import logging

from fastapi import APIRouter

from pocketpaw.api.v1.schemas.remote import TunnelStartResponse, TunnelStatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Remote"])


@router.get("/remote/status", response_model=TunnelStatusResponse)
async def get_tunnel_status():
    """Get active tunnel status."""
    from pocketpaw.tunnel import get_tunnel_manager

    manager = get_tunnel_manager()
    status = manager.get_status()
    return TunnelStatusResponse(active=status.get("active", False), url=status.get("url"))


@router.post("/remote/start", response_model=TunnelStartResponse)
async def start_tunnel():
    """Start Cloudflare tunnel."""
    from pocketpaw.tunnel import get_tunnel_manager

    manager = get_tunnel_manager()
    try:
        url = await manager.start()
        return TunnelStartResponse(url=url, active=True)
    except Exception as e:
        return TunnelStartResponse(active=False, error=str(e))


@router.post("/remote/stop", response_model=TunnelStatusResponse)
async def stop_tunnel():
    """Stop Cloudflare tunnel."""
    from pocketpaw.tunnel import get_tunnel_manager

    manager = get_tunnel_manager()
    await manager.stop()
    return TunnelStatusResponse(active=False)
