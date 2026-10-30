# tests/ee/sites/test_integrations_airtable.py: the airtable provider.
#
# Runs end to end through the real routes (connect -> /start -> Airtable ->
# callback -> site link -> settings PUT -> public gateway) against an in-memory
# fake of Airtable's OAuth server and Web API, swapped in as
# ``oauth_http._transport``. No network. Covers: PKCE S256 + Basic client auth on
# the token endpoint, whoami label, single-use refresh rotation (the vault keeps the
# newest), invalid_grant -> needs_reconnect, the base / table remote selects (bases
# the grant can edit, paginated; no tables without a base; a table re-checked and
# dropped when the base changes; cache keyed by base), create_record mapping onto
# existing text fields only (case-insensitive, unknown and non-text keys dropped,
# typecast off, by field id), the owner's ``writable_fields`` allowlist, the
# arg caps, fullmatched ids, and the error mapping.
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    _client,
    _code,
    _env,
    _gw,
    _message,
    _pocket,
    _site,
)

CLIENT_ID = "air-client"
SECRET = "air-secret"
BASE_A = "appAAAAAAAAAAAAAA"
BASE_B = "appBBBBBBBBBBBBBB"
BASE_RO = "appRRRRRRRRRRRRRR"
BASE_P2 = "appPPPPPPPPPPPPPP"
TBL_LEADS = "tblLLLLLLLLLLLLLL"
TBL_OTHER = "tblOOOOOOOOOOOOOO"
TBL_B = "tblBBBBBBBBBBBBBB"
USER_ID = "usrL2PNC5o3H4lBEi"


def _s256(verifier: str) -> str:
    from pocketpaw_ee.integrations.providers.oauth_http import s256

    return s256(verifier)


def _field(fid: str, name: str, ftype: str) -> dict:
    return {"id": fid, "name": name, "type": ftype}


LEADS_FIELDS = [
    _field("fldName0000000001", "Name", "singleLineText"),
    _field("fldEmail000000001", "Email", "email"),
    _field("fldMessage0000001", "Message", "multilineText"),
    _field("fldNotes000000001", "Notes", "richText"),
    _field("fldScore000000001", "Score", "number"),
    _field("fldCreated0000001", "Created", "createdTime"),
]


class FakeAirtable:
    def __init__(self) -> None:
        self.secret = SECRET
        self.valid: set[str] = set()
        self.live_refresh: set[str] = set()
        self.issued = 0
        self.challenges: dict[str, str] = {}
        self.token_calls: list[tuple[str, dict]] = []
        self.api_calls: list[tuple[str, str, dict, dict]] = []
        self.records: list[dict] = []
        self.create_status: tuple[int, dict] | None = None
        self.schema_status: tuple[int, dict] | None = None
        self.scope = "data.records:write schema.bases:read user.email:read"
        self.bases = [
            {"id": BASE_A, "name": "Leads base", "permissionLevel": "create"},
            {"id": BASE_RO, "name": "Read only", "permissionLevel": "read"},
            {"id": BASE_B, "name": "Second", "permissionLevel": "edit"},
            {"id": "bad-id", "name": "Broken", "permissionLevel": "create"},
            {"id": BASE_P2, "name": "Page two", "permissionLevel": "create"},
        ]
        self.tables = {
            BASE_A: [
                {"id": TBL_LEADS, "name": "Leads", "fields": LEADS_FIELDS},
                {"id": TBL_OTHER, "name": "Other", "fields": []},
            ],
            BASE_B: [{"id": TBL_B, "name": "B table", "fields": LEADS_FIELDS[:1]}],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "airtable.com":
            return self._token(request)
        assert request.url.host == "api.airtable.com", request.url
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or token not in self.valid:
            return httpx.Response(401, json={"error": {"type": "AUTHENTICATION_REQUIRED"}})
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.api_calls.append((request.method, path, dict(request.url.params), body))
        if path == "/v0/meta/whoami":
            return httpx.Response(200, json={"id": USER_ID, "email": "ada@example.com"})
        if path == "/v0/meta/bases":
            # Two pages: the last base comes behind an offset.
            if request.url.params.get("offset") == "itr1":
                return httpx.Response(200, json={"bases": self.bases[4:]})
            return httpx.Response(200, json={"bases": self.bases[:4], "offset": "itr1"})
        if path.startswith("/v0/meta/bases/") and path.endswith("/tables"):
            if self.schema_status is not None:
                return httpx.Response(self.schema_status[0], json=self.schema_status[1])
            base = path.split("/")[4]
            if base not in self.tables:
                return httpx.Response(
                    403, json={"error": {"type": "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"}}
                )
            return httpx.Response(200, json={"tables": self.tables[base]})
        if request.method == "POST":
            if self.create_status is not None:
                return httpx.Response(self.create_status[0], json=self.create_status[1])
            _, _, base, table = path.split("/")
            self.records.append({"base": base, "table": table, **body})
            return httpx.Response(200, json={"id": "recX", "fields": body["fields"]})
        return httpx.Response(404, json={"error": "NOT_FOUND"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/v1/token"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        auth = request.headers.get("authorization", "")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_calls.append((auth, form))
        expected = base64.b64encode(f"{CLIENT_ID}:{self.secret}".encode()).decode()
        if auth != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        assert "client_secret" not in form
        if form["grant_type"] == "authorization_code":
            challenge = self.challenges.pop(form["code"], "")
            if not challenge or _s256(form.get("code_verifier", "")) != challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
        else:
            assert form["grant_type"] == "refresh_token"
            if form["refresh_token"] not in self.live_refresh:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "used"}
                )
            # Single-use: the old pair dies with this refresh.
            self.live_refresh.discard(form["refresh_token"])
            self.valid.clear()
        self.issued += 1
        access, refresh = f"at-{self.issued}", f"rt-{self.issued}"
        self.valid.add(access)
        self.live_refresh.add(refresh)
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "Bearer ",
                "scope": self.scope,
                "expires_in": 3600,
                "refresh_expires_in": 5184000,
            },
        )


