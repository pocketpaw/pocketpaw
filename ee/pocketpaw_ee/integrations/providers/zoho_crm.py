# ee/pocketpaw_ee/integrations/providers/zoho_crm.py: the ``zoho_crm`` CRM provider.
#
# Kind ``connect`` (Zoho Accounts OAuth, server-based app), category ``crm``, plan
# ``site``, no per-site settings. The app is configured by ``POCKETPAW_INTEGRATIONS_
# ZOHO_CLIENT_ID`` / ``_SECRET``; without both it is ``unconfigured``. Multi-DC must
# be on in the Zoho API console with ONE secret shared across data centers.
#
# MULTI-DC. Authorization starts at accounts.zoho.com; Zoho sends the user to their
# region and the callback carries ``accounts-server`` (declared in
# ``callback_params``). That value is browser input and picks where we POST our
# client secret, so it must be an exact ``https://<host>`` from ``ACCOUNTS_HOSTS``
# (Zoho's /oauth/serverinfo list plus China) before any token request; an absent
# one means the US server. The token answer's ``api_domain`` must be an https host
# in ``API_HOSTS``. Both land in ``connection.meta`` and are re-checked on every use:
# refresh goes to the stored accounts server (``refresh_with_meta``), API calls to
# the stored ``api_domain`` (``ActionContext.connection_meta``).
#
# Scopes: leads CREATE/READ/UPDATE, ``ZohoCRM.org.READ`` (the label) and
# ``ZohoSearch.securesearch.READ`` (the Leads search API needs it). ``account_ref``
# = the Zoho org id, ``account_label`` = org name, else its primary email. Access
# tokens live an hour (``expires_in`` read); refresh tokens never expire, but Zoho
# keeps at most 20 per user and silently drops the oldest when a 21st is minted
# (every Connect mints one: ``access_type=offline&prompt=consent``). The vault keeps
# one per connection; a user who re-consents this app (or others) 20+ times kills
# older connections, which then surface as needs_reconnect. Zoho answers token
# errors with a 200 + ``error``; ``invalid_code`` / ``invalid_grant`` raise
# InvalidGrant (oauth_http).
#
# ``create_lead`` (public write): search Leads by email; a lead whose primary
# Email matches exactly (case-insensitive) gets only the fields that are empty on
# it (never an overwrite), else insert. An insert that still hits DUPLICATE_DATA
# (search index lag) fills that lead only if its primary Email matches too.
# Zoho requires Last_Name (default: the email's local part) and many orgs require
# Company (default ``Website visitor``); defaults never fill an existing lead.
# ``message`` goes to Description. A 401 refreshes once; 429 is ``rate_limited``;
# mandatory-field / invalid-data errors are ``invalid_args``.

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

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
from pocketpaw_ee.integrations.providers.newsletter import _EMAIL_RE

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_ZOHO"
DEFAULT_ACCOUNTS = "https://accounts.zoho.com"
AUTHORIZE_URL = f"{DEFAULT_ACCOUNTS}/oauth/v2/auth"
SCOPES = (
    "ZohoCRM.modules.leads.CREATE",
    "ZohoCRM.modules.leads.READ",
    "ZohoCRM.modules.leads.UPDATE",
    "ZohoCRM.org.READ",
    "ZohoSearch.securesearch.READ",
)
ACCOUNTS_HOSTS = frozenset(
    {
        "accounts.zoho.com",
        "accounts.zoho.eu",
        "accounts.zoho.in",
        "accounts.zoho.com.au",
        "accounts.zoho.jp",
        "accounts.zohocloud.ca",
        "accounts.zoho.sa",
        "accounts.zoho.uk",
        "accounts.zoho.ae",
        "accounts.zoho.sg",
        "accounts.zoho.com.cn",
    }
)
API_HOSTS = frozenset(
    f"www.zohoapis.{tld}"
    for tld in ("com", "eu", "in", "com.au", "jp", "ca", "sa", "uk", "ae", "sg", "com.cn")
)
CRM = "/crm/v8"
LEAD_FIELDS = ("Email", "First_Name", "Last_Name", "Phone", "Company", "Description")
DEFAULT_COMPANY = "Website visitor"
_INVALID = {"INVALID_DATA", "MANDATORY_NOT_FOUND", "INVALID_QUERY", "REQUIRED_PARAM_MISSING"}


