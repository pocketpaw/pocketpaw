# ee/pocketpaw_ee/cloud/notifications/email.py
# Owner notification email through Cloudflare Email Service's REST API
# (``POST /accounts/{id}/email/sending/send``, Bearer token). pocketPaw is not a
# Worker, so there is no send_email binding; this is a plain httpx client.
#
# REST field names differ from the Workers binding: a named sender is
# ``from: {address, name}`` (not ``email``) and the reply address is
# ``reply_to`` (not ``replyTo``). The response sorts recipients into
# ``result.delivered`` / ``queued`` / ``permanent_bounces``.
#
# ``send_email`` never raises. It returns a ``SendResult`` whose ``outcome`` the
# outbox acts on: ``sent``; ``retry`` for 429, 5xx and transport errors, and also
# for 401/403 (a revoked token or sending disabled on the account is an operator
# problem that gets fixed, not a reason to drop mail; ``auth_failure`` tells the
# outbox to warn the workspace admins once); ``permanent`` for every other
# non-2xx (400/422 schema or bad message). Permanent bounces ride back on a
# ``sent`` result so the caller can record them against the recipient.
#
# The sink is OFF until POCKETPAW_CF_EMAIL_ACCOUNT_ID, _API_TOKEN and _FROM are
# all set; ``load_config`` logs that once. Templates are plain and accessible,
# carry both html and text, HTML-escape every visitor- or user-supplied value,
# link to the item in paw-enterprise (POCKETPAW_FRONTEND_BASE_URL) and end with a
# link to the notification settings.

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

CF_API_BASE = "https://api.cloudflare.com/client/v4"
_SEND_TIMEOUT_SECONDS = 10.0
_MAX_FIELD_CHARS = 2000
_MAX_SUBJECT_CHARS = 150

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINEBREAK_RE = re.compile(r"[\r\n\t]+")

_config_warned = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailConfig:
    account_id: str
    api_token: str
    from_address: str
    from_name: str = "PocketPaw"


def load_config() -> EmailConfig | None:
    """The CF email config, or None (logged once) while any required value is unset."""
    global _config_warned
    from pocketpaw.config import get_settings

    settings = get_settings()
    account_id = (getattr(settings, "cf_email_account_id", None) or "").strip()
    token = (getattr(settings, "cf_email_api_token", None) or "").strip()
    from_address = (getattr(settings, "cf_email_from", None) or "").strip()
    from_name = (getattr(settings, "cf_email_from_name", None) or "").strip() or "PocketPaw"
    if not (account_id and token and from_address):
        if not _config_warned:
            logger.info(
                "owner email sink is off: set POCKETPAW_CF_EMAIL_ACCOUNT_ID, "
                "POCKETPAW_CF_EMAIL_API_TOKEN and POCKETPAW_CF_EMAIL_FROM to enable it"
            )
            _config_warned = True
        return None
    return EmailConfig(account_id, token, from_address, from_name)


def is_configured() -> bool:
    return load_config() is not None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class EmailMessage:
    to: list[str]
    subject: str
    html: str
    text: str
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SendResult:
    outcome: Literal["sent", "retry", "permanent"]
    error: str = ""
    status_code: int | None = None
    message_id: str = ""
    delivered: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    permanent_bounces: list[str] = field(default_factory=list)
    # 401/403 from Cloudflare: the token or the account's sending is broken.
    auth_failure: bool = False


def _request_body(message: EmailMessage, config: EmailConfig) -> dict[str, Any]:
    body: dict[str, Any] = {
        "to": list(message.to),
        "from": {"address": config.from_address, "name": config.from_name},
        "subject": message.subject,
        "html": message.html,
        "text": message.text,
    }
    if message.reply_to:
        body["reply_to"] = message.reply_to
    if message.headers:
        body["headers"] = dict(message.headers)
    return body


def _error_text(resp: httpx.Response) -> str:
    try:
        errors = resp.json().get("errors") or []
        parts = [f"{e.get('code')}: {e.get('message')}" for e in errors if isinstance(e, dict)]
        if parts:
            return f"http {resp.status_code} " + "; ".join(parts)
    except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
        pass
    return f"http {resp.status_code}"