@pytest.fixture
def air(monkeypatch):
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers import airtable, oauth_http

    fake = FakeAirtable()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_AIRTABLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_AIRTABLE_CLIENT_SECRET", SECRET)
    airtable.clear_cache()
    field_options.clear_cache()
    yield fake
    airtable.clear_cache()
    field_options.clear_cache()


def _url(pid: str) -> str:
    return f"/api/v1/sites/by-pocket/{pid}/integrations/airtable"


async def _connect(c, pid: str, air: FakeAirtable) -> dict:
    started = await c.post("/api/v1/integrations/airtable/connect", json={"pocket_id": pid})
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    location = urlsplit(start.headers["location"])
    assert (location.netloc, location.path) == ("airtable.com", "/oauth2/v1/authorize")
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert query["code_challenge_method"] == "S256"
    air.challenges["code-1"] = query["code_challenge"]
    cb = await c.get(
        "/api/v1/integrations/airtable/callback", params={"code": "code-1", "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text)


async def _linked(c, air: FakeAirtable, *, table: str | None = TBL_LEADS) -> tuple[str, str]:
    pid = await _pocket()
    site = await _site(pid)
    msg = await _connect(c, pid, air)
    assert msg["status"] == "active", msg
    if table:
        put = await c.put(_url(pid), json={"settings": {"base_id": BASE_A, "table_name": table}})
        assert put.status_code == 200, put.text
    return pid, str(site.id)


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.find_one(IntegrationConnection.provider == "airtable")


async def _binding():
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    return await SiteIntegration.find_one(SiteIntegration.provider == "airtable")


def _options(c, pid: str, key: str):
    return c.get(f"{_url(pid)}/fields/{key}/options")


# ----------------------------------------------------------------- unit


def test_registry_authorize_url_and_availability(monkeypatch, air):
    from pocketpaw_ee.integrations.providers import get_provider
    from pocketpaw_ee.integrations.providers.base import ProviderError

    provider = get_provider("airtable")
    assert (provider.kind, provider.category, provider.plan) == ("connect", "data", "site")
    assert provider.supports_pkce is True
    fields = {f.key: f for f in provider.settings_fields}
    assert fields["base_id"].options_source == fields["table_name"].options_source == "remote"
    assert fields["base_id"].required and fields["table_name"].required
    assert fields["table_name"].depends_on == ("base_id",)
    spec = provider.actions["create_record"]
    assert spec.public and spec.write
    split = urlsplit(provider.authorize_url("st-1", "https://connect.example.com/cb", "ch-1"))
    assert (split.netloc, split.path) == ("airtable.com", "/oauth2/v1/authorize")
    assert {k: v[0] for k, v in parse_qs(split.query).items()} == {
        "client_id": CLIENT_ID,
        "redirect_uri": "https://connect.example.com/cb",
        "response_type": "code",
        "scope": "data.records:write schema.bases:read user.email:read",
        "state": "st-1",
        "code_challenge": "ch-1",
        "code_challenge_method": "S256",
    }
    with pytest.raises(ProviderError):
        provider.authorize_url("st-1", "https://connect.example.com/cb")
    assert provider.availability() == "available"
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_AIRTABLE_CLIENT_SECRET")
    assert provider.availability() == "unconfigured"


@pytest.mark.asyncio
async def test_refresh_rotates_single_use_tokens_and_maps_invalid_grant(air):
    from pocketpaw_ee.integrations.providers.airtable import AirtableProvider
    from pocketpaw_ee.integrations.providers.base import InvalidGrant

    air.live_refresh.add("rt-0")
    provider = AirtableProvider()
    first = await provider.refresh("rt-0")
    assert (first.access_token, first.refresh_token, first.expires_in) == ("at-1", "rt-1", 3600)
    with pytest.raises(InvalidGrant):
        await provider.refresh("rt-0")  # already spent
    second = await provider.refresh("rt-1")
    assert second.refresh_token == "rt-2"
    form = air.token_calls[0][1]
    assert form == {"grant_type": "refresh_token", "refresh_token": "rt-0"}


# ------------------------------------------------------------- connect flow


@pytest.mark.asyncio
async def test_connect_uses_pkce_basic_auth_and_whoami(beanie_test_db, air):
    async with _client() as c:
        await _linked(c, air, table=None)
    [(auth, form)] = air.token_calls
    assert auth.startswith("Basic ")
    assert form["grant_type"] == "authorization_code" and form["code"] == "code-1"
    assert len(form["code_verifier"]) >= 43
    assert form["redirect_uri"].endswith("/api/v1/integrations/airtable/callback")
    conn = await _conn()
    assert (conn.account_ref, conn.account_label, conn.status) == (
        USER_ID,
        "ada@example.com",
        "active",
    )


@pytest.mark.asyncio
async def test_grant_without_the_write_scope_is_refused(beanie_test_db, air):
    air.scope = "schema.bases:read user.email:read"
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid, air)
    assert msg["status"] == "error"
    assert await _conn() is None


@pytest.mark.asyncio
async def test_refresh_after_expiry_stores_the_rotated_token(beanie_test_db, air):
    from pocketpaw_ee.integrations import vault

    async with _client() as c:
        _, sid = await _linked(c, air)
        conn = await _conn()
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await conn.save()
        ok = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "Ada"}})
        assert ok.json() == {"ok": True, "result": {"status": "ok"}}, ok.text
        assert vault._tokens(await _conn())["refresh_token"] == "rt-2"
        # The next expiry spends rt-2, not the dead rt-1.
        conn = await _conn()
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await conn.save()
        again = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "Bo"}})
    assert again.json()["ok"] is True, again.text
    assert [f["refresh_token"] for _, f in air.token_calls[1:]] == ["rt-1", "rt-2"]
    assert vault._tokens(await _conn())["refresh_token"] == "rt-3"


