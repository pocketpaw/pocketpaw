# ee/pocketpaw_ee/cloud/notifications/whatsapp_notify.py
# The provider seam for WhatsApp alerts to a SITE OWNER's own numbers (outbox
# sink ``whatsapp_owner``): which provider sends, the message text, the Meta
# request body, and how a Meta answer maps to sent / retry / dead. The outbox
# calls ``send``; nothing here touches the database.
#
# Providers: ``MetaCloudProvider`` when ``POCKETPAW_WA_NOTIFY_ACCESS_TOKEN`` and
# ``POCKETPAW_WA_NOTIFY_PHONE_NUMBER_ID`` are both set, else ``MockProvider``
# (test mode: one log line with the number masked, reported as sent with
# provider "mock"). ``mode()`` says which, for the settings API.
#
# Meta: ``POST https://graph.facebook.com/{version}/{phone_number_id}/messages``
# with a Bearer token, over the outbox's httpx client (a fixed public host, so
# the SafeFetcher's per-send DNS pinning buys nothing here). ``send_as=text`` is
# a free-form message (Meta only delivers it inside the 24 h window after the
# recipient messaged the business number); ``template`` (the default) sends the
# named template with the alert text as body parameter 1, except ``hello_world``,
# which takes no variables. 2xx is sent; 429, 5xx, a throttling code or a
# transport error retries; any other 4xx is dead, with 131047 (outside the 24 h
# window) and 131030 (not in the test number's allowed list) spelled out. Only
# our own short error string is stored, never Meta's text.
#
# The token is never logged: ``WaNotifyConfig.__repr__`` hides it, and any text
# we keep from a failure is scrubbed of it first.

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v26.0"
DEFAULT_TEMPLATE = "hello_world"
DEFAULT_TEMPLATE_LANG = "en_US"
SEND_AS = ("text", "template")
# Meta codes that mean "slow down", even when they arrive on a 400.
_THROTTLE_CODES = frozenset({4, 80007, 130429, 131056})
_PERMANENT_HINTS = {
    131047: "outside the 24h window: the number must message the business number "
    "first, or send as an approved template",
    131030: "recipient is not in the allowed list of the test number",
    190: "access token expired or invalid; check POCKETPAW_WA_NOTIFY_ACCESS_TOKEN",
}
_VERSION = re.compile(r"^v\d{1,3}\.\d{1,2}$")
_DIGITS = re.compile(r"^\d{5,20}$")

Mode = Literal["mock", "live"]


@dataclass(frozen=True)
class SendResult:
    status: Literal["sent", "retry", "dead"]
    error: str = ""
    provider: str = ""


@dataclass(frozen=True)
class WaNotifyConfig:
    access_token: str = field(repr=False)
    phone_number_id: str
    send_as: str = "template"
    template: str = DEFAULT_TEMPLATE
    template_lang: str = DEFAULT_TEMPLATE_LANG
    api_version: str = DEFAULT_API_VERSION

    def __repr__(self) -> str:  # the token must never reach a log or traceback
        return (
            f"WaNotifyConfig(access_token='***', phone_number_id={self.phone_number_id!r}, "
            f"send_as={self.send_as!r}, template={self.template!r}, "
            f"template_lang={self.template_lang!r}, api_version={self.api_version!r})"
        )


def _setting(settings: Any, name: str) -> str:
    return str(getattr(settings, name, None) or "").strip()


