# Telegram router — status, setup, pairing.
# Created: 2026-02-20
#
# Wraps the existing /api/telegram/* endpoints as v1 REST routes.
# The actual pairing logic (temporary bot, QR code) lives in dashboard.py
# and is referenced here via the shared _telegram_pairing_state dict.

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from pocketpaw.api.v1.schemas.telegram import (
    TelegramPairingStatusResponse,
    TelegramSetupRequest,
    TelegramSetupResponse,
    TelegramStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Telegram"])


@router.get("/telegram/status", response_model=TelegramStatusResponse)
async def get_telegram_status():
    """Get current Telegram configuration status."""
    from pocketpaw.config import Settings

    settings = Settings.load()
    return TelegramStatusResponse(
        configured=bool(settings.telegram_bot_token and settings.allowed_user_id),
        user_id=settings.allowed_user_id,
    )


@router.post("/telegram/setup", response_model=TelegramSetupResponse)
async def setup_telegram(body: TelegramSetupRequest):
    """Start Telegram pairing flow.

    Launches a temporary bot, generates a QR code and deep link for the user
    to scan or click in Telegram. Check /telegram/pairing-status to poll for
    completion.
    """
    import secrets

    try:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, ContextTypes
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="python-telegram-bot not installed. Run: pip install pocketpaw[telegram]",
        )

    from pocketpaw.config import Settings

    # Access the shared pairing state from dashboard
    try:
        from pocketpaw.dashboard import _telegram_pairing_state
    except ImportError:
        raise HTTPException(status_code=503, detail="Dashboard not running")

    bot_token = body.bot_token.strip()

    session_secret = secrets.token_urlsafe(32)
    _telegram_pairing_state["session_secret"] = session_secret
    _telegram_pairing_state["paired"] = False
    _telegram_pairing_state["user_id"] = None

    try:
        temp_app = Application.builder().token(bot_token).build()
        bot_info = await temp_app.bot.get_me()

        async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.message and update.message.text:
                parts = update.message.text.split()
                if len(parts) > 1 and parts[1] == session_secret:
                    user_id = update.effective_user.id
                    settings = Settings.load()
                    settings.telegram_bot_token = bot_token
                    settings.allowed_user_id = user_id
                    settings.save()

                    _telegram_pairing_state["paired"] = True
                    _telegram_pairing_state["user_id"] = user_id

                    await update.message.reply_text("Connected to PocketPaw!")

        temp_app.add_handler(CommandHandler("start", start_handler))
        await temp_app.initialize()
        await temp_app.start()
        await temp_app.updater.start_polling(allowed_updates=[Update.MESSAGE])

        _telegram_pairing_state["temp_bot_app"] = temp_app

        deep_link = f"https://t.me/{bot_info.username}?start={session_secret}"

        # Generate QR code
        qr_url = ""
        try:
            import io

            import qrcode

            qr = qrcode.make(deep_link)
            buf = io.BytesIO()
            qr.save(buf, format="PNG")
            import base64

            qr_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            pass

        return TelegramSetupResponse(qr_url=qr_url, deep_link=deep_link)

    except Exception as e:
        return TelegramSetupResponse(error=f"Failed to connect to Telegram: {e}")


@router.get("/telegram/pairing-status", response_model=TelegramPairingStatusResponse)
async def get_telegram_pairing_status():
    """Check if Telegram pairing is complete."""
    try:
        from pocketpaw.dashboard import _telegram_pairing_state
    except ImportError:
        return TelegramPairingStatusResponse(paired=False)

    paired = _telegram_pairing_state.get("paired", False)
    user_id = _telegram_pairing_state.get("user_id")

    # Cleanup temporary bot if pairing complete
    if paired and _telegram_pairing_state.get("temp_bot_app"):
        try:
            temp_app = _telegram_pairing_state["temp_bot_app"]
            if temp_app.updater.running:
                await temp_app.updater.stop()
            if temp_app.running:
                await temp_app.stop()
            await temp_app.shutdown()
            _telegram_pairing_state["temp_bot_app"] = None
        except Exception:
            logger.warning("Failed to cleanup temporary Telegram bot", exc_info=True)

    return TelegramPairingStatusResponse(paired=paired, user_id=user_id)
