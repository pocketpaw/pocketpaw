# ee/pocketpaw_ee/integrations/providers/google_sheets.py: the ``google_sheets`` provider.
#
# Kind ``connect`` (Google OAuth, authorization code + PKCE), category ``data``.
# The app is configured by ``POCKETPAW_INTEGRATIONS_GOOGLE_CLIENT_ID`` / ``_SECRET``
# (separate from the local connectors' Google app); without both it is
# ``unconfigured``. Scope is ``drive.file`` only: we can touch the files we create
# and nothing else in the owner's Drive. A grant without ``drive.file`` is refused.
# ``account_ref`` is the Google ``sub``, ``account_label`` the email, read from the
# id_token the token endpoint returned over TLS (aud + iss checked), else userinfo.
#
# ``site_setup`` creates "<site name> — responses" in the owner's Drive with a tab
# named ``sheet_name`` (default "Responses") and stores ``spreadsheet_id`` (hidden)
# and ``spreadsheet_url`` (readonly link). Rerun is idempotent: a reachable sheet
# is kept and only a missing tab is added; an unreachable one is replaced.
#
# ``append_row {fields}`` (public write) keeps row 1 as the header: ``submitted_at``
# (UTC ISO) first, then field keys in first-seen order; new keys add columns, up to
# 60 (beyond -> invalid_args). Values go in with ``valueInputOption=RAW`` so a cell
# is never evaluated as a formula. Control characters are stripped (newlines kept).
# A 401 refreshes once; a 403/404 or a missing tab marks the site setup_required.

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
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
    SettingsField,
    SetupContext,
    TokenGrant,
)

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_GOOGLE"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPES = (DRIVE_FILE, "openid", "email")
_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

DEFAULT_SHEET = "Responses"
TIMESTAMP_COLUMN = "submitted_at"
MAX_FIELDS = 50
MAX_KEY = 80
MAX_VALUE = 2000
MAX_COLUMNS = 60
_SPREADSHEET_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
# C0 controls except newline, plus DEL and C1.
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def _clean(value: str, *, keep_newlines: bool) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL.sub("", value)
    return value if keep_newlines else value.replace("\n", " ")


class AppendRowArgs(ActionArgs):
    fields: dict[str, str] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def _clean_fields(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_FIELDS:
            raise ValueError(f"At most {MAX_FIELDS} fields.")
        cleaned: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            key = _clean(raw_key, keep_newlines=False).strip()
            if not key or len(key) > MAX_KEY:
                raise ValueError(f"Field names must be 1-{MAX_KEY} characters.")
            if len(raw_value) > MAX_VALUE:
                raise ValueError(f"Field values must be at most {MAX_VALUE} characters.")
            if key == TIMESTAMP_COLUMN:
                continue  # the server stamps this column
            cleaned[key] = _clean(raw_value, keep_newlines=True).strip()
        if not cleaned:
            raise ValueError("Send at least one field.")
        return cleaned


def _a1(sheet: str, cells: str) -> str:
    """A quoted A1 range (``'My sheet'!1:1``), URL-encoded for a path segment."""
    return quote("'" + sheet.replace("'", "''") + "'!" + cells, safe="")


def _sheet_name(settings: Any) -> str:
    name = _clean(str((settings or {}).get("sheet_name") or ""), keep_newlines=False).strip()
    return name[:80] or DEFAULT_SHEET


def _sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def _refuse_for(resp: Any, what: str) -> None:
    """Map a failed Sheets answer: gone/forbidden -> setup_required, else 429 / 502."""
    status = resp.status_code
    if status < 400:
        return
    if status in (403, 404):
        raise ActionRefused(
            "setup_required",
            "The spreadsheet for this site can't be reached. The site owner needs to reconnect it.",
            setup_error="sheet_unreachable",
        )
    if status == 400 and "unable to parse range" in resp.text.lower():
        raise ActionRefused(
            "setup_required",
            "The sheet tab for this site is missing. The site owner needs to set it up again.",
            setup_error="sheet_tab_missing",
        )
    if status == 429:
        raise ActionRefused("rate_limited")
    raise ProviderError(f"google_sheets: {what} answered {status}")


async def _append_row(ctx: ActionContext, args: AppendRowArgs) -> dict[str, Any]:
    spreadsheet_id = str(ctx.settings.get("spreadsheet_id") or "")
    if not _SPREADSHEET_ID.match(spreadsheet_id):
        raise ActionRefused(
            "setup_required",
            "This site's spreadsheet isn't set up yet.",
            setup_error="sheet_missing",
        )
    sheet = _sheet_name(ctx.settings)
    base = f"{SHEETS_API}/{spreadsheet_id}/values"

    got = await oauth_http.authed(
        ctx.get_token, "GET", f"{base}/{_a1(sheet, '1:1')}", provider="google_sheets"
    )
    _refuse_for(got, "header read")
    rows = oauth_http.body_of(got).get("values") or []
    header = [str(c) for c in (rows[0] if rows else [])]

    wanted = [TIMESTAMP_COLUMN, *args.fields]
    new = [k for k in wanted if k not in header]
    if new:
        if len(header) + len(new) > MAX_COLUMNS:
            raise ActionRefused(
                "invalid_args", f"This sheet can't take more than {MAX_COLUMNS} columns."
            )
        header = [*header, *new]
        put = await oauth_http.authed(
            ctx.get_token,
            "PUT",
            f"{base}/{_a1(sheet, 'A1')}",
            provider="google_sheets",
            params={"valueInputOption": "RAW"},
            json={"values": [header]},
        )
        _refuse_for(put, "header write")

    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    values = {**args.fields, TIMESTAMP_COLUMN: stamp}
    row = [values.get(col, "") for col in header]
    appended = await oauth_http.authed(
        ctx.get_token,
        "POST",
        f"{base}/{_a1(sheet, 'A1')}:append",
        provider="google_sheets",
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row]},
    )
    _refuse_for(appended, "append")
    return {"appended": True}