async def send_email(
    message: EmailMessage,
    *,
    config: EmailConfig | None = None,
    client: httpx.AsyncClient | None = None,
) -> SendResult:
    """POST one message to Cloudflare. Never raises; see the module header."""
    config = config or load_config()
    if config is None:
        return SendResult(outcome="permanent", error="email sink not configured")
    url = f"{CF_API_BASE}/accounts/{config.account_id}/email/sending/send"
    headers = {"Authorization": f"Bearer {config.api_token}"}
    body = _request_body(message, config)
    try:
        if client is not None:
            resp = await client.post(url, json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS) as owned:
                resp = await owned.post(url, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001 — transport errors are retryable
        return SendResult(outcome="retry", error=f"transport: {type(exc).__name__}")

    code = resp.status_code
    if code == 429 or code >= 500:
        return SendResult(outcome="retry", error=_error_text(resp), status_code=code)
    if code in (401, 403):
        return SendResult(
            outcome="retry", error=_error_text(resp), status_code=code, auth_failure=True
        )
    if not 200 <= code < 300:
        return SendResult(outcome="permanent", error=_error_text(resp), status_code=code)
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {}
    if data.get("success") is False:
        return SendResult(outcome="permanent", error=_error_text(resp), status_code=code)
    result = data.get("result") or {}
    return SendResult(
        outcome="sent",
        status_code=code,
        message_id=str(result.get("message_id") or ""),
        delivered=[str(a) for a in result.get("delivered") or []],
        queued=[str(a) for a in result.get("queued") or []],
        permanent_bounces=[str(a) for a in result.get("permanent_bounces") or []],
    )


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def app_base_url() -> str:
    """paw-enterprise origin for links in email (same var meeting links use)."""
    return os.environ.get("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420").rstrip("/")


def api_base_url() -> str:
    """This backend's public origin, for links that hit an API route directly."""
    return os.environ.get("POCKETPAW_PUBLIC_BASE_URL", "http://localhost:8888").rstrip("/")


def lead_url(site_id: str, lead_id: str = "") -> str:
    suffix = f"&lead={lead_id}" if lead_id else ""
    return f"{app_base_url()}/sites/{site_id}?view=leads{suffix}"


def site_settings_url(site_id: str) -> str:
    return f"{app_base_url()}/sites/{site_id}?mode=more&section=notifications"


def workspace_settings_url() -> str:
    return f"{app_base_url()}/settings/notifications"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def clean_text(value: Any, cap: int = _MAX_FIELD_CHARS) -> str:
    """Stringify, drop control characters, cap. Escaping happens at render."""
    return _CONTROL_RE.sub("", str(value if value is not None else "")).strip()[:cap]


def _one_line(value: Any, cap: int) -> str:
    return _LINEBREAK_RE.sub(" ", clean_text(value, cap)).strip()


def safe_reply_to(value: Any) -> str | None:
    """A reply_to address, or None when the value is not a plausible email."""
    from pocketpaw.sites_capture.contact_form import looks_like_email

    raw = str(value or "").strip()
    if not raw or len(raw) > 320 or _LINEBREAK_RE.search(raw) or _CONTROL_RE.search(raw):
        return None
    return raw if looks_like_email(raw) else None


def _layout(
    *, heading: str, intro_html: str, body_html: str, cta: tuple[str, str] | None, footer_url: str
) -> str:
    cta_html = ""
    if cta is not None:
        label, href = cta
        cta_html = (
            f'<p style="margin:24px 0"><a href="{html.escape(href, quote=True)}" '
            'style="background:#1d4ed8;color:#ffffff;padding:10px 16px;border-radius:6px;'
            f'text-decoration:none;display:inline-block">{html.escape(label)}</a></p>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(heading)}</title></head>"
        '<body style="margin:0;padding:24px;background:#ffffff;color:#111827;'
        "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        'font-size:16px;line-height:1.5">'
        '<main style="max-width:560px;margin:0 auto">'
        f'<h1 style="font-size:20px;margin:0 0 12px">{html.escape(heading)}</h1>'
        f"{intro_html}{body_html}{cta_html}"
        '<hr style="border:none;border-top:1px solid #e5e7eb;margin:32px 0 16px">'
        '<p style="font-size:13px;color:#4b5563">You get this email because you are '
        "set up to hear about this. "
        f'<a href="{html.escape(footer_url, quote=True)}" style="color:#1d4ed8">'
        "Change notification settings</a>.</p>"
        "</main></body></html>"
    )


def _text_footer(footer_url: str) -> str:
    return f"\n\n--\nChange notification settings: {footer_url}\n"


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str
    reply_to: str | None = None


_CONTACT_ROWS = (("full_name", "Name"), ("email", "Email"), ("phone", "Phone"))


def render_lead_email(lead: dict[str, Any]) -> RenderedEmail:
    """The new-lead email. ``lead`` is the payload ``leads.service.lead_payload``
    builds at send time. Every visitor value is escaped; reply_to is the lead's
    email when it looks valid, so the owner can answer straight from their inbox."""
    from pocketpaw.sites_capture.contact_form import canonical_name, normalize

    properties = lead.get("properties") or {}
    props = normalize(dict(properties)) if isinstance(properties, dict) else {}
    site_name = _one_line(lead.get("site_name") or "your site", 120)
    site_id = str(lead.get("site_id") or "")
    lead_id = str(lead.get("id") or "")
    form_type = _one_line(lead.get("form_type") or "form", 80)
    source = lead.get("source") or {}
    source_kind = _one_line(source.get("kind") or "form", 40)

    name = _one_line(props.get("full_name"), 200)
    email_addr = _one_line(props.get("email"), 320)
    who = name or email_addr or "a visitor"
    subject = _one_line(f"New lead from {site_name}: {who}", _MAX_SUBJECT_CHARS)

    rows: list[tuple[str, str]] = []
    for key, label in _CONTACT_ROWS:
        value = clean_text(props.get(key), 320)
        if value:
            rows.append((label, value))
    message = clean_text(props.get("message"))
    shown = {"full_name", "email", "phone", "message"}
    # Keys that resolve to a contact field are already in the rows above.
    extras = (
        [
            (clean_text(k, 80), clean_text(v))
            for k, v in properties.items()
            if canonical_name(str(k)) not in shown and clean_text(v)
        ]
        if isinstance(properties, dict)
        else []
    )

    link = lead_url(site_id, lead_id)
    footer = site_settings_url(site_id)

    table_rows = "".join(
        f'<tr><th scope="row" style="text-align:left;padding:4px 12px 4px 0;'
        f'vertical-align:top;color:#4b5563;font-weight:600">{html.escape(label)}</th>'
        f'<td style="padding:4px 0;white-space:pre-wrap">{html.escape(value)}</td></tr>'
        for label, value in [*rows, *extras]
    )
    body_html = ""
    if table_rows:
        body_html += (
            f'<table role="presentation" style="border-collapse:collapse">{table_rows}</table>'
        )
    if message:
        body_html += (
            '<h2 style="font-size:16px;margin:20px 0 4px">Message</h2>'
            f'<p style="white-space:pre-wrap;margin:0">{html.escape(message)}</p>'
        )
    intro = (
        f"<p>Someone sent the <strong>{html.escape(form_type)}</strong> form on "
        f"<strong>{html.escape(site_name)}</strong> (source: {html.escape(source_kind)}).</p>"
    )
    html_out = _layout(
        heading=f"New lead from {site_name}",
        intro_html=intro,
        body_html=body_html,
        cta=("Open the lead", link),
        footer_url=footer,
    )

    lines = [
        f"New lead from {site_name}",
        f"Someone sent the {form_type} form (source: {source_kind}).",
        "",
    ]
    lines += [f"{label}: {value}" for label, value in [*rows, *extras]]
    if message:
        lines += ["", "Message:", message]
    lines += ["", f"Open the lead: {link}"]
    text_out = "\n".join(lines) + _text_footer(footer)
    return RenderedEmail(subject, html_out, text_out, safe_reply_to(props.get("email")))


def render_notification_email(
    *, title: str, body: str, link: str, footer_url: str, site_name: str = ""
) -> RenderedEmail:
    """A generic owner notification (a concierge handoff, a booking)."""
    heading = _one_line(title, 120) or "Notification"
    prefix = f"{_one_line(site_name, 120)}: " if site_name else ""
    subject = _one_line(f"{prefix}{heading}", _MAX_SUBJECT_CHARS)
    text_body = clean_text(body)
    body_html = f'<p style="white-space:pre-wrap">{html.escape(text_body)}</p>' if text_body else ""
    html_out = _layout(
        heading=heading,
        intro_html="",
        body_html=body_html,
        cta=("Open it", link),
        footer_url=footer_url,
    )
    text_out = f"{heading}\n\n{text_body}\n\nOpen it: {link}".strip() + _text_footer(footer_url)
    return RenderedEmail(subject, html_out, text_out)


def render_confirm_email(*, site_name: str, confirm_url: str, footer_url: str) -> RenderedEmail:
    name = _one_line(site_name, 120) or "a site"
    subject = _one_line(f"Confirm lead notifications for {name}", _MAX_SUBJECT_CHARS)
    intro = (
        f"<p>Someone asked for this address to get new-lead email for "
        f"<strong>{html.escape(name)}</strong>. Nothing is sent until you confirm. "
        "The link works for 7 days. If you did not expect this, ignore this email.</p>"
    )
    html_out = _layout(
        heading="Confirm your email",
        intro_html=intro,
        body_html="",
        cta=("Confirm this address", confirm_url),
        footer_url=footer_url,
    )
    text_out = (
        f"Someone asked for this address to get new-lead email for {name}.\n"
        "Nothing is sent until you confirm. The link works for 7 days.\n\n"
        f"Confirm: {confirm_url}\n\nIf you did not expect this, ignore this email."
        + _text_footer(footer_url)
    )
    return RenderedEmail(subject, html_out, text_out)


def render_test_email(*, site_name: str, footer_url: str) -> RenderedEmail:
    name = _one_line(site_name, 120) or "your site"
    subject = _one_line(f"Test notification for {name}", _MAX_SUBJECT_CHARS)
    intro = (
        f"<p>This is a test. New leads on <strong>{html.escape(name)}</strong> "
        "will arrive at this address.</p>"
    )
    html_out = _layout(
        heading="Notifications work",
        intro_html=intro,
        body_html="",
        cta=None,
        footer_url=footer_url,
    )
    text_out = f"This is a test. New leads on {name} will arrive at this address." + _text_footer(
        footer_url
    )
    return RenderedEmail(subject, html_out, text_out)


__all__ = [
    "EmailConfig",
    "EmailMessage",
    "RenderedEmail",
    "SendResult",
    "api_base_url",
    "app_base_url",
    "is_configured",
    "lead_url",
    "load_config",
    "render_confirm_email",
    "render_lead_email",
    "render_notification_email",
    "render_test_email",
    "safe_reply_to",
    "send_email",
    "site_settings_url",
    "workspace_settings_url",
]
