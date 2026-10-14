# ee/pocketpaw_ee/integrations/providers/mailchimp.py: the ``mailchimp`` email provider.
#
# Kind ``connect`` (Mailchimp OAuth 2, authorization code, no PKCE), category
# ``email``, plan ``site``. The app is configured by ``POCKETPAW_INTEGRATIONS_
# MAILCHIMP_CLIENT_ID`` / ``_SECRET``; without both it is ``unconfigured``.
#
# TOKENS. Mailchimp access tokens do not expire and there is no refresh token
# (``expires_in`` is 0, stored as no expiry). ``refresh`` always raises
# InvalidGrant, so an API 401 (the owner revoked the app) flips the connection to
# needs_reconnect through the vault's one refresh path.
#
# DATA CENTER. After the code exchange ``complete`` calls the OAuth metadata
# endpoint (header ``OAuth <token>``) for ``dc`` and ``api_endpoint``. ``dc`` must
# match ``^[a-z]{2,4}\d{1,3}$`` and ``api_endpoint`` must be exactly
# ``https://<dc>.api.mailchimp.com``; anything else refuses the connect, so a
# token is never sent to a host we did not derive. Both land in ``connection.meta``
# and are re-checked on every use. ``account_ref`` = metadata ``user_id`` (else
# the login id), ``account_label`` = account name (else the login email).
#
# SETTINGS. ``list_id`` is a remote select (the account's audiences, from
# ``field_options``); ``double_opt_in`` yes/no, unset reads as yes.
#
# ``subscribe`` (public write) reads the member by md5(lowercased email) first:
#   * none: PUT (upsert) with ``status_if_new`` = pending (double opt-in, Mailchimp
#     sends the confirmation) or subscribed, FNAME / LNAME only when given
#   * unsubscribed / cleaned / archived: ``{"status": "exists"}``, nothing written,
#     so a form can never re-subscribe someone who opted out or bounced
#   * subscribed / pending: PATCH only the merge fields that are empty on it
#   * transactional (a contact who never opted in or out): the visitor is opting in
#     now, so PATCH the status like a new member, plus empty merge fields
# A 404 on the write is the audience gone (``setup_required``); 400 (forgotten
# email, compliance state, a fake-looking address) is ``invalid_args`` with a
# generic message; 429 is ``rate_limited``. Merge-field rejection on a new member
# retries once without names, so an audience without FNAME/LNAME still subscribes.

from __future__ import annotations

import hashlib
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
    InvalidGrant,
    ProviderError,
    SettingOption,
    SettingsField,
    SetupContext,
    TokenGrant,
)

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_MAILCHIMP"
LOGIN = "https://login.mailchimp.com"
AUTHORIZE_URL = f"{LOGIN}/oauth2/authorize"
TOKEN_URL = f"{LOGIN}/oauth2/token"
METADATA_URL = f"{LOGIN}/oauth2/metadata"
MAX_AUDIENCES = 500
_DC_RE = re.compile(r"^[a-z]{2,4}\d{1,3}$")
_LIST_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
# Never written to from a public form: they opted out, bounced, or were removed.
_OPTED_OUT = {"unsubscribed", "cleaned", "archived"}
# The only stored statuses a form may touch; anything else reads as "exists".
_WRITABLE = {"subscribed", "pending", "transactional"}
_REFUSED = "Mailchimp didn't accept that email address."


def api_base(meta: Mapping[str, Any]) -> str | None:
    """``https://<dc>.api.mailchimp.com/3.0`` when the stored dc and endpoint agree."""
    dc = str(meta.get("dc") or "")
    endpoint = str(meta.get("api_endpoint") or "")
    if not _DC_RE.fullmatch(dc):
        return None
    origin = f"https://{dc}.api.mailchimp.com"
    if endpoint not in (origin, f"{origin}/"):
        return None
    return f"{origin}/3.0"


def _api(meta: Mapping[str, Any]) -> str:
    base = api_base(meta)
    if base is None:
        raise ProviderError("mailchimp: the connection has no valid data center")
    return base


def _double_opt_in(settings: Mapping[str, Any]) -> bool:
    return str(settings.get("double_opt_in") or "yes") != "no"


def _member_hash(email: str) -> str:
    return hashlib.md5(email.lower().encode(), usedforsecurity=False).hexdigest()


def _refuse_for(resp: Any, what: str) -> None:
    status = resp.status_code
    if status < 400:
        return
    if status == 429:
        raise ActionRefused("rate_limited")
    if status == 404:
        raise ActionRefused(
            "setup_required",
            "This signup form isn't set up yet.",
            setup_error="audience_missing",
        )
    if status == 400:
        raise ActionRefused("invalid_args", _REFUSED)
    raise ProviderError(f"mailchimp: {what} answered {status}")


async def _call(get_token: Any, method: str, url: str, **kwargs: Any) -> Any:
    return await oauth_http.authed(get_token, method, url, provider="mailchimp", **kwargs)


def _empty_merge(stored: Mapping[str, Any], supplied: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in supplied.items() if not str(stored.get(k) or "").strip()}


