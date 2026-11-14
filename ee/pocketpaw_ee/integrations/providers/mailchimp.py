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
# ``subscribe`` (public write) answers ``accepted()`` whatever it did, so a
# visitor can't learn who is on the audience. It reads the member by
# md5(lowercased email) first:
#   * none: POST (create only) with status pending (double opt-in, Mailchimp sends
#     the confirmation) or subscribed, FNAME / LNAME only when given. A "Member
#     Exists" 400 (added since the read) re-reads and takes the existing path, so
#     a race never overwrites; "Forgotten Email Not Subscribed" / "Member In
#     Compliance State" answer ``accepted()`` with nothing written
#   * unsubscribed / cleaned / archived: nothing written, so a form can never
#     re-subscribe someone who opted out or bounced
#   * subscribed / pending: PATCH only the merge fields that are empty on it
#   * transactional (a contact who never opted in or out): the visitor is opting in
#     now, so PATCH the status like a new member, plus empty merge fields
# Any write carrying names that gets a 400 drops the names and retries once (an
# audience without FNAME / LNAME); with nothing left to write it is ``accepted()``.
# A 404 on the write is the audience gone (``setup_required``); other 400s (a
# fake-looking address) are ``invalid_args``; 429 is ``rate_limited``.

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
    accepted,
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
# The only stored statuses a form may touch; anything else reads as "exists".
_WRITABLE = {"subscribed", "pending", "transactional"}
_REFUSED = "Mailchimp didn't accept that email address."
_MEMBER_EXISTS = "member exists"
# 400s that only say something about the address's history on this audience:
# answered like a success so the result never reveals it.
_QUIET = {"forgotten email not subscribed", "member in compliance state"}


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


def _title(resp: Any) -> str:
    return str(oauth_http.body_of(resp).get("title") or "").strip().lower()


async def _write(
    ctx: ActionContext, method: str, url: str, body: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """Send ``body``; a 400 on a body with names drops them and retries once. The
    answer, and the body that produced it (empty: nothing was left to write)."""
    resp = await _call(ctx.get_token, method, url, json=body)
    if resp.status_code != 400 or "merge_fields" not in body or _title(resp) in _QUIET:
        return resp, body
    if _title(resp) == _MEMBER_EXISTS:
        return resp, body
    # The audience may not have FNAME / LNAME: the address matters most.
    body = {k: v for k, v in body.items() if k != "merge_fields"}
    if not set(body) - {"email_address"}:
        return None, {}
    return await _call(ctx.get_token, method, url, json=body), body


async def _fill_existing(
    ctx: ActionContext, member_url: str, member: dict[str, Any], supplied: dict[str, str]
) -> dict[str, Any]:
    status = str(member.get("status") or "")
    if status not in _WRITABLE:  # opted out, bounced, archived, or unknown
        return accepted()
    merge = member.get("merge_fields")
    patch: dict[str, Any] = {}
    fill = _empty_merge(merge if isinstance(merge, Mapping) else {}, supplied)
    if fill:
        patch["merge_fields"] = fill
    if status == "transactional":
        patch["status"] = _target(ctx)
    if not patch:
        return accepted()
    updated, sent = await _write(ctx, "PATCH", member_url, patch)
    if sent and _title(updated) not in _QUIET:
        _refuse_for(updated, "update member")
    return accepted()


def _target(ctx: ActionContext) -> str:
    return "pending" if _double_opt_in(ctx.settings) else "subscribed"


async def _read(ctx: ActionContext, member_url: str) -> Any:
    return await _call(ctx.get_token, "GET", member_url, params={"fields": "status,merge_fields"})


async def _subscribe(ctx: ActionContext, args: SubscribeArgs) -> dict[str, Any]:
    api = _api(ctx.connection_meta)
    list_id = picked_list(ctx.settings, _LIST_RE)
    members_url = f"{api}/lists/{quote(list_id, safe='')}/members"
    member_url = f"{members_url}/{_member_hash(args.email)}"
    supplied = {"FNAME": args.first_name, "LNAME": args.last_name}
    supplied = {k: v for k, v in supplied.items() if v}

    found = await _read(ctx, member_url)
    if found.status_code != 404:
        _refuse_for(found, "read member")
        return await _fill_existing(ctx, member_url, oauth_http.body_of(found), supplied)

    # POST only creates: a member added since the read is a "Member Exists" 400,
    # never an overwrite, and takes the fill-empty path instead.
    body: dict[str, Any] = {"email_address": args.email, "status": _target(ctx)}
    if supplied:
        body["merge_fields"] = supplied
    created, _ = await _write(ctx, "POST", members_url, body)
    title = _title(created)
    if created.status_code == 400 and title == _MEMBER_EXISTS:
        again = await _read(ctx, member_url)
        if again.status_code == 404:
            raise ProviderError("mailchimp: an existing member could not be read")
        _refuse_for(again, "read member")
        return await _fill_existing(ctx, member_url, oauth_http.body_of(again), supplied)
    if created.status_code == 400 and title in _QUIET:
        # Forgotten / compliance-held addresses answer like any other: no leak.
        return accepted()
    _refuse_for(created, "add member")
    return accepted()


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