@pytest.mark.asyncio
async def test_a_401_refreshes_once_then_writes(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air)
        air.valid.clear()  # Airtable revoked the access token early
        ok = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "Ada"}})
    assert ok.json()["ok"] is True, ok.text
    assert len(air.records) == 1


@pytest.mark.asyncio
async def test_invalid_grant_flips_needs_reconnect(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air)
        conn = await _conn()
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await conn.save()
        air.live_refresh.clear()
        resp = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "Ada"}})
    assert resp.status_code == 409 and _code(resp) == "not_connected"
    assert (await _conn()).status == "needs_reconnect"


# ------------------------------------------------------------ remote options


@pytest.mark.asyncio
async def test_base_options_are_editable_bases_across_pages(beanie_test_db, air):
    async with _client() as c:
        pid, _ = await _linked(c, air, table=None)
        bases = await _options(c, pid, "base_id")
        tables = await _options(c, pid, "table_name")
    assert bases.status_code == 200, bases.text
    assert bases.json()["options"] == [
        {"value": BASE_A, "label": "Leads base"},
        {"value": BASE_B, "label": "Second"},
        {"value": BASE_P2, "label": "Page two"},
    ]
    # No base picked: no tables, and Airtable is not asked.
    assert tables.json()["options"] == []
    assert not any("/tables" in path for _, path, _, _ in air.api_calls)


