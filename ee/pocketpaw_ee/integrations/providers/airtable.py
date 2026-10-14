# ee/pocketpaw_ee/integrations/providers/airtable.py: the ``airtable`` provider.
#
# Kind ``connect`` (Airtable OAuth, authorization code + PKCE S256, which Airtable
# requires), category ``data``, plan ``site``. The app is configured by
# ``POCKETPAW_INTEGRATIONS_AIRTABLE_CLIENT_ID`` / ``_SECRET``; without both it is
# ``unconfigured``. The token endpoint takes the client secret as HTTP Basic auth
# (standard base64, see ``_basic``).
# Scopes: ``data.records:write`` + ``schema.bases:read`` (both required on the
# grant) and ``user.email:read`` for the label. ``account_ref`` is the Airtable
# user id from ``/meta/whoami``, ``account_label`` its email.
# Refresh tokens are single-use: every refresh returns a new pair and the vault
# stores the newest; a dead one (``invalid_grant``) raises ``InvalidGrant``.
#
# Settings: ``base_id`` and ``table_name`` are remote selects. Bases are the ones
# the grant can edit; tables are those of the chosen base (none without one), and
# ``table_name`` depends on ``base_id``. A table's value is its id (``tbl…``), so a
# rename in Airtable doesn't break the site. Optional ``writable_fields`` (text,
# comma-separated field names, case-insensitive) narrows what visitors may write;
# empty means every text-like field. ``site_setup`` checks the names against the
# table, so a typo leaves the site ``setup_required``; a name that later vanishes
# from the table refuses writes the same way (never silently widened). Ids are
# fullmatched against Airtable's shapes and quoted in URL paths.
#
# ``create_record {fields}`` (public write) maps keys (normalised like
# google_sheets) case-insensitively onto the table's EXISTING text-like fields
# (single line, long text, rich text, email, url, phone). Nothing is ever created
# in the owner's base, so strangers can't grow its schema: unknown keys and
# non-text fields are dropped, an email field only takes something that looks
# like an address, and the record is written by field id with ``typecast`` off.
# A request where no key matches is ``invalid_args`` with a fixed message that
# never names the table's fields. The table schema is cached
# for 60s per binding and dropped on a 403 / 404 / 422.
# Errors: a 401 refreshes once; 403 / 404 (base or table gone or unreachable) ->
# setup_required (marks the site); 422 / 413 -> invalid_args; 429 -> rate_limited;
# anything else -> provider_error. A successful write clears a stale setup_required.

from __future__ import annotations

import base64
import re
import time
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
    SettingOption,
    SettingsField,
    SetupContext,
    TokenGrant,
    accepted,
)
from pocketpaw_ee.integrations.providers.google_sheets import (
    MAX_FIELDS,
    MAX_VALUE,
    clean_text,
    normalise_key,
)

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_AIRTABLE"
AUTHORIZE_URL = "https://airtable.com/oauth2/v1/authorize"
TOKEN_URL = "https://airtable.com/oauth2/v1/token"
API = "https://api.airtable.com/v0"
SCOPES = ("data.records:write", "schema.bases:read", "user.email:read")
REQUIRED_SCOPES = frozenset({"data.records:write", "schema.bases:read"})

# Bases a grant can add records to (Airtable's base permission levels).
_WRITABLE_BASES = frozenset({"create", "edit"})
# Field types that take a plain string with typecast off.
_SINGLE_LINE = frozenset({"singleLineText", "email", "url", "phoneNumber"})
_MULTI_LINE = frozenset({"multilineText", "richText"})
# Always ``fullmatch`` (``$`` alone would let a trailing newline through).
_BASE_ID = re.compile(r"app[A-Za-z0-9]{14}")
_TABLE_ID = re.compile(r"tbl[A-Za-z0-9]{14}")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_SPACES = re.compile(r"\s+")
MAX_BASE_PAGES = 5
MAX_WRITABLE = 60
NO_MATCH = "These fields can't be saved to this form's table."
SCHEMA_TTL = 60.0
_SCHEMA_CACHE_MAX = 512

