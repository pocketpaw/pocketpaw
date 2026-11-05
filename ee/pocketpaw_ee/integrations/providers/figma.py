# ee/pocketpaw_ee/integrations/providers/figma.py: the ``figma`` design provider.
#
# Kind ``connect`` (Figma OAuth 2, authorization code + PKCE S256), category
# ``design``, plan ``site``. The app is ``POCKETPAW_INTEGRATIONS_FIGMA_CLIENT_ID`` /
# ``_SECRET``; without both it is ``unconfigured`` and every OAuth call refuses.
# Token calls are form posts with HTTP Basic client auth: ``/v1/oauth/token`` for the
# code, ``/v1/oauth/refresh`` (body ``refresh_token`` only) for a new access token.
#
# Scopes are read-only: ``file_content:read file_metadata:read current_user:read``,
# plus ``file_variables:read`` only when the owner picks Enterprise in the optional
# ``plan`` connect param (Figma only grants it on Enterprise). Never the deprecated
# ``files:read``.
#
# Lifetimes: access tokens last 90 days (``expires_in``); the refresh token does NOT
# rotate (the refresh answer carries none, so the vault keeps the old one), and a
# refresh kills the previous access token, which is why every refresh goes through
# the vault lease. A refused refresh (``invalid_grant``) flips needs_reconnect.
#
# WORKSPACE ONLY. ``site_bindable = False`` and ``actions == {}``: the Sites builder
# agent reads Figma for the owner (``agent/mcp_servers/figma.py``); a published site
# can never call it, and the bindings layer refuses to link it to a site.
# ``account_ref`` is Figma's ``user_id_string``; ``meta`` keeps the handle, whether
# the variables scope was granted, and the plan tier / seat type once Figma reveals
# them (rate-limit headers, written by the tools); ``connection_attention`` warns a
# View/Collab seat (Figma allows it about 20 file reads a month).

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers.base import (
    Availability,
    BaseProvider,
    ConnectParam,
    ProviderError,
    SettingOption,
    TokenGrant,
)

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_FIGMA"
API = "https://api.figma.com"
AUTHORIZE_URL = "https://www.figma.com/oauth"
TOKEN_URL = f"{API}/v1/oauth/token"
REFRESH_URL = f"{API}/v1/oauth/refresh"
ME_URL = f"{API}/v1/me"

SCOPES = ("file_content:read", "file_metadata:read", "current_user:read")
VARIABLES_SCOPE = "file_variables:read"
REQUIRED_SCOPES = frozenset({"file_content:read"})
LOW_SEAT_NOTE = (
    "Your Figma seat is View or Collab: Figma allows it only a few file reads a "
    "month. A Dev or Full seat lifts the limit."
)


def scopes_for(params: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The scopes to ask for: the base three, plus variables on Enterprise."""
    if str((params or {}).get("plan") or "") == "enterprise":
        return (*SCOPES, VARIABLES_SCOPE)
    return SCOPES


class FigmaProvider(BaseProvider):
    id = "figma"
    name = "Figma"
    kind = "connect"
    category = "design"
    plan = "site"
    supports_pkce = True
    site_bindable = False
    actions = {}
    settings_fields = ()
    connect_params = (
        ConnectParam(
            key="plan",
            label="Figma plan",
            type="select",
            required=False,
            options=(
                SettingOption(value="standard", label="Starter, Professional or Organization"),
                SettingOption(value="enterprise", label="Enterprise"),
            ),
            help="Enterprise also lets the builder read your design variables.",
        ),
    )

    def availability(self) -> Availability:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        return "available" if client_id and secret else "unconfigured"

    def _credentials(self) -> tuple[str, str]:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        if not client_id or not secret:
            raise ProviderError("figma: not configured")
        return client_id, secret

    def _basic(self) -> dict[str, str]:
        client_id, secret = self._credentials()
        raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
        return {"authorization": f"Basic {raw}"}

    def authorize_url(
        self,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> str:
        client_id, _ = self._credentials()
        if not code_challenge:
            raise ProviderError("figma: PKCE is required")
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes_for(params)),
            "state": state,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def complete(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> TokenGrant:
        if not code_verifier:
            raise ProviderError("figma: PKCE verifier missing")
        body = await oauth_http.token_request(
            TOKEN_URL,
            {
                "redirect_uri": redirect_uri,
                "code": code,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            provider=self.id,
            headers=self._basic(),
        )
        access = str(body["access_token"])
        # Figma's answer has no ``scope``: the grant is what we asked for.
        asked = scopes_for(params)
        who = await self._me(access)
        user_id = str(body.get("user_id_string") or who.get("id") or "")
        if not user_id:
            raise ProviderError("figma: no user id in the grant")
        handle = str(who.get("handle") or "")[:100]
        connect = {k: str(v) for k, v in (params or {}).items() if k == "plan"}
        return TokenGrant(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=list(asked),
            account_label=handle or "Figma account",
            account_ref=user_id,
            meta={
                "handle": handle,
                "variables": VARIABLES_SCOPE in asked,
                "plan_tier": "enterprise" if VARIABLES_SCOPE in asked else "",
                "connect_params": connect,
            },
        )

    async def refresh(
        self,
        refresh_token: str,
        meta: Mapping[str, Any] | None = None,  # noqa: ARG002
    ) -> TokenGrant:
        if not refresh_token:
            raise ProviderError("figma: no refresh token")
        body = await oauth_http.token_request(
            REFRESH_URL,
            {"refresh_token": refresh_token},
            provider=self.id,
            headers=self._basic(),
        )
        # No refresh token comes back: the vault keeps the one it has.
        return TokenGrant(
            access_token=str(body["access_token"]),
            expires_in=_int(body.get("expires_in")),
        )

    async def _me(self, access: str) -> dict[str, Any]:
        try:
            async with oauth_http.client() as http:
                resp = await http.get(ME_URL, headers={"authorization": f"Bearer {access}"})
        except Exception:  # noqa: BLE001 - the label is optional; the grant still holds
            return {}
        return oauth_http.body_of(resp) if resp.status_code < 400 else {}

    def connection_attention(self, connection: Any) -> str | None:
        meta = getattr(connection, "meta", None) or {}
        if meta.get("rate_limit_type") == "low":
            return LOW_SEAT_NOTE
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