@pytest.mark.asyncio
async def test_table_options_follow_the_picked_base(beanie_test_db, air):
    async with _client() as c:
        pid, _ = await _linked(c, air, table=None)
        assert (await _options(c, pid, "table_name")).json()["options"] == []
        put = await c.put(_url(pid), json={"settings": {"base_id": BASE_A}})
        assert put.status_code == 200, put.text
        # The empty list cached before the base was picked is not served.
        a_tables = (await _options(c, pid, "table_name")).json()["options"]
        put = await c.put(_url(pid), json={"settings": {"base_id": BASE_B}})
        b_tables = (await _options(c, pid, "table_name")).json()["options"]
    assert a_tables == [
        {"value": TBL_LEADS, "label": "Leads"},
        {"value": TBL_OTHER, "label": "Other"},
    ]
    assert b_tables == [{"value": TBL_B, "label": "B table"}]


@pytest.mark.asyncio
async def test_a_draft_base_lists_its_tables_before_it_is_saved(beanie_test_db, air):
    async with _client() as c:
        pid, _ = await _linked(c, air, table=None)
        put = await c.put(_url(pid), json={"settings": {"base_id": BASE_A}})
        assert put.status_code == 200, put.text
        draft = await c.get(f"{_url(pid)}/fields/table_name/options", params={"base_id": BASE_B})
        foreign = await c.get(f"{_url(pid)}/fields/table_name/options", params={"base_id": BASE_RO})
        saved = await _options(c, pid, "table_name")
    assert draft.status_code == 200, draft.text
    assert draft.json()["options"] == [{"value": TBL_B, "label": "B table"}]
    # A base the grant can't edit is not a base option: refused, its tables never asked.
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (422, "invalid_settings")
    assert not any(BASE_RO in path for _, path, _, _ in air.api_calls)
    assert [o["value"] for o in saved.json()["options"]] == [TBL_LEADS, TBL_OTHER]
    assert (await _binding()).settings == {"base_id": BASE_A}


@pytest.mark.asyncio
async def test_put_checks_the_table_against_the_new_base(beanie_test_db, air):
    async with _client() as c:
        pid, _ = await _linked(c, air)
        # Moving base and table together validates the table on the new base.
        moved = await c.put(_url(pid), json={"settings": {"base_id": BASE_B, "table_name": TBL_B}})
        assert moved.status_code == 200, moved.text
        # Re-sending the stored table with a new base re-checks it: not on base A.
        stale = await c.put(_url(pid), json={"settings": {"base_id": BASE_A, "table_name": TBL_B}})
        assert stale.status_code == 422, stale.text
        # Changing only the base drops the stored table: it named a table there.
        only_base = await c.put(_url(pid), json={"settings": {"base_id": BASE_A}})
        assert only_base.status_code == 200, only_base.text
        assert only_base.json()["status"] == "setup_required"
    binding = await _binding()
    assert binding.settings == {"base_id": BASE_A}


@pytest.mark.asyncio
async def test_put_refuses_a_base_the_grant_cant_edit(beanie_test_db, air):
    async with _client() as c:
        pid, _ = await _linked(c, air, table=None)
        resp = await c.put(_url(pid), json={"settings": {"base_id": BASE_RO}})
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------- create_record


@pytest.mark.asyncio
async def test_create_record_maps_onto_existing_text_fields(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air)
        resp = await _gw(
            c,
            sid,
            "airtable",
            "create_record",
            args={
                "fields": {
                    "  name ": "Ada\nLovelace",
                    "EMAIL": "ada@example.com",
                    "message": "line one\nline\x00 two",
                    "Notes": "**hi**",
                    "Score": "99",  # number field: dropped, never typecast
                    "Created": "now",  # computed field: dropped
                    "utm source": "ads",  # no such field: dropped, nothing created
                }
            },
        )
    assert resp.json() == {"ok": True, "result": {"status": "ok"}}, resp.text
    [record] = air.records
    assert record == {
        "base": BASE_A,
        "table": TBL_LEADS,
        "fields": {
            "fldName0000000001": "Ada Lovelace",
            "fldEmail000000001": "ada@example.com",
            "fldMessage0000001": "line one\nline two",
            "fldNotes000000001": "**hi**",
        },
        "typecast": False,
    }
    # No schema write ever: the only calls are reads and the record create.
    assert {(m, p) for m, p, _, _ in air.api_calls if m != "GET"} == {
        ("POST", f"/v0/{BASE_A}/{TBL_LEADS}")
    }


