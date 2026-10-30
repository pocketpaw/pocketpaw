# ee/pocketpaw_ee/integrations/providers/resend.py: the ``resend`` provider.
#
# Kind ``connect`` (Resend OAuth 2.1: authorization code + PKCE S256, which Resend
# requires of every client), category ``email``, plan ``site``. The app is
# ``POCKETPAW_INTEGRATIONS_RESEND_CLIENT_ID`` (pre-registered with Resend), plus
# ``_CLIENT_SECRET`` when it was registered as a confidential client (then sent as
# HTTP Basic on the token endpoint); without a client id it is ``unconfigured``.
# Scope ``emails:send`` only. Access tokens last 15 minutes and every refresh
# rotates the refresh token (a replayed one is ``invalid_grant`` and can revoke
# the grant), so refreshes go through the vault lease like every other provider.
# Resend documents no identity endpoint for a token, so the connection carries no
# ``account_ref`` and its label is "Resend".
#
# WHAT IT DOES. The owner's own sender for the site's lead notifications: once a
# site has a connected ``resend`` binding with a ``from_address`` on a domain the
# owner verified in Resend, ``owner_send`` mails that site's notifications
# (new lead, handoff, booking, recipient confirm, test) through the owner's Resend
# account instead of the platform's Cloudflare sender. There are no public
# actions: a site visitor can't send mail through it.
#
# FALLBACK. ``owner_send`` returns None (the caller uses the platform sender) when
# the site has no usable binding, the grant is dead (``needs_reconnect``), or
# Resend refuses the sender (any 4xx other than 409 / 429 after one refresh,
# e.g. 403 domain not verified). A refused sender also marks the binding
# ``setup_required`` (``sender_rejected``) so the owner sees it. 409 / 429 / 5xx /
# transport errors are retried by the outbox with the same ``Idempotency-Key``.
#
# Nothing here logs a token, an address list or a message body.

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urlencode

from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers.base import (
    ActionRefused,
    Availability,
    BaseProvider,
    ProviderError,
    SettingsField,
    SetupContext,
    TokenGrant,
)

PROVIDER_ID = "resend"
ENV_PREFIX = "POCKETPAW_INTEGRATIONS_RESEND"
AUTHORIZE_URL = "https://api.resend.com/oauth/authorize"
TOKEN_URL = "https://api.resend.com/oauth/token"
EMAILS_URL = "https://api.resend.com/emails"
SCOPE = "emails:send"
MAX_ADDRESS = 320
MAX_NAME = 80
SENDER_REJECTED = "sender_rejected"

# One address, no display name or line break: the name is a separate setting.
_ADDRESS = re.compile(
    r"[^\s@<>\"',;:]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)+"
)
_NAME_DROP = re.compile(r"[\x00-\x1f\x7f<>\"\\]")


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def valid_address(value: Any) -> str:
    """``value`` as a bare sender address, else ""."""
    text = str(value or "").strip()
    return text if len(text) <= MAX_ADDRESS and _ADDRESS.fullmatch(text) else ""


def sender(settings: Any) -> str:
    """``Name <address>`` (or the bare address) from the binding settings, else ""."""
    address = valid_address((settings or {}).get("from_address"))
    if not address:
        return ""
    name = " ".join(_NAME_DROP.sub("", str((settings or {}).get("from_name") or "")).split())
    return f'"{name[:MAX_NAME]}" <{address}>' if name else address


