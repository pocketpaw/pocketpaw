# ee/pocketpaw_ee/cloud/growth/connector.py — the Mailtrap email-sending
# client for the /growth outbound engine (G-5).
#
# Created 2026-07-27 (feat/growth-g5): new module.
#
# WHY A MODULE AND NOT A YAML ACTION. ``connectors/mailtrap.yaml`` declares the
# credential but ZERO actions, exactly like ``ship.yaml``. A generic
# "send_email" REST action would be reachable from the agent/connector-execute
# surface and would route around the Instinct send gate. Outbound email leaves
# the building through one path only: an approved draft → ``growth.dispatch`` →
# ``email_dispatch.dispatch_email`` → this module.
#
# CREDENTIALS — connector state, never env-inline. The Mailtrap token lives in
# the workspace's ``WorkspaceConnector`` row config blob (keyed on the YAML's
# declared credential name, the same shape stripe/gmail use) and is read back
# through the cloud connector state store (``ws:<workspace_id>`` scope key).
# The central deployment never holds a shared sending key. The token is never
# logged, never put on a domain/DTO/response, and never streamed: it exists
# only as a local in this module's request builder, and every error string this
# module raises is passed through ``_scrub`` so a provider echo can't leak it.
#
# GROWTH_SENDING_DOMAIN — the SECONDARY sending domain, deliberately not the
# apex. Cold outreach gets marked as spam at rates a transactional stream never
# sees; every complaint lands on the sending domain's reputation. Burning a
# secondary domain costs a DNS record and a warm-up. Burning the apex takes
# password resets, invoices and receipts down with it, and reputation recovery
# on a burnt apex is measured in months. So: outreach rides its own domain, and
# this module REFUSES to send when the from-address isn't on it — including
# refusing when the configured sending domain is the deployment's own public
# host (``POCKETPAW_PUBLIC_BASE_URL``), which is the apex by definition.

"""Mailtrap Email Sending API client for the /growth dispatch worker."""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The registry key for ``connectors/mailtrap.yaml`` and the ``provider`` value
# stamped onto every MessageLog row this module produces.
MAILTRAP_CONNECTOR_NAME = "mailtrap"

# Mailtrap Email Sending API — transactional stream. HTTPS JSON, preferred over
# SMTP: one request, a structured error body, and a provider message id we can
# put on the audit row.
MAILTRAP_SEND_URL = "https://send.api.mailtrap.io/api/send"

# Config-blob keys, matching the credential names declared in the YAML.
TOKEN_KEY = "MAILTRAP_API_TOKEN"
FROM_EMAIL_KEY = "MAILTRAP_FROM_EMAIL"
FROM_NAME_KEY = "MAILTRAP_FROM_NAME"

# Deployment-level config: the secondary sending domain (see the module header).
SENDING_DOMAIN_ENV = "GROWTH_SENDING_DOMAIN"

# The deployment's own public host — the apex we refuse to send outreach from.
PUBLIC_BASE_URL_ENV = "POCKETPAW_PUBLIC_BASE_URL"

# Local part used when the workspace hasn't pinned an explicit from-address.
_DEFAULT_LOCAL_PART = "outreach"

_TIMEOUT_SECONDS = 20.0


class EmailSendError(Exception):
    """A send could not be made, or the provider refused it.

    Every message is scrubbed of the workspace's token before it is raised, so
    the string is safe to persist on a ``MessageLog.error`` and to log.
    """


@dataclass(frozen=True)
class SentEmail:
    """What a successful send yields for the audit row."""

    provider: str
    provider_message_id: str | None
    to_address: str
    from_address: str


def _scrub(text: str, secret: str) -> str:
    """Remove a credential from a string before it is logged or persisted.

    The provider can echo request material in an error body; a token that took
    a round trip through their error path is still a token. Belt and braces on
    top of "never put it in the message in the first place".
    """
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text


def resolve_sending_domain() -> str:
    """The configured secondary sending domain. Raises when unusable.

    Fails CLOSED: an unset ``GROWTH_SENDING_DOMAIN`` means nothing goes out,
    rather than falling back to whatever domain the token happens to allow.
    Also refuses the deployment's own public host — see the module header on
    why cold outreach never rides the apex.
    """
    domain = os.environ.get(SENDING_DOMAIN_ENV, "").strip().lower().rstrip(".")
    if not domain:
        raise EmailSendError(
            f"{SENDING_DOMAIN_ENV} is not set — refusing to send outreach without an "
            "explicit secondary sending domain"
        )
    if domain.startswith("www."):
        domain = domain[len("www.") :]

    public_host = urlparse(os.environ.get(PUBLIC_BASE_URL_ENV, "").strip()).hostname or ""
    public_host = public_host.lower().rstrip(".")
    if public_host.startswith("www."):
        public_host = public_host[len("www.") :]
    if public_host and domain == public_host:
        raise EmailSendError(
            f"{SENDING_DOMAIN_ENV} is the deployment's own apex domain — cold outreach "
            "must ride a secondary domain so a spam-complaint burst can never take "
            "transactional mail down with it"
        )
    return domain