@pytest.mark.asyncio
async def test_create_record_drops_a_bad_email_and_refuses_no_match(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air)
        kept = await _gw(
            c,
            sid,
            "airtable",
            "create_record",
            args={"fields": {"Email": "not an email", "Name": "Ada"}},
        )
        none = await _gw(
            c, sid, "airtable", "create_record", args={"fields": {"Whatever": "x", "Email": "no"}}
        )
    assert kept.json()["ok"] is True
    assert air.records[0]["fields"] == {"fldName0000000001": "Ada"}
    assert none.status_code == 422 and _code(none) == "invalid_args"
    # A fixed message: it never names the table's fields.
    message = none.json()["error"]["message"]
    assert message == "These fields can't be saved to this form's table."
    assert not any(f["name"].lower() in message.lower() for f in LEADS_FIELDS)
    assert len(air.records) == 1


@pytest.mark.asyncio
async def test_create_record_caps(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air)
        too_many = await _gw(
            c,
            sid,
            "airtable",
            "create_record",
            args={"fields": {f"k{i}": "v" for i in range(51)}},
        )
        too_long = await _gw(
            c, sid, "airtable", "create_record", args={"fields": {"Name": "x" * 2001}}
        )
        bad_key = await _gw(c, sid, "airtable", "create_record", args={"fields": {"!!": "x"}})
    for resp in (too_many, too_long, bad_key):
        assert _code(resp) == "invalid_args", resp.text
    assert air.records == []


@pytest.mark.asyncio
async def test_schema_is_cached_between_submissions(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air)
        before = sum("/tables" in p for _, p, _, _ in air.api_calls)
        for name in ("A", "B", "C"):
            ok = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": name}})
            assert ok.json()["ok"] is True
    assert sum("/tables" in p for _, p, _, _ in air.api_calls) == before + 1


@pytest.mark.asyncio
async def test_unpicked_table_is_setup_required(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air, table=None)
        resp = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "Ada"}})
    assert _code(resp) == "setup_required"


# ------------------------------------------------------------- error mapping


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (403, {"error": {"type": "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"}}, "setup_required"),
        (404, {"error": "NOT_FOUND"}, "setup_required"),
        (422, {"error": {"type": "INVALID_VALUE_FOR_COLUMN"}}, "invalid_args"),
        (429, {"error": {"type": "RATE_LIMIT_REACHED"}}, "rate_limited"),
        (500, {"error": {"type": "SERVER_ERROR"}}, "provider_error"),
    ],
)
async def test_create_errors_map_to_gateway_codes(beanie_test_db, air, status, body, code):
    async with _client() as c:
        _, sid = await _linked(c, air)
        air.create_status = (status, body)
        resp = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "Ada"}})
    assert _code(resp) == code, resp.text
    binding = await _binding()
    assert bool(binding.setup_error) is (code == "setup_required")


@pytest.mark.asyncio
async def test_unreachable_table_marks_the_site_and_a_write_clears_it(beanie_test_db, air):
    async with _client() as c:
        pid, sid = await _linked(c, air)
        air.schema_status = (403, {"error": {"type": "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"}})
        refused = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "A"}})
        assert _code(refused) == "setup_required"
        assert (await _binding()).setup_error == "table_unreachable"
        air.schema_status = None
        ok = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "A"}})
        view = (await c.get(_url(pid).rsplit("/", 1)[0])).json()
    assert ok.json()["ok"] is True, ok.text
    assert (await _binding()).setup_error == ""
    assert view[0]["status"] == "connected"


@pytest.mark.asyncio
async def test_deleted_table_is_setup_required(beanie_test_db, air):
    async with _client() as c:
        _, sid = await _linked(c, air)
        air.tables[BASE_A] = [t for t in air.tables[BASE_A] if t["id"] != TBL_LEADS]
        resp = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "A"}})
    assert _code(resp) == "setup_required"
    assert (await _binding()).setup_error == "table_missing"


