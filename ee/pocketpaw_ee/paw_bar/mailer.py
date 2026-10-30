# ee/paw_bar/mailer.py — Minimal, fail-soft SMTP sender for the Paw Bar
# async decision delivery (2026-07-30).
# Created: 2026-07-30 — pocketpaw has no system-owned outbound email transport
#   (the Gmail client is a user-scoped OAuth tool, not ours to send from), so
#   this module is a deliberately tiny SMTP sender used ONLY by the decision
#   delivery hook. Env-driven, no config-module coupling:
#     POCKETPAW_SMTP_HOST      — SMTP server hostname (unset → transport off)
#     POCKETPAW_SMTP_PORT      — port (default 587)
#     POCKETPAW_SMTP_USER      — auth username (optional; no user → no login)
#     POCKETPAW_SMTP_PASSWORD  — auth password (optional)
#     POCKETPAW_SMTP_FROM      — the From address (unset → transport off)
#     POCKETPAW_SMTP_STARTTLS  — "1" (default) upgrades via STARTTLS; "0" off
#   FAIL-SOFT is the contract: no transport configured → one warning log and
#   False; a send error → warning and False. Nothing here ever raises into the
#   caller — a mail failure must never break an approve/reject, and the on-page
#   decision poll keeps working regardless.

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    """True when the minimum viable transport (host + from) is present."""
    return bool(os.environ.get("POCKETPAW_SMTP_HOST")) and bool(
        os.environ.get("POCKETPAW_SMTP_FROM")
    )


def _send_sync(to_addr: str, subject: str, body: str) -> None:
    """Blocking SMTP send — run via ``asyncio.to_thread`` from the async path."""
    host = os.environ["POCKETPAW_SMTP_HOST"]
    port = int(os.environ.get("POCKETPAW_SMTP_PORT", "587") or "587")
    user = os.environ.get("POCKETPAW_SMTP_USER", "")
    password = os.environ.get("POCKETPAW_SMTP_PASSWORD", "")
    sender = os.environ["POCKETPAW_SMTP_FROM"]
    starttls = os.environ.get("POCKETPAW_SMTP_STARTTLS", "1") != "0"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if starttls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


async def send_decision_email(to_addr: str, subject: str, body: str) -> bool:
    """Send one plain-text email; return True on success, False otherwise.

    Fail-soft by contract: an unconfigured transport logs a warning and returns
    False; so does any transport error. Never raises. The recipient address is
    visitor PII — it is logged only in redacted form (local-part hidden).
    """
    if not smtp_configured():
        logger.warning(
            "paw-bar decision email skipped — no SMTP transport configured "
            "(set POCKETPAW_SMTP_HOST + POCKETPAW_SMTP_FROM to enable async "
            "delivery; the on-page decision poll still works)"
        )
        return False
    try:
        await asyncio.to_thread(_send_sync, to_addr, subject, body)
        return True
    except Exception:  # noqa: BLE001 — fail-soft: mail must never break the approve
        domain = to_addr.rsplit("@", 1)[-1] if "@" in to_addr else "<invalid>"
        logger.warning(
            "paw-bar decision email send failed (recipient domain %s) — the "
            "decision was still delivered on the poll endpoint",
            domain,
            exc_info=True,
        )
        return False


__all__ = ["send_decision_email", "smtp_configured"]