def _origin(value: str, allowed: frozenset[str]) -> str | None:
    """``https://<host>`` when ``value`` is exactly that for an allowed host."""
    try:
        split = urlsplit(value.strip())
        port = split.port
    except ValueError:
        return None
    host = (split.hostname or "").lower()
    if (
        split.scheme != "https"
        or host not in allowed
        or port is not None
        or split.username
        or split.password
        or split.path not in ("", "/")
        or split.query
        or split.fragment
    ):
        return None
    return f"https://{host}"


def accounts_origin(value: str) -> str | None:
    return _origin(value, ACCOUNTS_HOSTS)


def api_origin(value: str) -> str | None:
    return _origin(value, API_HOSTS)


def _single_line(value: str) -> str:
    return "".join(c for c in value if ord(c) >= 0x20 and ord(c) != 0x7F)


class CreateLeadArgs(ActionArgs):
    email: str = Field(max_length=100)
    first_name: str = Field(default="", max_length=40)
    last_name: str = Field(default="", max_length=80)
    phone: str = Field(default="", max_length=30)
    company: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=2000)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("first_name", "last_name", "phone", "company")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return _single_line(value).strip()

    @field_validator("message")
    @classmethod
    def _no_controls(cls, value: str) -> str:
        return "".join(c for c in value if c in "\n\t" or (ord(c) >= 0x20 and ord(c) != 0x7F))


def _codes(body: dict[str, Any]) -> set[str]:
    codes = {str(body.get("code") or "")}
    for item in body.get("data") or []:
        if isinstance(item, dict):
            codes.add(str(item.get("code") or ""))
    return codes - {""}


def _refuse_for(resp: Any, what: str) -> None:
    status = resp.status_code
    if status < 400:
        return
    codes = _codes(oauth_http.body_of(resp))
    if status == 429 or "TOO_MANY_REQUESTS" in codes:
        raise ActionRefused("rate_limited")
    if status == 400 and codes & _INVALID:
        raise ActionRefused("invalid_args", "Zoho CRM didn't accept those lead details.")
    raise ProviderError(f"zoho_crm: {what} answered {status}")


