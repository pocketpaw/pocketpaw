"""WhatsApp bot gateway (standalone mode with webhook server)."""

import logging

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse

from pocketpaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)

# Module-level references set during startup
_whatsapp_adapter: WhatsAppAdapter | None = None


def create_whatsapp_app(settings: Settings) -> FastAPI:
    """Create a minimal FastAPI app with WhatsApp webhook routes."""
    wa_app = FastAPI(title="PocketPaw WhatsApp Gateway")

    @wa_app.get("/webhook/whatsapp")
    async def verify_webhook(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_token: str | None = Query(None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(None, alias="hub.challenge"),
    ):
        """Meta webhook verification."""
        if _whatsapp_adapter is None:
            return PlainTextResponse("Not configured", status_code=503)
        result = _whatsapp_adapter.handle_webhook_verify(hub_mode, hub_token, hub_challenge)
        if result:
            return PlainTextResponse(result)
        return PlainTextResponse("Forbidden", status_code=403)

    @wa_app.post("/webhook/whatsapp")
    async def receive_webhook(request: Request):
        """Incoming WhatsApp messages."""
        if _whatsapp_adapter is None:
            return {"status": "not configured"}
        payload = await request.json()
        await _whatsapp_adapter.handle_webhook_message(payload)
        return {"status": "ok"}

    return wa_app


async def run_whatsapp_bot(settings: Settings) -> None:
    """Run WhatsApp bot with its own FastAPI webhook server."""
    import asyncio

    from pocketpaw.agents.loop import AgentLoop
    from pocketpaw.bus import get_message_bus

    global _whatsapp_adapter

    bus = get_message_bus()

    adapter = WhatsAppAdapter(
        access_token=settings.whatsapp_access_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        verify_token=settings.whatsapp_verify_token,
        allowed_phone_numbers=settings.whatsapp_allowed_phone_numbers,
    )
    _whatsapp_adapter = adapter

    agent_loop = AgentLoop()
    from pocketpaw.bus.commands import get_command_handler

    get_command_handler().set_agent_loop(agent_loop)

    logger.info("Starting PocketPaw WhatsApp bot...")

    await adapter.start(bus)
    asyncio.create_task(agent_loop.start())

    wa_app = create_whatsapp_app(settings)
    config = uvicorn.Config(
        wa_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    try:
        await server.serve()
    except asyncio.CancelledError:
        logger.info("Stopping WhatsApp bot...")
    finally:
        await agent_loop.stop()
        await adapter.stop()
        _whatsapp_adapter = None