# --------------------------------------------------------- security review


@pytest.mark.asyncio
async def test_basic_auth_is_standard_base64(monkeypatch, air):
    from pocketpaw_ee.integrations.providers.airtable import AirtableProvider

    # Standard base64 of "air-client:sk~~~~?>" has both "+" and "/".
    air.secret = "sk~~~~?>"
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_AIRTABLE_CLIENT_SECRET", air.secret)
    air.live_refresh.add("rt-0")
    grant = await AirtableProvider().refresh("rt-0")
    assert grant.access_token == "at-1"
    auth = air.token_calls[0][0]
    assert auth == "Basic YWlyLWNsaWVudDpza35+fn4/Pg=="
    assert "+" in auth and "/" in auth


@pytest.mark.asyncio
async def test_ids_must_fullmatch(air):
    from pocketpaw_ee.integrations.providers import airtable
    from pocketpaw_ee.integrations.providers.base import SetupContext

    assert airtable._BASE_ID.fullmatch(BASE_A)
    for bad in (BASE_A + "\n", BASE_A + "/x", "x" + BASE_A, BASE_A[:-1]):
        assert airtable._BASE_ID.fullmatch(bad) is None
    assert airtable._TABLE_ID.fullmatch(TBL_LEADS + "\n") is None
    assert airtable._seg("a/b?c") == "a%2Fb%3Fc"
    binding = type("B", (), {"settings": {"base_id": BASE_A + "\n"}})()
    ctx = SetupContext(workspace_id="w", pocket_id="p", user_id="u", provider="airtable")
    assert await airtable.AirtableProvider().field_options(binding, ctx, "table_name") == []
    assert air.api_calls == []


@pytest.mark.asyncio
async def test_writable_fields_limit_what_visitors_write(beanie_test_db, air):
    async with _client() as c:
        pid, sid = await _linked(c, air)
        put = await c.put(_url(pid), json={"settings": {"writable_fields": " name , EMAIL "}})
        assert put.status_code == 200 and put.json()["status"] == "connected", put.text
        ok = await _gw(
            c,
            sid,
            "airtable",
            "create_record",
            args={"fields": {"Name": "Ada", "Email": "ada@example.com", "Message": "hi"}},
        )
        only_other = await _gw(
            c, sid, "airtable", "create_record", args={"fields": {"Message": "hi"}}
        )
    assert ok.json()["ok"] is True, ok.text
    assert air.records[0]["fields"] == {
        "fldName0000000001": "Ada",
        "fldEmail000000001": "ada@example.com",
    }
    assert _code(only_other) == "invalid_args"
    assert len(air.records) == 1


@pytest.mark.asyncio
async def test_unknown_writable_field_is_setup_required_until_fixed(beanie_test_db, air):
    async with _client() as c:
        pid, sid = await _linked(c, air)
        # "Nope" doesn't exist; "Score" exists but is a number field.
        for bad in ("Name, Nope", "Score"):
            put = await c.put(_url(pid), json={"settings": {"writable_fields": bad}})
            assert put.status_code == 200, put.text
            assert put.json()["status"] == "setup_required"
        refused = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "A"}})
        assert _code(refused) == "setup_required"
        assert air.records == []
        fixed = await c.put(_url(pid), json={"settings": {"writable_fields": "Name"}})
        assert fixed.json()["status"] == "connected", fixed.text
        ok = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "A"}})
    assert ok.json()["ok"] is True, ok.text


@pytest.mark.asyncio
async def test_writable_field_dropped_from_the_table_refuses_writes(beanie_test_db, air):
    from pocketpaw_ee.integrations.providers import airtable

    async with _client() as c:
        pid, sid = await _linked(c, air)
        await c.put(_url(pid), json={"settings": {"writable_fields": "Name, Email"}})
        # The owner deletes the Email column later: the allowlist is never widened.
        air.tables[BASE_A][0]["fields"] = [LEADS_FIELDS[0]]
        airtable.clear_cache()
        resp = await _gw(c, sid, "airtable", "create_record", args={"fields": {"Name": "A"}})
    assert _code(resp) == "setup_required"
    assert (await _binding()).setup_error == "writable_fields_unknown"
    assert air.records == []