async def load_workspace_config(workspace_id: str) -> dict[str, Any]:
    """Read the workspace's Mailtrap connector config from connector state.

    Goes through the cloud ``ConnectorStateStore`` seam (``ws:<workspace_id>``
    scope key), which resolves the ENABLED ``WorkspaceConnector`` row — so
    disabling the connector immediately revokes sending, with no separate
    kill switch to remember. ``get`` returns an awaitable for namespaced keys;
    the file-backed fallback is sync, hence the ``inspect.isawaitable`` dance
    the registry itself uses.
    """
    from pocketpaw_ee.cloud.connectors.state_provider import CloudConnectorStateStore

    result: Any = CloudConnectorStateStore().get(MAILTRAP_CONNECTOR_NAME, f"ws:{workspace_id}")
    if inspect.isawaitable(result):
        result = await result
    return dict(result or {})


def _http_client() -> Any:
    """Build the HTTPS client. A seam so tests inject a MockTransport.

    ``httpx`` is already a core dependency — no Mailtrap SDK is pulled in for
    one JSON POST (and no new package to age past the 7-day supply-chain bar).
    """
    import httpx

    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)


def _resolve_from_address(config: dict[str, Any], sending_domain: str) -> str:
    """Pick and VALIDATE the from-address against the sending domain."""
    raw = str(config.get(FROM_EMAIL_KEY) or "").strip()
    from_address = raw or f"{_DEFAULT_LOCAL_PART}@{sending_domain}"
    _, _, host = from_address.partition("@")
    host = host.strip().lower().rstrip(".")
    if not host or host != sending_domain:
        raise EmailSendError(
            f"from-address is not on the configured sending domain "
            f"('{sending_domain}') — refusing to send"
        )
    return from_address


async def send_email(
    *,
    workspace_id: str,
    to_address: str,
    subject: str,
    body: str,
) -> SentEmail:
    """Send one outreach email through Mailtrap. Raises ``EmailSendError``.

    The caller (``email_dispatch``) turns any raise into a ``failed``
    MessageLog row and leaves the draft ``approved`` so the send is retryable.
    """
    to_address = (to_address or "").strip()
    if "@" not in to_address:
        raise EmailSendError("prospect has no usable email address")
    if not (subject or "").strip():
        raise EmailSendError("draft has no subject — refusing to send a subject-less cold email")
    if not (body or "").strip():
        raise EmailSendError("draft has no body — refusing to send an empty email")

    sending_domain = resolve_sending_domain()
    config = await load_workspace_config(workspace_id)
    token = str(config.get(TOKEN_KEY) or "").strip()
    if not token:
        raise EmailSendError(
            "the mailtrap connector is not configured for this workspace — "
            "enable it and supply the sending token"
        )
    from_address = _resolve_from_address(config, sending_domain)
    from_name = str(config.get(FROM_NAME_KEY) or "").strip()

    sender: dict[str, str] = {"email": from_address}
    if from_name:
        sender["name"] = from_name
    payload = {
        "from": sender,
        "to": [{"email": to_address}],
        "subject": subject,
        "text": body,
    }

    client = _http_client()
    try:
        async with client:
            response = await client.post(
                MAILTRAP_SEND_URL,
                json=payload,
                headers={"Api-Token": token},
            )
    except Exception as exc:  # noqa: BLE001 — transport failures are retryable sends
        # Type name only: an httpx exception's str() can carry request detail,
        # and this string is persisted on the audit row.
        raise EmailSendError(f"mailtrap request failed ({type(exc).__name__})") from exc

    if response.status_code >= 400:
        detail = _scrub((response.text or "")[:200], token)
        raise EmailSendError(f"mailtrap rejected the send (HTTP {response.status_code}): {detail}")

    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — a 2xx with an unparseable body still sent
        data = {}
    if isinstance(data, dict) and data.get("success") is False:
        errors = data.get("errors")
        detail = _scrub(str(errors)[:200] if errors else "no detail", token)
        raise EmailSendError(f"mailtrap reported failure: {detail}")

    message_ids = data.get("message_ids") if isinstance(data, dict) else None
    provider_message_id = (
        str(message_ids[0]) if isinstance(message_ids, list) and message_ids else None
    )
    return SentEmail(
        provider=MAILTRAP_CONNECTOR_NAME,
        provider_message_id=provider_message_id,
        to_address=to_address,
        from_address=from_address,
    )


__all__ = [
    "FROM_EMAIL_KEY",
    "FROM_NAME_KEY",
    "MAILTRAP_CONNECTOR_NAME",
    "MAILTRAP_SEND_URL",
    "PUBLIC_BASE_URL_ENV",
    "SENDING_DOMAIN_ENV",
    "TOKEN_KEY",
    "EmailSendError",
    "SentEmail",
    "load_workspace_config",
    "resolve_sending_domain",
    "send_email",
]
