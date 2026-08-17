# ee/pocketpaw_ee/cloud/growth/msg91.py — the MSG91 WhatsApp provider seam for
# the /growth outbound engine (G-6).
#
# MSG91 is an official Meta WhatsApp Business Solution Provider, so a
# business-initiated message goes out as a PRE-APPROVED TEMPLATE — free-form
# text is only legal inside a 24-hour service window opened by the recipient.
# That is why this module only knows how to send a template: the draft body
# rides in as a template variable, never as raw content.
#
# CREDENTIALS — the authkey is resolved through the repo's connector state
# pattern (``CloudConnectorStateStore``, the same seam
# ``registry.ensure_connected`` uses to rehydrate a connector on a cold
# process), keyed ``ws:<workspace_id>`` against the workspace's ``msg91``
# ``WorkspaceConnector`` row. Three consequences worth stating plainly:
#
#   * NEVER env-inline. There is no ``MSG91_AUTHKEY`` fallback. A cloud install
#     is multi-tenant; a deployment-global provider key would let one tenant's
#     outbound traffic burn another tenant's WABA quality rating. No row, no
#     send.
#   * NEVER logged. ``Msg91Credentials.__repr__`` redacts the authkey, so an
#     exception traceback, a ``logger.info("...%r", creds)``, or a debugger
#     frame dump cannot spill it. The only place the raw value is read is the
#     outbound request header.
#   * NEVER DTO'd. Nothing in this module returns credentials to a caller that
#     serialises; the growth DTOs have no credential field at all.
#
# The preferred storage shape is ``authkey_enc`` — a ``_core.crypto`` Fernet
# ciphertext, so the plaintext key is not sitting in Mongo and is not exposed by
# the connectors entity's own ``ConnectorResponse.config`` echo. A plaintext
# ``authkey`` key is accepted for installs with no ``CLOUD_ENCRYPTION_KEY``, and
# logs a warning on every resolve so the gap is visible.
#
# Created 2026-07-27 (feat/growth-g6): new module — MSG91 WhatsApp dispatch with
# hard opt-in enforcement.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The registry/connector-row name the workspace's MSG91 credentials live under.
MSG91_CONNECTOR_NAME = "msg91"

# MSG91's v5 WhatsApp outbound endpoint. Overridable per workspace via the
# connector row's ``base_url`` (regional mirrors / a recorded test double),
# never via a process-wide env var.
DEFAULT_MSG91_BASE_URL = "https://api.msg91.com"
_OUTBOUND_PATH = "/api/v5/whatsapp/whatsapp-outbound-message/bulk/"

# A template send is a single small POST; a slow provider must not pin an arq
# worker slot open indefinitely.
_REQUEST_TIMEOUT_SECONDS = 30


def _scrub(text: str, secret: str) -> str:
    """Remove the authkey from provider text before it is raised or stored.

    Mirrors ``connector._scrub`` and exists for the same reason: an MSG91
    error body can echo the request headers back, and an authkey that took a
    round trip through their error path is still an authkey. Without this, a
    401 whose body quotes the submitted key landed verbatim in a durable
    MessageLog row (``whatsapp.py`` writes ``exc.message`` as ``error``).
    """
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text


# ORDER MATTERS AT EVERY CALL SITE: scrub the WHOLE body, then truncate. Slicing
# first can cut a key in half across the boundary, so ``secret in text`` misses
# and the prefix survives into a durable MessageLog row — which is precisely the
# leak this function exists to stop.