def _id_token_claims(id_token: str, client_id: str) -> dict[str, Any]:
    """Claims of an id_token received straight from Google's token endpoint over TLS
    (OIDC core 3.1.3.7 lets TLS stand in for the signature here); aud + iss checked."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    if not isinstance(claims, dict):
        return {}
    if claims.get("aud") != client_id or claims.get("iss") not in _ISSUERS:
        return {}
    return claims


class GoogleSheetsProvider(BaseProvider):
    id = "google_sheets"
    name = "Google Sheets"
    kind = "connect"
    category = "data"
    plan = "free"
    supports_pkce = True
    settings_fields = (
        SettingsField(
            key="spreadsheet_url",
            label="Spreadsheet",
            type="link",
            readonly=True,
            help="Created in your Google Drive when the site is linked.",
        ),
        SettingsField(
            key="sheet_name",
            label="Sheet tab",
            type="text",
            max_length=80,
            help=f"The tab new rows go to. Defaults to {DEFAULT_SHEET}.",
        ),
        SettingsField(
            key="spreadsheet_id",
            label="Spreadsheet id",
            type="text",
            readonly=True,
            hidden=True,
            max_length=200,
        ),
    )
    actions = {
        "append_row": ActionSpec(
            args=AppendRowArgs,
            handler=_append_row,
            description="Add a row to the site's spreadsheet (a form submission).",
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
            raise ProviderError("google_sheets: not configured")
        return client_id, secret

    def authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str:
        client_id, _ = self._credentials()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        if code_challenge:
            params |= {"code_challenge": code_challenge, "code_challenge_method": "S256"}
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenGrant:
        client_id, secret = self._credentials()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        body = await oauth_http.token_request(TOKEN_URL, data, provider=self.id)
        scopes = str(body.get("scope") or "").split()
        if DRIVE_FILE not in scopes:
            raise ProviderError("google_sheets: the grant is missing drive.file")
        claims = _id_token_claims(str(body.get("id_token") or ""), client_id)
        if not claims.get("sub"):
            claims = await self._userinfo(str(body["access_token"]))
        sub = str(claims.get("sub") or "")
        if not sub:
            raise ProviderError("google_sheets: no account id in the grant")
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=scopes,
            account_label=str(claims.get("email") or "Google account"),
            account_ref=sub,
        )

    async def _userinfo(self, access_token: str) -> dict[str, Any]:
        async def _token(rejected: str = "") -> str:  # noqa: ARG001 - no refresh mid-connect
            return access_token

        resp = await oauth_http.authed(_token, "GET", USERINFO_URL, provider=self.id)
        if resp.status_code >= 400:
            raise ProviderError(f"google_sheets: userinfo answered {resp.status_code}")
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
            # Google does not rotate: an empty value keeps the stored one.
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=str(body.get("scope") or "").split(),
        )

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:
        settings = dict(getattr(binding, "settings", None) or {})
        sheet = _sheet_name(settings)
        existing = str(settings.get("spreadsheet_id") or "")
        if _SPREADSHEET_ID.match(existing):
            resp = await oauth_http.authed(
                ctx.get_token,
                "GET",
                f"{SHEETS_API}/{existing}",
                provider=self.id,
                params={"fields": "spreadsheetId,sheets.properties.title"},
            )
            if resp.status_code < 400:
                titles = {
                    str((s.get("properties") or {}).get("title") or "")
                    for s in oauth_http.body_of(resp).get("sheets") or []
                }
                if sheet not in titles:
                    added = await oauth_http.authed(
                        ctx.get_token,
                        "POST",
                        f"{SHEETS_API}/{existing}:batchUpdate",
                        provider=self.id,
                        json={"requests": [{"addSheet": {"properties": {"title": sheet}}}]},
                    )
                    if added.status_code >= 400:
                        raise ProviderError(f"google_sheets: add tab answered {added.status_code}")
                return {"spreadsheet_id": existing, "spreadsheet_url": _sheet_url(existing)}
            if resp.status_code not in (403, 404):
                raise ProviderError(f"google_sheets: sheet check answered {resp.status_code}")
            # Deleted, or no longer ours: make a fresh one below.

        title = f"{await _site_name(ctx.pocket_id)} — responses"
        created = await oauth_http.authed(
            ctx.get_token,
            "POST",
            SHEETS_API,
            provider=self.id,
            json={
                "properties": {"title": title[:200]},
                "sheets": [{"properties": {"title": sheet}}],
            },
        )
        if created.status_code >= 400:
            raise ProviderError(f"google_sheets: create answered {created.status_code}")
        new_id = str(oauth_http.body_of(created).get("spreadsheetId") or "")
        if not _SPREADSHEET_ID.match(new_id):
            raise ProviderError("google_sheets: create returned no spreadsheet id")
        return {"spreadsheet_id": new_id, "spreadsheet_url": _sheet_url(new_id)}


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _site_name(pocket_id: str) -> str:
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.pocket import Pocket

    try:
        pocket = await Pocket.get(PydanticObjectId(pocket_id))
    except Exception:  # noqa: BLE001 - a bad id just means no name
        pocket = None
    name = _clean(str(getattr(pocket, "name", "") or ""), keep_newlines=False).strip()
    return name[:120] or "Paw Site"