class ResendProvider(BaseProvider):
    id = PROVIDER_ID
    name = "Resend"
    kind = "connect"
    category = "email"
    plan = "site"
    supports_pkce = True
    actions = {}
    settings_fields = (
        SettingsField(
            key="from_address",
            label="Send from",
            help="An address on a domain you verified in Resend, like leads@yourshop.com. "
            "Lead notifications for this site go out from it.",
            required=True,
            max_length=MAX_ADDRESS,
        ),
        SettingsField(
            key="from_name",
            label="Sender name",
            help="Shown as the sender, like your business name. Optional.",
            max_length=MAX_NAME,
        ),
    )

    def availability(self) -> Availability:
        client_id, _ = oauth_http.client_credentials(ENV_PREFIX)
        return "available" if client_id else "unconfigured"

    def _client_auth(self) -> tuple[dict[str, str], dict[str, str]]:
        """(extra form fields, headers) identifying the client at the token endpoint."""
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        if not client_id:
            raise ProviderError("resend: not configured")
        if not secret:
            return {"client_id": client_id}, {}
        raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
        return {}, {"authorization": f"Basic {raw}"}

    def authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str:
        client_id, _ = oauth_http.client_credentials(ENV_PREFIX)
        if not client_id:
            raise ProviderError("resend: not configured")
        if not code_challenge:
            raise ProviderError("resend: PKCE challenge missing")
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenGrant:
        if not code_verifier:
            raise ProviderError("resend: PKCE verifier missing")
        fields, headers = self._client_auth()
        body = await oauth_http.token_request(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                **fields,
            },
            provider=self.id,
            headers=headers,
        )
        scopes = str(body.get("scope") or "").split()
        if SCOPE not in scopes:
            raise ProviderError("resend: the grant can't send email")
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=scopes,
            account_label="Resend",
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:  # type: ignore[override]
        fields, headers = self._client_auth()
        body = await oauth_http.token_request(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": refresh_token, **fields},
            provider=self.id,
            headers=headers,
        )
        return TokenGrant(
            access_token=str(body["access_token"]),
            # Rotating: the old refresh token is spent now; the vault keeps this one.
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=str(body.get("scope") or "").split(),
        )

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:  # noqa: ARG002
        """A sender that isn't one plain address leaves the site setup_required."""
        settings = dict(getattr(binding, "settings", None) or {})
        if settings.get("from_address") and not valid_address(settings.get("from_address")):
            raise ActionRefused(
                "setup_required", "Enter one email address.", setup_error="invalid_sender"
            )
        return {}


# ---------------------------------------------------------------------------
# The owner's sender
# ---------------------------------------------------------------------------


async def owner_send(workspace_id: str, site_ref: str, message: Any, *, idempotency_key: str):
    """Send ``message`` (a ``notifications.email.EmailMessage``) through the Resend
    account the site's owner connected. Returns a ``SendResult``, or None when the
    platform sender should take it (see the header)."""
    from pocketpaw_ee.cloud.leads import notification_settings as ns
    from pocketpaw_ee.cloud.notifications.email import SendResult
    from pocketpaw_ee.integrations import bindings, vault
    from pocketpaw_ee.integrations.providers import get_provider

    site = await ns.find_site(workspace_id, site_ref) if site_ref else None
    if site is None or site.workspace != workspace_id:
        return None
    binding = await bindings.binding_for_site(site, PROVIDER_ID)
    provider = get_provider(PROVIDER_ID) if binding is not None else None
    if binding is None or provider is None:
        return None
    conn = await bindings.connection_for(binding)
    if bindings.binding_status(binding, provider, conn) != "connected":
        return None
    from_header = sender(binding.settings)
    if not from_header:
        return None

    async def _token(rejected: str = "") -> str:
        return await vault.get_valid_token(conn, rejected=rejected)

    body: dict[str, Any] = {
        "from": from_header,
        "to": list(message.to),
        "subject": message.subject,
        "html": message.html,
        "text": message.text,
    }
    if message.reply_to:
        body["reply_to"] = message.reply_to
    if message.headers:
        body["headers"] = dict(message.headers)
    try:
        resp = await oauth_http.authed(
            _token,
            "POST",
            EMAILS_URL,
            provider=PROVIDER_ID,
            json=body,
            headers={"Idempotency-Key": idempotency_key[:256]},
        )
    except vault.NotConnected:
        return None
    except ProviderError as exc:
        return SendResult(outcome="retry", error=str(exc)[:200])
    code = resp.status_code
    if 200 <= code < 300:
        data = oauth_http.body_of(resp)
        return SendResult(
            outcome="sent",
            status_code=code,
            message_id=str(data.get("id") or ""),
            delivered=list(message.to),
        )
    if code in (409, 429) or code >= 500:
        return SendResult(outcome="retry", error=f"resend http {code}", status_code=code)
    # The owner's sender can't send this (unverified domain, a revoked or
    # under-scoped grant, quota): the platform sender takes it, the owner is told.
    await bindings.mark_setup_error(binding, SENDER_REJECTED)
    return None