class Msg91Error(Exception):
    """The provider refused or failed the send.

    ``code`` is machine-readable so the send log can record WHY without
    re-parsing prose. The message is truncated by the caller before it reaches
    the log — a provider error body must never carry the request headers back
    into storage.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Msg91NotConfigured(Exception):
    """No usable MSG91 credentials for this workspace.

    Deliberately distinct from ``Msg91Error``: nothing was attempted, so the
    send log records a ``blocked`` row rather than a ``failed`` one.
    """


@dataclass(frozen=True, repr=False)
class Msg91Credentials:
    """One workspace's resolved MSG91 WhatsApp configuration.

    ``__repr__`` is hand-written to redact ``authkey``. The dataclass default
    would interpolate it into every traceback and every ``%r`` format — the
    single most common way a provider key ends up in a log aggregator.
    """

    authkey: str
    integrated_number: str
    template_name: str
    language_code: str = "en"
    namespace: str | None = None
    base_url: str = DEFAULT_MSG91_BASE_URL

    def __repr__(self) -> str:
        return (
            "Msg91Credentials(authkey='***redacted***', "
            f"integrated_number={self.integrated_number!r}, "
            f"template_name={self.template_name!r}, "
            f"language_code={self.language_code!r}, "
            f"namespace={self.namespace!r}, base_url={self.base_url!r})"
        )

    __str__ = __repr__


def _read_authkey(config: dict[str, Any]) -> str:
    """Pull the authkey out of a connector config blob.

    ``authkey_enc`` (Fernet ciphertext) wins; a plaintext ``authkey`` is the
    documented fallback for installs without ``CLOUD_ENCRYPTION_KEY`` and warns
    every time so the weaker posture is visible in the logs. A ciphertext that
    fails to decrypt returns "" — the caller treats that as "not configured"
    rather than falling through to a half-resolved credential.
    """
    enc = str(config.get("authkey_enc") or "").strip()
    if enc:
        try:
            from pocketpaw_ee.cloud._core import crypto

            return str(crypto.decrypt(enc)).strip()
        except Exception:  # noqa: BLE001 — an undecryptable key is "not configured"
            logger.warning(
                "growth/msg91: the stored authkey_enc could not be decrypted — is "
                "CLOUD_ENCRYPTION_KEY the key it was written with? Refusing to send."
            )
            return ""
    plain = str(config.get("authkey") or "").strip()
    if plain:
        logger.warning(
            "growth/msg91: the MSG91 authkey is stored in PLAINTEXT on the connector "
            "row. Set CLOUD_ENCRYPTION_KEY and re-save it as authkey_enc."
        )
    return plain


async def resolve_credentials(workspace_id: str) -> Msg91Credentials:
    """Resolve one workspace's MSG91 credentials, or raise ``Msg91NotConfigured``.

    Reads the workspace's ``msg91`` connector row through the connector state
    store — the same seam the OSS connector registry uses to rehydrate a
    connector's config on a cold process. There is deliberately NO env-var
    fallback for the authkey (see the module header).
    """
    if not workspace_id:
        raise Msg91NotConfigured("no workspace — cannot resolve MSG91 credentials")

    from pocketpaw_ee.cloud.connectors.state_provider import CloudConnectorStateStore

    raw = CloudConnectorStateStore().get(MSG91_CONNECTOR_NAME, f"ws:{workspace_id}")
    # The store returns a coroutine for namespaced (``ws:``) keys and a plain
    # value for the file-delegate path; await only what needs awaiting.
    config = await raw if hasattr(raw, "__await__") else raw
    if not isinstance(config, dict):
        raise Msg91NotConfigured(
            f"workspace {workspace_id} has no enabled '{MSG91_CONNECTOR_NAME}' connector"
        )

    authkey = _read_authkey(config)
    integrated_number = str(config.get("integrated_number") or "").strip()
    template_name = str(config.get("template_name") or "").strip()
    missing = [
        name
        for name, value in (
            ("authkey", authkey),
            ("integrated_number", integrated_number),
            ("template_name", template_name),
        )
        if not value
    ]
    if missing:
        # The NAMES of the missing fields are safe to log; the values are not,
        # and no value is interpolated here.
        raise Msg91NotConfigured(
            f"the '{MSG91_CONNECTOR_NAME}' connector for workspace {workspace_id} is "
            f"missing: {', '.join(missing)}"
        )

    return Msg91Credentials(
        authkey=authkey,
        integrated_number=integrated_number,
        template_name=template_name,
        language_code=str(config.get("language_code") or "en").strip() or "en",
        namespace=(str(config.get("namespace")).strip() or None)
        if config.get("namespace")
        else None,
        base_url=str(config.get("base_url") or DEFAULT_MSG91_BASE_URL).rstrip("/"),
    )


class Msg91WhatsAppClient:
    """Minimal MSG91 WhatsApp template-send client.

    One method, one endpoint. ``httpx`` is already a core dependency, so this
    adds no new package (and therefore no 7-day release-age exposure). Tests
    inject a fake with the same ``send_template`` signature — nothing in the
    test suite ever constructs this class, so the suite is network-free by
    construction rather than by mock-patching ``httpx``.
    """

    def __init__(self, credentials: Msg91Credentials) -> None:
        self._creds = credentials

    def __repr__(self) -> str:
        # Never let the wrapped credentials leak through the client's repr.
        return f"Msg91WhatsAppClient(base_url={self._creds.base_url!r})"

    async def send_template(self, *, to_number: str, body_text: str) -> str:
        """Send the pre-approved template to one number; return the provider id.

        ``body_text`` fills the template's first body variable — the draft copy
        a human approved in the Tray. Raises ``Msg91Error`` on a non-2xx or an
        error-shaped 2xx body.
        """
        import httpx

        creds = self._creds
        template: dict[str, Any] = {
            "name": creds.template_name,
            "language": {"code": creds.language_code, "policy": "deterministic"},
            "to_and_components": [
                {
                    "to": [to_number],
                    "components": {"body_1": {"type": "text", "value": body_text}},
                }
            ],
        }
        if creds.namespace:
            template["namespace"] = creds.namespace

        payload = {
            "integrated_number": creds.integrated_number,
            "content_type": "template",
            "payload": {
                "messaging_product": "whatsapp",
                "type": "template",
                "template": template,
            },
        }

        url = f"{creds.base_url}{_OUTBOUND_PATH}"
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    # The ONLY place the raw authkey is read. Not logged, not
                    # returned, not stored.
                    headers={"authkey": creds.authkey, "Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise Msg91Error("msg91.transport_error", f"MSG91 request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise Msg91Error(
                "msg91.http_error",
                f"MSG91 returned {resp.status_code}: {_scrub(resp.text, creds.authkey)[:300]}",
            )
        return _extract_message_id(resp, creds.authkey)


def _extract_message_id(resp: Any, authkey: str = "") -> str:
    """Best-effort provider message id out of an MSG91 2xx response.

    MSG91 has shipped several 2xx envelopes over the v5 lifetime; the id is not
    load-bearing for us (the send log's own id is), so an unrecognised shape
    returns "" instead of failing a send that actually went out. An explicit
    error-shaped 2xx DOES raise — those are real failures wearing a 200.
    """
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON 2xx still means it went out
        return ""
    if not isinstance(data, dict):
        return ""
    status = str(data.get("status") or data.get("type") or "").lower()
    if status in ("error", "fail", "failure"):
        raise Msg91Error(
            "msg91.rejected", f"MSG91 rejected the send: {_scrub(str(data), authkey)[:300]}"
        )
    inner = data.get("data")
    if isinstance(inner, dict):
        for key in ("message_id", "messageId", "request_id", "requestId", "id"):
            if inner.get(key):
                return str(inner[key])
    for key in ("message_id", "messageId", "request_id", "requestId"):
        if data.get(key):
            return str(data[key])
    return ""


__all__ = [
    "DEFAULT_MSG91_BASE_URL",
    "MSG91_CONNECTOR_NAME",
    "Msg91Credentials",
    "Msg91Error",
    "Msg91NotConfigured",
    "Msg91WhatsAppClient",
    "resolve_credentials",
]