def load_config() -> WaNotifyConfig | None:
    """The Meta config, or None (mock mode) while the token or phone number id
    is unset. A malformed phone number id is treated as unset, with a warning."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    token = _setting(settings, "wa_notify_access_token")
    phone_id = _setting(settings, "wa_notify_phone_number_id")
    if not (token and phone_id):
        return None
    if not _DIGITS.match(phone_id):
        logger.warning("POCKETPAW_WA_NOTIFY_PHONE_NUMBER_ID is not numeric; using mock mode")
        return None
    send_as = _setting(settings, "wa_notify_send_as").lower() or "template"
    if send_as not in SEND_AS:
        logger.warning("POCKETPAW_WA_NOTIFY_SEND_AS=%r is unknown; sending templates", send_as)
        send_as = "template"
    version = _setting(settings, "wa_notify_api_version") or DEFAULT_API_VERSION
    if not _VERSION.match(version):
        logger.warning("POCKETPAW_WA_NOTIFY_API_VERSION=%r is malformed; using default", version)
        version = DEFAULT_API_VERSION
    return WaNotifyConfig(
        access_token=token,
        phone_number_id=phone_id,
        send_as=send_as,
        template=_setting(settings, "wa_notify_template") or DEFAULT_TEMPLATE,
        template_lang=_setting(settings, "wa_notify_template_lang") or DEFAULT_TEMPLATE_LANG,
        api_version=version,
    )


def mode() -> Mode:
    return "live" if load_config() is not None else "mock"


def send_as() -> str:
    """What a live send would use (``template`` in mock mode too, the default)."""
    config = load_config()
    return config.send_as if config is not None else "template"


def mask(e164: str) -> str:
    """``+919876543210`` -> ``+91******3210``, for logs."""
    if len(e164) <= 6:
        return "***"
    return e164[:3] + "*" * (len(e164) - 7) + e164[-4:]


# ---------------------------------------------------------------------------
# Message text
# ---------------------------------------------------------------------------


def event_text(*, site_name: str, title: str, body: str) -> str:
    """The alert for an event that has no lead (a concierge handoff): one line,
    defanged, capped like the lead text."""
    from pocketpaw_ee.cloud.notifications.outbox import WHATSAPP_BODY_CAP, _defang, _one_line

    site = _one_line(site_name, 80) or "your site"
    head = f"{_one_line(title, 80) or 'New activity'} on {site} via Paw Sites by PocketPaw"
    room = WHATSAPP_BODY_CAP - len(head) - len(": ")
    detail = _one_line(_defang(body), room) if room > 1 else ""
    return (head + (f": {detail}" if detail else ""))[:WHATSAPP_BODY_CAP]


def probe_text(site_name: str) -> str:
    from pocketpaw_ee.cloud.notifications.outbox import _one_line

    site = _one_line(site_name, 80) or "your site"
    return f"Test from PocketPaw: new leads for {site} will arrive on this number."


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class MockProvider:
    """Test mode: nothing leaves the server."""

    name = "mock"

    async def send(self, to: str, text: str, *, client: Any = None) -> SendResult:
        logger.info("whatsapp notify (test mode, not sent): to=%s chars=%d", mask(to), len(text))
        return SendResult("sent", provider=self.name)


class MetaCloudProvider:
    """The Meta WhatsApp Cloud API."""

    name = "meta"

    def __init__(self, config: WaNotifyConfig) -> None:
        self.config = config

    def __repr__(self) -> str:
        return f"MetaCloudProvider({self.config!r})"

    @property
    def url(self) -> str:
        c = self.config
        return f"{GRAPH_BASE_URL}/{c.api_version}/{c.phone_number_id}/messages"

    def payload(self, to: str, text: str) -> dict[str, Any]:
        recipient = to.lstrip("+")
        c = self.config
        if c.send_as == "text":
            return {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": text, "preview_url": False},
            }
        template: dict[str, Any] = {"name": c.template, "language": {"code": c.template_lang}}
        if c.template != DEFAULT_TEMPLATE:
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": text}]}
            ]
        return {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": template,
        }

    async def send(self, to: str, text: str, *, client: Any) -> SendResult:
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(self.url, json=self.payload(to, text), headers=headers)
        except Exception as exc:  # noqa: BLE001 — any transport failure retries
            return SendResult("retry", f"meta transport: {type(exc).__name__}", self.name)
        return classify(response.status_code, _json(response), token=self.config.access_token)


def _json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error page
        return None


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token and token in text else text


def classify(status: int, body: Any, *, token: str = "") -> SendResult:
    """Map a Graph API answer to a send result (see the module header)."""
    if 200 <= status < 300:
        return SendResult("sent", provider=MetaCloudProvider.name)
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    code = code if isinstance(code, int) else None
    label = f"meta http {status}" + (f" code {code}" if code is not None else "")
    if code in _PERMANENT_HINTS:
        if code == 190:
            logger.error("whatsapp notify: Meta rejected the access token (%s)", label)
        return SendResult("dead", f"{label}: {_PERMANENT_HINTS[code]}", MetaCloudProvider.name)
    if status == 429 or status >= 500 or code in _THROTTLE_CODES:
        return SendResult("retry", _scrub(label, token), MetaCloudProvider.name)
    if 400 <= status < 500:
        return SendResult("dead", _scrub(label, token), MetaCloudProvider.name)
    return SendResult("retry", _scrub(label, token), MetaCloudProvider.name)


def get_provider() -> MockProvider | MetaCloudProvider:
    config = load_config()
    return MetaCloudProvider(config) if config is not None else MockProvider()


async def send(to: str, text: str, *, client: Any) -> SendResult:
    """Send ``text`` to ``to`` through the configured provider. Never raises."""
    provider = get_provider()
    try:
        return await provider.send(to, text, client=client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("whatsapp notify send crashed (%s)", type(exc).__name__)
        return SendResult("retry", f"crash: {type(exc).__name__}", provider.name)


__all__ = [
    "DEFAULT_API_VERSION",
    "GRAPH_BASE_URL",
    "MetaCloudProvider",
    "MockProvider",
    "SendResult",
    "WaNotifyConfig",
    "classify",
    "event_text",
    "get_provider",
    "load_config",
    "mask",
    "mode",
    "probe_text",
    "send",
    "send_as",
]
