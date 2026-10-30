# ee/pocketpaw_ee/integrations/providers/brevo.py: the ``brevo`` email provider.
#
# Kind ``connect`` (Brevo OAuth 2.0, ``partner`` realm, authorization code, no
# PKCE), category ``email``, plan ``site``. The app is configured by
# ``POCKETPAW_INTEGRATIONS_BREVO_CLIENT_ID`` / ``_SECRET``; without both it is
# ``unconfigured``. Scopes: ``contacts:read contacts:write account:read`` (write
# does not imply read in Brevo). The granted scopes come from the token
# response's ``scope``, else the access token's JWT ``scope`` claim; a grant that
# doesn't show both contacts scopes is refused (fail closed). ``complete`` reads
# ``GET /v3/account``: ``account_ref`` =
# organization_id (else user_id), ``account_label`` = company name (else email).
#
# TOKENS. Access tokens live an hour (``expires_in`` is read); refresh tokens live
# 30 days and may rotate (the vault keeps whichever came last). Thirty days with no
# refresh, or a revoked app, answers ``invalid_grant`` -> InvalidGrant ->
# needs_reconnect. API calls send ``Authorization: Bearer``; a 401 refreshes once.
#
# SETTINGS. ``list_id`` is a remote select of the account's contact lists
# (``field_options``, paged 50 at a time up to 500). Single opt-in only: Brevo's
# double opt-in needs an owner-made confirmation template and redirect URL, so
# there is no ``double_opt_in`` field.
#
# ``subscribe`` (public write) answers ``accepted()`` whatever it did. Create the
# contact with ``listIds=[list_id]``
# (``updateEnabled`` false, FIRSTNAME / LASTNAME only when given). On
# ``duplicate_parameter`` read the contact: blacklisted for email, or unsubscribed
# from this list, gets nothing written; otherwise one
# PUT adds the list (when missing) and only the name attributes that are empty.
# An ``invalid_parameter`` with names retries once without them, on create or
# update (accounts whose attributes have other names, e.g. PRENOM / NOM); an
# update left with nothing to write is ``accepted()``, the same as a new address.
# A missing list (404 /
# ``document_not_found``) is ``setup_required``; other 400s are ``invalid_args``;
# 429 is ``rate_limited``.

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers._email_list import SubscribeArgs, picked_list
from pocketpaw_ee.integrations.providers.base import (
    ActionContext,
    ActionRefused,
    ActionSpec,
    Availability,
    BaseProvider,
    ProviderError,
    SettingOption,
    SettingsField,
    SetupContext,
    TokenGrant,
    accepted,
)

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_BREVO"
OAUTH = "https://oauth.brevo.com/realms/partner/oauth"
AUTHORIZE_URL = f"{OAUTH}/authorize"
TOKEN_URL = f"{OAUTH}/token"
API = "https://api.brevo.com/v3"
CONTACTS_READ = "contacts:read"
CONTACTS_WRITE = "contacts:write"
SCOPES = (CONTACTS_READ, CONTACTS_WRITE, "account:read")
PAGE = 50  # Brevo's max page size for contact lists
MAX_LISTS = 500
_LIST_RE = re.compile(r"^[0-9]{1,12}$")
_REFUSED = "Brevo didn't accept that email address."


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _jwt_scopes(token: str) -> list[str]:
    """The ``scope`` claim of Brevo's JWT access token. Read only to decide what
    the grant allows (it came straight from the token endpoint over TLS); never
    trusted for identity. [] when it isn't a readable JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return []
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return []
    scope = payload.get("scope") if isinstance(payload, dict) else None
    return scope.split() if isinstance(scope, str) else []


def _code(resp: Any) -> str:
    return str(oauth_http.body_of(resp).get("code") or "")


def _refuse_for(resp: Any, what: str) -> None:
    status = resp.status_code
    if status < 400:
        return
    if status == 429:
        raise ActionRefused("rate_limited")
    if status == 404 or _code(resp) == "document_not_found":
        raise ActionRefused(
            "setup_required",
            "This signup form isn't set up yet.",
            setup_error="list_missing",
        )
    if status == 400:
        raise ActionRefused("invalid_args", _REFUSED)
    raise ProviderError(f"brevo: {what} answered {status}")


async def _call(get_token: Any, method: str, url: str, **kwargs: Any) -> Any:
    headers = {"accept": "application/json", **(kwargs.pop("headers", None) or {})}
    return await oauth_http.authed(
        get_token, method, url, provider="brevo", headers=headers, **kwargs
    )


def _ids(value: Any) -> set[int]:
    return {i for i in (_int(v) for v in value or []) if i is not None}


async def _subscribe(ctx: ActionContext, args: SubscribeArgs) -> dict[str, Any]:
    list_id = int(picked_list(ctx.settings, _LIST_RE))
    supplied = {"FIRSTNAME": args.first_name, "LASTNAME": args.last_name}
    supplied = {k: v for k, v in supplied.items() if v}

    body: dict[str, Any] = {"email": args.email, "listIds": [list_id], "updateEnabled": False}
    if supplied:
        body["attributes"] = supplied
    created = await _call(ctx.get_token, "POST", f"{API}/contacts", json=body)
    if created.status_code == 400 and supplied and _code(created) == "invalid_parameter":
        body.pop("attributes")
        supplied = {}
        created = await _call(ctx.get_token, "POST", f"{API}/contacts", json=body)
    if created.status_code < 400:
        return accepted()
    if _code(created) != "duplicate_parameter":
        _refuse_for(created, "create contact")
        raise ProviderError("brevo: create contact failed")  # pragma: no cover

    contact_url = f"{API}/contacts/{quote(args.email, safe='')}"
    found = await _call(ctx.get_token, "GET", contact_url, params={"identifierType": "email_id"})
    if found.status_code == 404:
        raise ProviderError("brevo: duplicate contact could not be read")
    _refuse_for(found, "read contact")
    contact = oauth_http.body_of(found)
    if contact.get("emailBlacklisted") or list_id in _ids(contact.get("listUnsubscribed")):
        return accepted()
    stored = contact.get("attributes")
    stored = stored if isinstance(stored, Mapping) else {}
    fill = {k: v for k, v in supplied.items() if not str(stored.get(k) or "").strip()}
    joins = list_id not in _ids(contact.get("listIds"))
    update: dict[str, Any] = {}
    if joins:
        update["listIds"] = [list_id]
    if fill:
        update["attributes"] = fill
    if not update:
        return accepted()
    params = {"identifierType": "email_id"}
    updated = await _call(ctx.get_token, "PUT", contact_url, params=params, json=update)
    if updated.status_code == 400 and fill and _code(updated) == "invalid_parameter":
        # Same answer as a new address whose names the account can't take.
        update.pop("attributes")
        if not update:
            return accepted()
        updated = await _call(ctx.get_token, "PUT", contact_url, params=params, json=update)
    _refuse_for(updated, "update contact")
    return accepted()


class BrevoProvider(BaseProvider):
    id = "brevo"
    name = "Brevo"
    kind = "connect"
    category = "email"
    plan = "site"
    supports_pkce = False
    settings_fields = (
        SettingsField(
            key="list_id",
            label="Contact list",
            type="select",
            help="The Brevo list new signups join.",
            required=True,
            options_source="remote",
            max_length=12,
        ),
    )
    actions = {
        "subscribe": ActionSpec(
            args=SubscribeArgs,
            handler=_subscribe,
            description="Add an email address to the site's Brevo contact list.",
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
            raise ProviderError("brevo: not configured")
        return client_id, secret

    def authorize_url(
        self,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,  # noqa: ARG002 - no PKCE
    ) -> str:
        client_id, _ = self._credentials()
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"

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
        granted = str(body.get("scope") or "").split() or _jwt_scopes(access)
        if not {CONTACTS_READ, CONTACTS_WRITE} <= set(granted):
            # Fail closed: a grant that doesn't show contacts access is refused.
            raise ProviderError("brevo: the grant doesn't show contacts read and write")
        account = await self._account(access)
        ref = str(account.get("organization_id") or account.get("user_id") or "")
        if not ref:
            raise ProviderError("brevo: no account id in the grant")
        label = str(account.get("companyName") or account.get("email") or f"Brevo {ref}")
        return TokenGrant(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=granted,
            account_label=label,
            account_ref=ref,
        )

    async def _account(self, access_token: str) -> dict[str, Any]:
        try:
            async with oauth_http.client() as http:
                resp = await http.get(
                    f"{API}/account",
                    headers={
                        "authorization": f"Bearer {access_token}",
                        "accept": "application/json",
                    },
                )
        except Exception as exc:  # noqa: BLE001 - one safe error for any transport failure
            raise ProviderError(f"brevo: account unreachable ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise ProviderError(f"brevo: account answered {resp.status_code}")
        return oauth_http.body_of(resp)

    async def refresh(
        self,
        refresh_token: str,
        meta: Mapping[str, Any] | None = None,  # noqa: ARG002
    ) -> TokenGrant:
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

    async def field_options(
        self,
        binding: Any,
        ctx: SetupContext,
        key: str,  # noqa: ARG002
    ) -> list[SettingOption]:
        if key != "list_id":
            return []
        out: list[SettingOption] = []
        for offset in range(0, MAX_LISTS, PAGE):
            resp = await _call(
                ctx.get_token,
                "GET",
                f"{API}/contacts/lists",
                params={"limit": str(PAGE), "offset": str(offset), "sort": "desc"},
            )
            if resp.status_code >= 400:
                raise ProviderError(f"brevo: lists answered {resp.status_code}")
            page = [i for i in oauth_http.body_of(resp).get("lists") or [] if isinstance(i, dict)]
            for item in page:
                value = str(item.get("id") or "")
                if _LIST_RE.fullmatch(value):
                    out.append(SettingOption(value=value, label=str(item.get("name") or "")))
            if len(page) < PAGE:
                break
        return out