_schemas: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    _schemas.clear()


def _fold(name: str) -> str:
    return _SPACES.sub(" ", name).strip().casefold()


def _seg(value: str) -> str:
    """One URL path segment."""
    return quote(value, safe="")


def writable_names(settings: Any) -> set[str] | None:
    """The owner's ``writable_fields`` folded, or None when unset (all text fields)."""
    raw = str((settings or {}).get("writable_fields") or "")
    names = {_fold(n) for n in raw.split(",")} - {""}
    return names or None


def _error_type(resp: Any) -> str:
    """Airtable's ``error`` is a string or ``{type, message}``."""
    err = oauth_http.body_of(resp).get("error")
    if isinstance(err, dict):
        return str(err.get("type") or "")
    return str(err or "")


def _refuse_for(resp: Any, what: str) -> None:
    code = resp.status_code
    if code < 400:
        return
    if code == 429:
        raise ActionRefused("rate_limited")
    if code in (403, 404):
        raise ActionRefused(
            "setup_required",
            "The Airtable table for this site can't be reached. "
            "The site owner needs to pick it again.",
            setup_error="table_unreachable",
        )
    if code in (413, 422):
        raise ActionRefused("invalid_args", "Airtable didn't accept this record.")
    raise ProviderError(f"airtable: {what} answered {code} {_error_type(resp)}".strip())


def _setup_missing() -> ActionRefused:
    return ActionRefused(
        "setup_required",
        "This site's Airtable table isn't picked yet.",
        setup_error="table_missing",
    )


async def _list_bases(get_token: Any) -> list[SettingOption]:
    options: list[SettingOption] = []
    offset = ""
    for _ in range(MAX_BASE_PAGES):
        resp = await oauth_http.authed(
            get_token,
            "GET",
            f"{API}/meta/bases",
            provider="airtable",
            params={"offset": offset} if offset else None,
        )
        if resp.status_code >= 400:
            raise ProviderError(f"airtable: list bases answered {resp.status_code}")
        body = oauth_http.body_of(resp)
        for base in body.get("bases") or []:
            if not isinstance(base, dict) or base.get("permissionLevel") not in _WRITABLE_BASES:
                continue
            base_id, name = str(base.get("id") or ""), str(base.get("name") or "")
            if _BASE_ID.fullmatch(base_id):
                options.append(SettingOption(value=base_id, label=name or base_id))
        offset = str(body.get("offset") or "")
        if not offset:
            break
    return options


async def _tables(get_token: Any, base_id: str) -> Any:
    return await oauth_http.authed(
        get_token, "GET", f"{API}/meta/bases/{_seg(base_id)}/tables", provider="airtable"
    )


async def _table_schema(
    ctx: ActionContext | SetupContext, base_id: str, table: str
) -> dict[str, Any]:
    """The bound table (``{id, fields}``), cached for ``SCHEMA_TTL`` seconds."""
    key = (ctx.workspace_id, ctx.pocket_id, base_id, table)
    now = time.monotonic()
    hit = _schemas.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    resp = await _tables(ctx.get_token, base_id)
    _refuse_for(resp, "schema read")
    tables = [t for t in oauth_http.body_of(resp).get("tables") or [] if isinstance(t, dict)]
    found = next((t for t in tables if t.get("id") == table), None)
    if found is None or not _TABLE_ID.fullmatch(str(found.get("id") or "")):
        raise ActionRefused(
            "setup_required",
            "The Airtable table for this site is gone. The site owner needs to pick it again.",
            setup_error="table_missing",
        )
    schema = {"id": str(found["id"]), "fields": list(found.get("fields") or [])}
    if len(_schemas) >= _SCHEMA_CACHE_MAX:
        _schemas.pop(next(iter(_schemas)))
    _schemas[key] = (now + SCHEMA_TTL, schema)
    return schema