async def _subscribe(ctx: ActionContext, args: SubscribeArgs) -> dict[str, Any]:
    api = _api(ctx.connection_meta)
    list_id = picked_list(ctx.settings, _LIST_RE)
    target = "pending" if _double_opt_in(ctx.settings) else "subscribed"
    member_url = f"{api}/lists/{quote(list_id, safe='')}/members/{_member_hash(args.email)}"
    supplied = {"FNAME": args.first_name, "LNAME": args.last_name}
    supplied = {k: v for k, v in supplied.items() if v}

    found = await _call(ctx.get_token, "GET", member_url, params={"fields": "status,merge_fields"})
    if found.status_code == 404:
        body: dict[str, Any] = {"email_address": args.email, "status_if_new": target}
        if supplied:
            body["merge_fields"] = supplied
        created = await _call(ctx.get_token, "PUT", member_url, json=body)
        if created.status_code == 400 and supplied:
            # The audience may not have FNAME / LNAME: the address matters most.
            body.pop("merge_fields")
            created = await _call(ctx.get_token, "PUT", member_url, json=body)
        _refuse_for(created, "add member")
        stored = str(oauth_http.body_of(created).get("status") or "")
        if stored in _OPTED_OUT:
            return {"status": "exists"}
        return {"status": target}
    _refuse_for(found, "read member")

    member = oauth_http.body_of(found)
    status = str(member.get("status") or "")
    if status not in _WRITABLE:  # opted out, bounced, archived, or unknown
        return {"status": "exists"}
    merge = member.get("merge_fields")
    patch: dict[str, Any] = {}
    fill = _empty_merge(merge if isinstance(merge, Mapping) else {}, supplied)
    if fill:
        patch["merge_fields"] = fill
    if status == "transactional":
        patch["status"] = target
    if not patch:
        return {"status": "exists"}
    updated = await _call(ctx.get_token, "PATCH", member_url, json=patch)
    if updated.status_code == 400 and "status" in patch and fill:
        patch.pop("merge_fields")
        updated = await _call(ctx.get_token, "PATCH", member_url, json=patch)
    _refuse_for(updated, "update member")
    return {"status": target if "status" in patch else "updated"}


class MailchimpProvider(BaseProvider):
    id = "mailchimp"
    name = "Mailchimp"
    kind = "connect"
    category = "email"
    plan = "site"
    supports_pkce = False
    settings_fields = (
        SettingsField(
            key="list_id",
            label="Audience",
            type="select",
            help="The Mailchimp audience new signups join.",
            required=True,
            options_source="remote",
            max_length=32,
        ),
        SettingsField(
            key="double_opt_in",
            label="Double opt-in",
            type="select",
            help="Send a confirmation email before adding someone (recommended).",
            options=(SettingOption("yes", "Yes"), SettingOption("no", "No")),
            max_length=3,
        ),
    )
    actions = {
        "subscribe": ActionSpec(
            args=SubscribeArgs,
            handler=_subscribe,
            description="Add an email address to the site's Mailchimp audience.",
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
            raise ProviderError("mailchimp: not configured")
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
        meta = await self._metadata(access)
        dc = str(meta.get("dc") or "")
        endpoint = str(meta.get("api_endpoint") or "")
        if api_base({"dc": dc, "api_endpoint": endpoint}) is None:
            raise ProviderError("mailchimp: the metadata named an unknown data center")
        login = meta.get("login") if isinstance(meta.get("login"), dict) else {}
        ref = str(meta.get("user_id") or login.get("login_id") or "")
        if not ref:
            raise ProviderError("mailchimp: no account id in the metadata")
        label = str(meta.get("accountname") or login.get("login_email") or f"Mailchimp {ref}")
        return TokenGrant(
            access_token=access,
            expires_in=None,  # Mailchimp tokens don't expire (``expires_in`` is 0)
            account_label=label,
            account_ref=ref,
            meta={"dc": dc, "api_endpoint": endpoint.rstrip("/")},
        )

    async def _metadata(self, access_token: str) -> dict[str, Any]:
        try:
            async with oauth_http.client() as http:
                resp = await http.get(
                    METADATA_URL,
                    headers={
                        "authorization": f"OAuth {access_token}",
                        "accept": "application/json",
                    },
                )
        except Exception as exc:  # noqa: BLE001 - one safe error for any transport failure
            raise ProviderError(f"mailchimp: metadata unreachable ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise ProviderError(f"mailchimp: metadata answered {resp.status_code}")
        return oauth_http.body_of(resp)

    async def refresh(
        self,
        refresh_token: str,  # noqa: ARG002
        meta: Mapping[str, Any] | None = None,  # noqa: ARG002
    ) -> TokenGrant:
        # No refresh grant exists: a refused token means the owner revoked the app.
        raise InvalidGrant("mailchimp: tokens can't be refreshed")

    async def field_options(
        self,
        binding: Any,
        ctx: SetupContext,
        key: str,  # noqa: ARG002
    ) -> list[SettingOption]:
        if key != "list_id":
            return []
        conn_meta = getattr(ctx.connection, "meta", None) or {}
        resp = await _call(
            ctx.get_token,
            "GET",
            f"{_api(conn_meta)}/lists",
            params={"count": str(MAX_AUDIENCES), "fields": "lists.id,lists.name"},
        )
        if resp.status_code >= 400:
            raise ProviderError(f"mailchimp: lists answered {resp.status_code}")
        out: list[SettingOption] = []
        for item in oauth_http.body_of(resp).get("lists") or []:
            if isinstance(item, dict) and _LIST_RE.fullmatch(str(item.get("id") or "")):
                out.append(SettingOption(value=str(item["id"]), label=str(item.get("name") or "")))
        return out
