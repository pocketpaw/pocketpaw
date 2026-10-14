# ee/pocketpaw_ee/integrations/providers/hubspot.py: the ``hubspot`` CRM provider.
#
# Kind ``connect`` (HubSpot OAuth, authorization code), category ``crm``, no
# per-site settings. The app is configured by ``POCKETPAW_INTEGRATIONS_HUBSPOT_
# CLIENT_ID`` / ``_SECRET``; without both it is ``unconfigured``. Scopes:
# ``oauth crm.objects.contacts.read crm.objects.contacts.write``; a grant without
# contacts write is refused. ``complete`` reads the token-info endpoint:
# ``account_ref`` = hub_id, ``account_label`` = hub_domain (or the user). Access
# tokens live ~30 minutes (``expires_in`` is read, never assumed); refresh tokens
# do not expire. ``BAD_REFRESH_TOKEN`` / ``invalid_grant`` raise InvalidGrant.
#
# ``create_contact`` (public write) upserts by email: create, and on a 409 (the
# email exists) read that contact and PATCH (``idProperty=email``) only the
# properties that are empty on it: a stranger can fill gaps, never overwrite a
# stored value. The token-info call carries the token in its URL path, so it runs
# under ``oauth_http.untraced()`` and never lands in a Logfire span. ``message``
# goes to HubSpot's default ``message`` contact property. A 401 refreshes once;
# a 429 is ``rate_limited``; a 400 from HubSpot is ``invalid_args``.

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from pydantic import Field, field_validator

from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers.base import (
    ActionArgs,
    ActionContext,
    ActionRefused,
    ActionSpec,
    Availability,
    BaseProvider,
    ProviderError,
    TokenGrant,
)
from pocketpaw_ee.integrations.providers.newsletter import _EMAIL_RE, MAX_EMAIL

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_HUBSPOT"
AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
API = "https://api.hubapi.com"
TOKEN_URL = f"{API}/oauth/v1/token"
TOKEN_INFO_URL = f"{API}/oauth/v1/access-tokens"
CONTACTS_URL = f"{API}/crm/v3/objects/contacts"
CONTACTS_WRITE = "crm.objects.contacts.write"
SCOPES = ("oauth", "crm.objects.contacts.read", CONTACTS_WRITE)


class CreateContactArgs(ActionArgs):
    email: str = Field(max_length=MAX_EMAIL)
    firstname: str = Field(default="", max_length=100)
    lastname: str = Field(default="", max_length=100)
    phone: str = Field(default="", max_length=40)
    company: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=2000)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("firstname", "lastname", "phone", "company", "message")
    @classmethod
    def _no_controls(cls, value: str) -> str:
        return "".join(c for c in value if c in "\n\t" or (ord(c) >= 0x20 and ord(c) != 0x7F))


def _refuse_for(resp: Any, what: str) -> None:
    status = resp.status_code
    if status < 400:
        return
    if status == 429:
        raise ActionRefused("rate_limited")
    if status == 400:
        raise ActionRefused("invalid_args", "HubSpot didn't accept those contact details.")
    raise ProviderError(f"hubspot: {what} answered {status}")


async def _create_contact(ctx: ActionContext, args: CreateContactArgs) -> dict[str, Any]:
    properties = {k: v for k, v in args.model_dump().items() if v}
    created = await oauth_http.authed(
        ctx.get_token,
        "POST",
        CONTACTS_URL,
        provider="hubspot",
        json={"properties": properties},
    )
    if created.status_code != 409:
        _refuse_for(created, "create contact")
        return {"status": "created"}
    # The email exists. A public caller may only fill what is empty on it.
    contact_url = f"{CONTACTS_URL}/{quote(args.email, safe='')}"
    fillable = [k for k in properties if k != "email"]
    existing = await oauth_http.authed(
        ctx.get_token,
        "GET",
        contact_url,
        provider="hubspot",
        params={"idProperty": "email", "properties": ",".join(fillable)},
    )
    _refuse_for(existing, "read contact")
    stored = oauth_http.body_of(existing).get("properties") or {}
    empty = {k: properties[k] for k in fillable if not str(stored.get(k) or "").strip()}
    if not empty:
        return {"status": "exists"}
    updated = await oauth_http.authed(
        ctx.get_token,
        "PATCH",
        contact_url,
        provider="hubspot",
        params={"idProperty": "email"},
        json={"properties": empty},
    )
    _refuse_for(updated, "update contact")
    return {"status": "updated"}


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class HubSpotProvider(BaseProvider):
    id = "hubspot"
    name = "HubSpot"
    kind = "connect"
    category = "crm"
    plan = "free"
    supports_pkce = False
    actions = {
        "create_contact": ActionSpec(
            args=CreateContactArgs,
            handler=_create_contact,
            description="Add or update a contact in HubSpot by email (a lead form).",
            public=True,
            write=True,
        ),
    }

    def availability(self) -> Availability:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        return "available" if client_id and secret else "unconfigured"

    def _credentials(self) -> tuple[str, str]:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        if not client_id or not secret:
            raise ProviderError("hubspot: not configured")
        return client_id, secret

    def authorize_url(
        self,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,  # noqa: ARG002 - no PKCE
    ) -> str:
        client_id, _ = self._credentials()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,  # noqa: ARG002 - no PKCE
    ) -> TokenGrant:
        client_id, secret = self._credentials()
        body = await oauth_http.token_request(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
            },
            provider=self.id,
        )
        access = str(body["access_token"])
        info = await self._token_info(access)
        scopes = [str(s) for s in info.get("scopes") or []]
        if CONTACTS_WRITE not in scopes:
            raise ProviderError("hubspot: the grant is missing contacts write")
        hub_id = str(info.get("hub_id") or "")
        if not hub_id:
            raise ProviderError("hubspot: no hub id in the grant")
        return TokenGrant(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=scopes,
            account_label=str(info.get("hub_domain") or info.get("user") or f"Hub {hub_id}"),
            account_ref=hub_id,
        )

    async def _token_info(self, access_token: str) -> dict[str, Any]:
        # The token sits in this URL path: keep the request out of every span.
        try:
            with oauth_http.untraced():
                async with oauth_http.client() as http:
                    resp = await http.get(f"{TOKEN_INFO_URL}/{quote(access_token, safe='')}")
        except Exception as exc:  # noqa: BLE001 - one safe error for any transport failure
            raise ProviderError(f"hubspot: token info unreachable ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise ProviderError(f"hubspot: token info answered {resp.status_code}")
        return oauth_http.body_of(resp)

    async def refresh(self, refresh_token: str) -> TokenGrant:
        client_id, secret = self._credentials()
        body = await oauth_http.token_request(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": secret,
            },
            provider=self.id,
        )
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
        )
