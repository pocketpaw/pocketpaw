"""Google social sign-in adapter.

Created 2026-07-29 (AM-2).

Google IS an OIDC provider, so this is thin: it reuses the discovery, token
exchange and id_token verification already written for enterprise SSO in
``auth/sso/oidc.py`` (``accounts.google.com`` is already a PROVIDER_PRESETS
entry there). PKCE and the nonce both apply, unlike GitHub.

Verification comes from the ``email_verified`` claim inside the SIGNED id_token
— not from the userinfo endpoint and not from the mere presence of an ``email``
claim. An unverified Google address is refused the same way an unverified
GitHub one is: the identity is returned with ``email=None`` and the service
turns that into a refusal rather than a link.

These credentials are separate from the OSS Drive connector's
``GOOGLE_OAUTH_CLIENT_ID/SECRET`` in ``src/pocketpaw/config.py`` — that one is a
per-install data integration, this is cloud sign-in.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.auth.sso import oidc

from .base import SocialIdentity

logger = logging.getLogger(__name__)

_ISSUER = "https://accounts.google.com"
_SCOPES = ["openid", "email", "profile"]


def _client_id() -> str:
    return os.environ.get("POCKETPAW_GOOGLE_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("POCKETPAW_GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


def identity_from_claims(claims: dict) -> SocialIdentity:
    """Map verified id_token claims onto a SocialIdentity.

    Split out from the network path so the verification rule is testable
    without standing up a fake Google.

    `sub` is the match key: it is Google's stable per-account identifier and
    survives the user changing their email. Matching on the email itself would
    hand the account to whoever next owns that address.
    """
    sub = claims.get("sub")
    if not sub:
        raise CloudError(502, "social.google_no_subject", "Google id_token carried no sub claim")

    raw_email = claims.get("email")
    # The claim is a real boolean in Google's id_token. Anything else — absent,
    # a string, null — is not an assertion of verification, so it is not one.
    verified = claims.get("email_verified") is True
    email = str(raw_email).lower() if (verified and isinstance(raw_email, str)) else None

    if raw_email and not verified:
        logger.info("google: refusing to trust an unverified email claim for sub=%s", sub)

    return SocialIdentity(
        provider="google",
        account_id=str(sub),
        email=email,
        full_name=str(claims.get("name") or ""),
        avatar=str(claims.get("picture") or ""),
    )


class GoogleProvider:
    name = "google"

    def is_configured(self) -> bool:
        return bool(_client_id() and _client_secret())

    def authorize_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": _client_id(),
            "redirect_uri": redirect_uri,
            "scope": " ".join(_SCOPES),
            "state": state,
            # Always show the chooser: a shared browser must not silently sign
            # in whichever Google account was last used.
            "prompt": "select_account",
        }
        if nonce:
            params["nonce"] = nonce
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        # Discovery would give us this, but the authorize endpoint is stable and
        # this keeps the redirect off the network path.
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

    async def exchange(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> SocialIdentity:
        discovery = await oidc.discover(_ISSUER, "google")
        token_endpoint = discovery.get("token_endpoint")
        jwks_uri = discovery.get("jwks_uri")
        if not token_endpoint or not jwks_uri:
            raise CloudError(
                502, "social.google_discovery_incomplete", "Google discovery doc was incomplete"
            )

        tokens = await oidc.exchange_code(
            token_endpoint,
            code,
            _client_id(),
            _client_secret(),
            redirect_uri,
            code_verifier=code_verifier,
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise CloudError(
                502, "social.google_no_id_token", "Google returned no id_token"
            )

        # Verifies RS256 signature, audience, issuer, expiry, and the nonce.
        claims = await oidc.parse_id_token(
            id_token,
            jwks_uri,
            audience=_client_id(),
            issuer=_ISSUER,
            nonce=nonce,
        )
        return identity_from_claims(claims)