def _first(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data")
    item = data[0] if isinstance(data, list) and data else {}
    return item if isinstance(item, dict) else {}


def _api(ctx: ActionContext) -> str:
    base = api_origin(str(ctx.connection_meta.get("api_domain") or ""))
    if base is None:
        raise ProviderError("zoho_crm: the connection has no valid api domain")
    return f"{base}{CRM}"


async def _call(ctx: ActionContext, method: str, url: str, **kwargs: Any) -> Any:
    return await oauth_http.authed(
        ctx.get_token, method, url, provider="zoho_crm", auth_scheme="Zoho-oauthtoken", **kwargs
    )


def _same_email(record: dict[str, Any], email: str) -> bool:
    return str(record.get("Email") or "").strip().lower() == email.lower()


async def _find(ctx: ActionContext, api: str, email: str) -> dict[str, Any] | None:
    resp = await _call(
        ctx,
        "GET",
        f"{api}/Leads/search",
        params={"email": email, "fields": ",".join(LEAD_FIELDS)},
    )
    if resp.status_code == 204:
        return None
    _refuse_for(resp, "search leads")
    records = [r for r in oauth_http.body_of(resp).get("data") or [] if isinstance(r, dict)]
    # Only the primary Email counts: a Secondary_Email hit is someone else's lead.
    return next((r for r in records if _same_email(r, email) and r.get("id")), None)


async def _fill_empty(
    ctx: ActionContext, api: str, record: dict[str, Any], supplied: dict[str, str]
) -> dict[str, Any]:
    empty = {k: v for k, v in supplied.items() if not str(record.get(k) or "").strip()}
    if not empty:
        return {"status": "exists"}
    lead_url = f"{api}/Leads/{quote(str(record['id']), safe='')}"
    updated = await _call(ctx, "PUT", lead_url, json={"data": [empty]})
    _refuse_for(updated, "update lead")
    result = _first(oauth_http.body_of(updated))
    if result.get("code") != "SUCCESS":
        if _codes({"data": [result]}) & _INVALID:
            raise ActionRefused("invalid_args", "Zoho CRM didn't accept those lead details.")
        raise ProviderError("zoho_crm: update lead was not accepted")
    return {"status": "updated"}


async def _create_lead(ctx: ActionContext, args: CreateLeadArgs) -> dict[str, Any]:
    api = _api(ctx)
    supplied = {
        "First_Name": args.first_name,
        "Last_Name": args.last_name,
        "Phone": args.phone,
        "Company": args.company,
        "Description": args.message,
    }
    supplied = {k: v for k, v in supplied.items() if v}
    existing = await _find(ctx, api, args.email)
    if existing is not None:
        return await _fill_empty(ctx, api, existing, supplied)

    local = _single_line(args.email.split("@", 1)[0])[:80] or "Website visitor"
    record = {
        "Last_Name": local,
        "Company": DEFAULT_COMPANY,
        **supplied,
        "Email": args.email,
    }
    created = await _call(ctx, "POST", f"{api}/Leads", json={"data": [record]})
    body = oauth_http.body_of(created)
    result = _first(body)
    code = str(result.get("code") or "")
    if code == "SUCCESS" and created.status_code < 400:
        return {"status": "created"}
    if code == "DUPLICATE_DATA":
        # The search index lagged behind a lead created moments ago.
        details = result.get("details") if isinstance(result.get("details"), dict) else {}
        dup = details.get("duplicate_record") if isinstance(details, dict) else None
        dup_id = str((dup or {}).get("id") or details.get("id") or "")
        if not dup_id:
            raise ProviderError("zoho_crm: duplicate lead without an id")
        stored = await _call(
            ctx,
            "GET",
            f"{api}/Leads/{quote(dup_id, safe='')}",
            params={"fields": ",".join(LEAD_FIELDS)},
        )
        _refuse_for(stored, "read lead")
        record = {**_first(oauth_http.body_of(stored)), "id": dup_id}
        if not _same_email(record, args.email):
            # The duplicate is on another unique field: never write to that lead.
            return {"status": "exists"}
        return await _fill_empty(ctx, api, record, supplied)
    _refuse_for(created, "create lead")
    if code in _INVALID:
        raise ActionRefused("invalid_args", "Zoho CRM didn't accept those lead details.")
    raise ProviderError("zoho_crm: create lead was not accepted")


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class ZohoCrmProvider(BaseProvider):
    id = "zoho_crm"
    name = "Zoho CRM"
    kind = "connect"
    category = "crm"
    plan = "site"
    supports_pkce = False
    callback_params = ("accounts-server",)
    refresh_with_meta = True
    actions = {
        "create_lead": ActionSpec(
            args=CreateLeadArgs,
            handler=_create_lead,
            description="Add a lead to Zoho CRM, or fill the gaps on an existing one (by email).",
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
            raise ProviderError("zoho_crm: not configured")
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
            "scope": ",".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,  # noqa: ARG002 - no PKCE
        callback_params: Mapping[str, str] | None = None,
    ) -> TokenGrant:
        client_id, secret = self._credentials()
        raw = str((callback_params or {}).get("accounts-server") or "")
        accounts = accounts_origin(raw) if raw else DEFAULT_ACCOUNTS
        if accounts is None:
            # Never send the client secret to a host we did not allowlist.
            raise ProviderError("zoho_crm: the callback named an unknown accounts server")
        body = await oauth_http.token_request(
            f"{accounts}/oauth/v2/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
            },
            provider=self.id,
        )
        api = api_origin(str(body.get("api_domain") or ""))
        if api is None:
            raise ProviderError("zoho_crm: the token answer named an unknown api domain")
        access = str(body["access_token"])
        org = await self._org(api, access)
        org_id = str(org.get("id") or "")
        if not org_id:
            raise ProviderError("zoho_crm: no org id in the grant")
        label = str(org.get("company_name") or org.get("primary_email") or f"Zoho org {org_id}")
        return TokenGrant(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=list(SCOPES),
            account_label=label,
            account_ref=org_id,
            meta={"accounts_server": accounts, "api_domain": api},
        )

    async def _org(self, api: str, access_token: str) -> dict[str, Any]:
        try:
            async with oauth_http.client() as http:
                resp = await http.get(
                    f"{api}{CRM}/org",
                    headers={"authorization": f"Zoho-oauthtoken {access_token}"},
                )
        except Exception as exc:  # noqa: BLE001 - one safe error for any transport failure
            raise ProviderError(f"zoho_crm: org unreachable ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise ProviderError(f"zoho_crm: org answered {resp.status_code}")
        orgs = oauth_http.body_of(resp).get("org")
        org = orgs[0] if isinstance(orgs, list) and orgs else {}
        return org if isinstance(org, dict) else {}

    async def refresh(
        self, refresh_token: str, meta: Mapping[str, Any] | None = None
    ) -> TokenGrant:
        client_id, secret = self._credentials()
        accounts = accounts_origin(str((meta or {}).get("accounts_server") or ""))
        if accounts is None:
            raise ProviderError("zoho_crm: the connection has no valid accounts server")
        body = await oauth_http.token_request(
            f"{accounts}/oauth/v2/token",
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
