"""GitHub social sign-in adapter.

Created 2026-07-29 (AM-3).

GitHub is NOT an OIDC provider — no discovery document, no ``id_token`` — so it
cannot ride the OIDC path Google uses. It needs a plain OAuth2 code exchange
plus two REST calls, and *which* call answers "is this email verified" is the
entire security question:

    GET /user         -> id, login, name, avatar_url
                         Its `email` field is the user's PUBLIC PROFILE email.
                         It is self-asserted, freely editable, and carries NO
                         verification signal whatsoever. Reading it as an
                         identity is the account-takeover bug.

    GET /user/emails  -> [{email, primary, verified, visibility}]
                         The only authoritative source. Requires the
                         `user:email` scope.

So this adapter takes the primary address that is *also* ``verified: true``,
falls back to any verified address, and otherwise reports ``email=None`` —
which the service turns into a refusal rather than a link.

A user may legitimately decline the `user:email` scope, in which case
/user/emails returns 403 and we report no email. That is a normal outcome for a
careful person, not an error, so it is handled rather than raised.

Two further notes:
  * This is a GitHub **OAuth App**, deliberately not the codeconnect **GitHub
    App**. Sign-in asks for identity only; repository access stays a separate,
    later consent. See the design doc for why.
  * GitHub OAuth Apps do not support PKCE, so ``code_challenge`` is accepted
    and ignored. The protections that do apply are the single-use ``state``
    (see auth/_oauth_state.py) and the client secret on the token exchange.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlencode

import httpx

from pocketpaw_ee.cloud._core.errors import CloudError

from .base import SocialIdentity

logger = logging.getLogger(__name__)

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"

#: Identity only. `user:email` is required — without it /user/emails 403s and
#: no address can be verified, which downgrades every sign-in to a refusal.
_SCOPES = ["read:user", "user:email"]

_TIMEOUT = 10.0


def _api_base() -> str:
    # Mirrors websandbox/githubapp.py so GitHub Enterprise deploys work.
    return os.environ.get("GITHUB_API_BASE", "https://api.github.com").strip().rstrip("/")


def _client_id() -> str:
    return os.environ.get("POCKETPAW_GITHUB_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET", "").strip()


def pick_verified_email(rows: list[dict[str, Any]]) -> str | None:
    """The best VERIFIED address from GET /user/emails, or None.

    Order: verified+primary, then any verified. Unverified rows are never
    eligible no matter how they are flagged — an unverified address on a
    provider account proves only that someone typed it.
    """
    verified = [
        r
        for r in rows
        if isinstance(r, dict) and r.get("verified") is True and isinstance(r.get("email"), str)
    ]
    if not verified:
        return None
    for row in verified:
        if row.get("primary") is True:
            return str(row["email"]).lower()
    return str(verified[0]["email"]).lower()


class GitHubProvider:
    name = "github"

    def is_configured(self) -> bool:
        return bool(_client_id() and _client_secret())

    def authorize_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,  # noqa: ARG002 — GitHub OAuth Apps lack PKCE
        nonce: str | None = None,  # noqa: ARG002 — no id_token to bind a nonce to
    ) -> str:
        params = {
            "client_id": _client_id(),
            "redirect_uri": redirect_uri,
            "scope": " ".join(_SCOPES),
            "state": state,
            # Force the account chooser rather than silently reusing whichever
            # GitHub session the browser happens to hold.
            "allow_signup": "true",
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,  # noqa: ARG002 — no PKCE on GitHub OAuth Apps
        nonce: str | None = None,  # noqa: ARG002
    ) -> SocialIdentity:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            token = await self._access_token(client, code, redirect_uri)
            profile = await self._get(client, token, "/user")
            email = await self._verified_email(client, token)

        account_id = profile.get("id")
        if account_id is None:
            raise CloudError(502, "social.github_no_account_id", "GitHub returned no account id")

        return SocialIdentity(
            provider=self.name,
            account_id=str(account_id),
            email=email,
            full_name=str(profile.get("name") or profile.get("login") or ""),
            avatar=str(profile.get("avatar_url") or ""),
        )

    # -- internals ---------------------------------------------------------

    async def _access_token(self, client: httpx.AsyncClient, code: str, redirect_uri: str) -> str:
        resp = await client.post(
            _TOKEN_URL,
            # Without this header GitHub replies in form-urlencoded, not JSON.
            headers={"Accept": "application/json"},
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        body = resp.json()

        # GitHub reports exchange failures as HTTP 200 with an `error` key, so
        # raise_for_status alone would let a failed exchange through.
        if isinstance(body, dict) and body.get("error"):
            raise CloudError(
                401,
                "social.github_token_exchange_failed",
                str(body.get("error_description") or body["error"]),
            )

        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise CloudError(
                502, "social.github_token_exchange_failed", "GitHub returned no access token"
            )
        return str(token)

    async def _get(self, client: httpx.AsyncClient, token: str, path: str) -> dict[str, Any]:
        resp = await client.get(
            f"{_api_base()}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def _verified_email(self, client: httpx.AsyncClient, token: str) -> str | None:
        """The verified address, or None when GitHub won't vouch for one.

        Never falls back to /user's `email` field. That field is the public
        profile address — self-asserted and unverified — and treating it as an
        identity is precisely the takeover path this adapter exists to close.
        """
        try:
            resp = await client.get(
                f"{_api_base()}/user/emails",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        except httpx.HTTPError:
            logger.warning("github: /user/emails request failed", exc_info=True)
            return None

        if resp.status_code in (403, 404):
            # The user declined `user:email`. A normal choice, not an error —
            # they land on the "sign in with your password, then connect this
            # account" path.
            logger.info("github: /user/emails denied (status=%s)", resp.status_code)
            return None
        if resp.status_code >= 400:
            logger.warning("github: /user/emails returned %s", resp.status_code)
            return None

        rows = resp.json()
        if not isinstance(rows, list):
            return None
        return pick_verified_email(rows)
