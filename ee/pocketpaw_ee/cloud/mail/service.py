"""Mail service — thin wrapper around the Mailtrap Python SDK.

Best-effort delivery: mail failures are logged but never propagate to the
caller. Invite creation (or any future mail-triggering action) must never
fail because the mail provider is down.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

import mailtrap as mt

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_PROVIDER_NAMES = {
    "zoom": "Zoom",
    "google_meet": "Google Meet",
    "livekit": "pawOS Call",
    "recall": "Meeting",
}

# ---------------------------------------------------------------------------
# Configuration (env vars — no Pydantic Settings dependency needed here)
# ---------------------------------------------------------------------------


def _env_bool(key: str, default: bool = True) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("1", "true", "yes", "on")


_MAILTRAP_API_TOKEN = os.environ.get("POCKETPAW_MAILTRAP_API_TOKEN")
_SENDER_EMAIL = os.environ.get("POCKETPAW_MAILTRAP_SENDER_EMAIL", "noreply@pocketpaw.ai")
_SENDER_NAME = os.environ.get("POCKETPAW_MAILTRAP_SENDER_NAME", "PocketPaw")
_USE_SANDBOX = _env_bool("POCKETPAW_MAILTRAP_USE_SANDBOX", True)
_INBOX_ID_RAW = os.environ.get("POCKETPAW_MAILTRAP_INBOX_ID")
_INBOX_ID: int | None = int(_INBOX_ID_RAW) if _INBOX_ID_RAW and _INBOX_ID_RAW.isdigit() else None

# ---------------------------------------------------------------------------
# Lazy client
# ---------------------------------------------------------------------------

_client: object | None = None  # MailtrapClient | None (lazy to avoid import crash)


def _get_client():
    """Return (or create) the shared MailtrapClient.

    Lazy so a missing API token doesn't crash the import path — the
    workspace service can still load even when mail is not configured.
    """
    global _client
    if _client is not None:
        return _client
    if not _MAILTRAP_API_TOKEN:
        raise RuntimeError("POCKETPAW_MAILTRAP_API_TOKEN is not set")

    kwargs: dict[str, object] = dict(
        token=_MAILTRAP_API_TOKEN,
        sandbox=_USE_SANDBOX,
    )
    if _USE_SANDBOX and _INBOX_ID is not None:
        kwargs["inbox_id"] = _INBOX_ID
    _client = mt.MailtrapClient(**kwargs)
    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_invite_email(
    to_email: str,
    workspace_name: str,
    invite_token: str,
    inviter_name: str = "Someone",
) -> None:
    """Send a workspace-invitation email via Mailtrap.

    Builds a plain-text + HTML email with the invite link.

    Failures are logged at WARNING level — the caller (workspace service)
    must NOT let a mail blip abort the invite creation.
    """

    base_url = os.environ.get("POCKETPAW_CLOUD_BASE_URL").rstrip("/")
    invite_url = f"{base_url}/invite/{invite_token}"

    subject = f"{inviter_name} invited you to join {workspace_name} on PocketPaw"

    text_body = (
        f"Hi,\n\n"
        f"{inviter_name} has invited you to join the workspace "
        f"{workspace_name!r} on PocketPaw.\n\n"
        f"Accept the invite here:\n{invite_url}\n\n"
        f"This invite expires in 7 days.\n\n"
        f"— The PocketPaw Team"
    )

    template = (_TEMPLATES_DIR / "send_invite.html").read_text()
    html_body = template.format(
        inviter_name=inviter_name,
        workspace_name=workspace_name,
        invite_url=invite_url,
    )

    try:
        client = await asyncio.to_thread(_get_client)
    except RuntimeError:
        logger.warning("Mailtrap not configured — skipping invite email to %s", to_email)
        return
    except Exception:
        logger.warning("Failed to initialise Mailtrap client", exc_info=True)
        return

    try:
        mail = mt.Mail(
            sender=mt.Address(email=_SENDER_EMAIL, name=_SENDER_NAME),
            to=[mt.Address(email=to_email)],
            subject=subject,
            text=text_body,
            html=html_body,
        )
        await asyncio.to_thread(client.send, mail)
        logger.info("Invite email sent to %s for workspace %r", to_email, workspace_name)
    except Exception:
        logger.warning(
            "Failed to send invite email to %s for workspace %r",
            to_email,
            workspace_name,
            exc_info=True,
        )


async def send_meeting_scheduled_email(
    to_email: str,
    to_name: str,
    title: str,
    group_name: str,
    scheduled_start: str,
    join_url: str | None = None,
    provider: str = "meeting",
    creator_name: str = "Someone",
) -> None:
    """Send a meeting-scheduled notification email via Mailtrap.

    Builds a plain-text + HTML email. The ``join_url`` is optional — if
    provided the join link table row and button render in the HTML template;
    if omitted those sections are stripped from the template using marker
    comments so the email focuses on the group + provider info.

    Failures are logged at WARNING level — the caller must NOT let a mail
    blip abort the meeting creation.
    """

    provider_pretty = _PROVIDER_NAMES.get(provider, provider)
    subject = f"📅 Meeting scheduled in {group_name}: {title}"

    text_body = (
        f"Hi {to_name},\n\n"
        f"A meeting has been scheduled in {group_name}.\n\n"
        f"Title:      {title}\n"
        f"Scheduled:  {scheduled_start}\n"
        f"Provider:   {provider_pretty}\n"
    )
    if join_url:
        text_body += f"Join:       {join_url}\n"
    text_body += f"\nCreated by {creator_name}.\n\n— The PocketPaw Team"

    # Load template and conditionally strip join-url blocks
    try:
        template = (_TEMPLATES_DIR / "send_meeting_scheduled.html").read_text()
    except Exception:
        logger.exception("Failed to read meeting scheduled template — falling back to plain text")
        html_body = None
    else:
        if not join_url:
            template = re.sub(
                r"<!-- JOIN_URL_TABLE_ROW -->.*?<!-- END_JOIN_URL_TABLE_ROW -->",
                "",
                template,
                flags=re.DOTALL,
            )
            template = re.sub(
                r"<!-- JOIN_URL_BUTTON -->.*?<!-- END_JOIN_URL_BUTTON -->",
                "",
                template,
                flags=re.DOTALL,
            )
        html_body = template.format(
            title=title,
            group_name=group_name,
            scheduled_start=scheduled_start,
            provider_pretty=provider_pretty,
            join_url=join_url or "",
            creator_name=creator_name,
        )

    try:
        client = await asyncio.to_thread(_get_client)
    except RuntimeError:
        logger.warning("Mailtrap not configured — skipping meeting scheduled email to %s", to_email)
        return
    except Exception:
        logger.warning("Failed to initialise Mailtrap client", exc_info=True)
        return

    try:
        mail_kwargs: dict[str, object] = dict(
            sender=mt.Address(email=_SENDER_EMAIL, name=_SENDER_NAME),
            to=[mt.Address(email=to_email)],
            subject=subject,
            text=text_body,
        )
        if html_body:
            mail_kwargs["html"] = html_body

        mail = mt.Mail(**mail_kwargs)
        await asyncio.to_thread(client.send, mail)
        logger.info("Meeting scheduled email sent to %s for %r", to_email, title)
    except Exception:
        logger.warning(
            "Failed to send meeting scheduled email to %s for %r",
            to_email,
            title,
            exc_info=True,
        )


async def send_feedback_email(
    user_email: str,
    user_name: str,
    subject: str,
    message: str,
    workspace_name: str = "",
) -> None:
    """Send user feedback via Mailtrap to the configured feedback inbox.

    The destination address is read from ``POCKETPAW_FEEDBACK_EMAIL``
    (required).  Failures are logged at WARNING level — the caller must
    NOT let a mail blip abort the feedback submission.
    """

    feedback_email = os.environ.get("POCKETPAW_FEEDBACK_EMAIL")
    if not feedback_email:
        logger.warning("POCKETPAW_FEEDBACK_EMAIL is not set — skipping feedback email")
        return

    mail_subject = f"[Feedback] {subject}"
    if workspace_name:
        mail_subject += f" — {workspace_name}"

    text_body = (
        f"New feedback submission\n\nFrom:    {user_name} ({user_email})\nSubject: {subject}\n"
    )
    if workspace_name:
        text_body += f"Workspace: {workspace_name}\n"
    text_body += f"\nMessage:\n{message}\n\n— Sent via PocketPaw Feedback"

    html_body = _render_feedback_html(
        user_name=user_name,
        user_email=user_email,
        subject=subject,
        message=message,
        workspace_name=workspace_name,
    )

    try:
        client = await asyncio.to_thread(_get_client)
    except RuntimeError:
        logger.warning("Mailtrap not configured — skipping feedback email from %s", user_email)
        return
    except Exception:
        logger.warning("Failed to initialise Mailtrap client for feedback email", exc_info=True)
        return

    try:
        mail = mt.Mail(
            sender=mt.Address(email=_SENDER_EMAIL, name=_SENDER_NAME),
            to=[mt.Address(email=feedback_email)],
            subject=mail_subject,
            text=text_body,
            html=html_body,
        )
        await asyncio.to_thread(client.send, mail)
        logger.info("Feedback email sent from %s to %s", user_email, feedback_email)
    except Exception:
        logger.warning(
            "Failed to send feedback email from %s",
            user_email,
            exc_info=True,
        )


def _render_feedback_html(
    *,
    user_name: str,
    user_email: str,
    subject: str,
    message: str,
    workspace_name: str = "",
) -> str:
    """Render a simple HTML feedback email (inline, no template file needed)."""
    ws_line = f"<p><strong>Workspace:</strong> {workspace_name}</p>" if workspace_name else ""
    # escape angle brackets for safety
    safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<!doctype html><html><head><meta charset="UTF-8"></head>'
        '<body style="font-family: -apple-system, BlinkMacSystemFont, '
        "'Segoe UI', Roboto, sans-serif;\">"
        '<div style="max-width: 560px; margin: 0 auto; padding: 32px 24px;">'
        '<h2 style="margin-top: 0;">📬 New Feedback</h2>'
        f"<p><strong>From:</strong> {user_name} &lt;{user_email}&gt;</p>"
        f"{ws_line}"
        f"<p><strong>Subject:</strong> {subject}</p>"
        '<hr style="border: 0; border-top: 1px solid #e5e7eb; margin: 20px 0;">'
        f'<pre style="white-space: pre-wrap; font-family: inherit; '
        f'font-size: 14px; line-height: 1.6;">{safe_message}</pre>'
        '<hr style="border: 0; border-top: 1px solid #e5e7eb; margin: 20px 0;">'
        '<p style="color: #6b7280; font-size: 13px;">Sent via PocketPaw Feedback</p>'
        "</div></body></html>"
    )