def _text_targets(schema: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """``{folded name: (field id, type)}`` of the table's text-like fields."""
    targets: dict[str, tuple[str, str]] = {}
    for item in schema["fields"]:
        if not isinstance(item, dict):
            continue
        ftype, fid, name = (
            str(item.get("type")),
            str(item.get("id") or ""),
            str(item.get("name") or ""),
        )
        if fid and name and ftype in _SINGLE_LINE | _MULTI_LINE:
            targets.setdefault(_fold(name), (fid, ftype))
    return targets


def _map_fields(
    schema: dict[str, Any], fields: dict[str, str], allowed: set[str] | None = None
) -> dict[str, str]:
    """``{field id: value}`` for the keys that match an existing text-like field
    (and, when the owner set ``writable_fields``, one of those)."""
    targets = _text_targets(schema)
    if allowed is not None:
        targets = {k: v for k, v in targets.items() if k in allowed}
    out: dict[str, str] = {}
    for key, value in fields.items():
        target = targets.get(_fold(key))
        if target is None or not value:
            continue
        fid, ftype = target
        if ftype in _SINGLE_LINE:
            value = value.replace("\n", " ").strip()
        if ftype == "email" and not _EMAIL.fullmatch(value):
            continue
        if value:
            out.setdefault(fid, value)
    return out


class CreateRecordArgs(ActionArgs):
    fields: dict[str, str] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def _clean_fields(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_FIELDS:
            raise ValueError(f"At most {MAX_FIELDS} fields.")
        cleaned: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            key = normalise_key(raw_key)
            if len(raw_value) > MAX_VALUE:
                raise ValueError(f"Field values must be at most {MAX_VALUE} characters.")
            cleaned[key] = clean_text(raw_value, keep_newlines=True).strip()
        return cleaned


async def _create_record(ctx: ActionContext, args: CreateRecordArgs) -> dict[str, Any]:
    from pocketpaw_ee.integrations import bindings

    base_id = str(ctx.settings.get("base_id") or "")
    table = str(ctx.settings.get("table_name") or "").strip()
    if not _BASE_ID.fullmatch(base_id) or not table:
        raise _setup_missing()
    key = (ctx.workspace_id, ctx.pocket_id, base_id, table)
    try:
        schema = await _table_schema(ctx, base_id, table)
        allowed = writable_names(ctx.settings)
        if allowed is not None and not allowed <= set(_text_targets(schema)):
            raise ActionRefused(
                "setup_required",
                "This site's Airtable form fields need checking by the site owner.",
                setup_error="writable_fields_unknown",
            )
        mapped = _map_fields(schema, args.fields, allowed)
        if not mapped:
            raise ActionRefused("invalid_args", NO_MATCH)
        created = await oauth_http.authed(
            ctx.get_token,
            "POST",
            f"{API}/{_seg(base_id)}/{_seg(schema['id'])}",
            provider="airtable",
            json={"fields": mapped, "typecast": False},
        )
        if created.status_code in (403, 404, 413, 422):
            _schemas.pop(key, None)
        _refuse_for(created, "create record")
    except ActionRefused as exc:
        if exc.code == "setup_required":
            _schemas.pop(key, None)
        raise
    await bindings.clear_setup_error(ctx.workspace_id, ctx.pocket_id, ctx.provider)
    return accepted()


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class AirtableProvider(BaseProvider):
    id = "airtable"
    name = "Airtable"
    kind = "connect"
    category = "data"
    plan = "site"
    supports_pkce = True
    settings_fields = (
        SettingsField(
            key="base_id",
            label="Base",
            type="select",
            required=True,
            options_source="remote",
            max_length=64,
            help="The Airtable base new records go to.",
        ),
        SettingsField(
            key="table_name",
            label="Table",
            type="select",
            required=True,
            options_source="remote",
            depends_on=("base_id",),
            max_length=64,
            help="Form fields fill the table's text fields with the same name.",
        ),
        SettingsField(
            key="writable_fields",
            label="Fields visitors can fill",
            type="text",
            max_length=1000,
            help="Comma-separated field names. Leave empty to allow every text field.",
        ),
    )
    actions = {
        "create_record": ActionSpec(
            args=CreateRecordArgs,
            handler=_create_record,
            description="Add a record to the site's Airtable table (a form submission).",
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
            raise ProviderError("airtable: not configured")
        return client_id, secret

    def _basic(self) -> dict[str, str]:
        client_id, secret = self._credentials()
        # Standard base64. The reference page words it "base64 url-encoding"
        # (https://airtable.com/developers/web/api/oauth-reference), but Airtable's
        # own example it links (https://github.com/Airtable/oauth-example, index.js:
        # ``Buffer.from(`${clientId}:${clientSecret}`).toString('base64')``) and
        # RFC 7617 use standard base64, which is what the server is known to accept.
        raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
        return {"authorization": f"Basic {raw}"}

    def authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str:
        client_id, _ = self._credentials()
        if not code_challenge:
            raise ProviderError("airtable: PKCE is required")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenGrant:
        if not code_verifier:
            raise ProviderError("airtable: PKCE verifier missing")
        body = await oauth_http.token_request(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            provider=self.id,
            headers=self._basic(),
        )
        scopes = str(body.get("scope") or "").split()
        if not REQUIRED_SCOPES <= set(scopes):
            raise ProviderError("airtable: the grant is missing a required scope")
        access = str(body["access_token"])
        who = await self._whoami(access)
        user_id = str(who.get("id") or "")
        if not user_id:
            raise ProviderError("airtable: no user id in whoami")
        return TokenGrant(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=scopes,
            account_label=str(who.get("email") or "Airtable account"),
            account_ref=user_id,
        )

    async def _whoami(self, access_token: str) -> dict[str, Any]:
        async def _token(rejected: str = "") -> str:  # noqa: ARG001 - no refresh mid-connect
            return access_token

        resp = await oauth_http.authed(_token, "GET", f"{API}/meta/whoami", provider=self.id)
        if resp.status_code >= 400:
            raise ProviderError(f"airtable: whoami answered {resp.status_code}")
        return oauth_http.body_of(resp)

    async def refresh(self, refresh_token: str) -> TokenGrant:
        body = await oauth_http.token_request(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            provider=self.id,
            headers=self._basic(),
        )
        return TokenGrant(
            access_token=str(body["access_token"]),
            # Single-use: the old refresh token is dead now; the vault keeps this one.
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=str(body.get("scope") or "").split(),
        )

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:
        """Check ``writable_fields`` against the table; a bad name raises, so the
        site reads ``setup_required`` until the owner fixes it."""
        settings = dict(getattr(binding, "settings", None) or {})
        allowed = writable_names(settings)
        base_id = str(settings.get("base_id") or "")
        table = str(settings.get("table_name") or "").strip()
        if allowed is None or not _BASE_ID.fullmatch(base_id) or not table:
            return {}
        if len(allowed) > MAX_WRITABLE:
            raise ProviderError(f"airtable: more than {MAX_WRITABLE} writable fields")
        _schemas.pop((ctx.workspace_id, ctx.pocket_id, base_id, table), None)
        schema = await _table_schema(ctx, base_id, table)
        if not allowed <= set(_text_targets(schema)):
            raise ProviderError("airtable: writable_fields names a field the table lacks")
        return {}

    async def field_options(self, binding: Any, ctx: SetupContext, key: str) -> list[SettingOption]:
        if key == "base_id":
            return await _list_bases(ctx.get_token)
        if key != "table_name":
            return []
        base_id = str((getattr(binding, "settings", None) or {}).get("base_id") or "")
        if not _BASE_ID.fullmatch(base_id):
            return []
        resp = await _tables(ctx.get_token, base_id)
        if resp.status_code >= 400:
            raise ProviderError(f"airtable: list tables answered {resp.status_code}")
        return [
            SettingOption(value=str(t["id"]), label=str(t.get("name") or t["id"]))
            for t in oauth_http.body_of(resp).get("tables") or []
            if isinstance(t, dict) and _TABLE_ID.fullmatch(str(t.get("id") or ""))
        ]
