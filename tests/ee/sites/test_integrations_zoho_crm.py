# tests/ee/sites/test_integrations_zoho_crm.py: the zoho_crm provider.
#
# Runs end to end through the real routes (connect -> /start -> Zoho -> callback
# with ``accounts-server`` -> site link -> public gateway) against an in-memory fake
# of Zoho Accounts and the CRM v8 API, swapped in as ``oauth_http._transport``. No
# network. Covers: the accounts-server and api_domain allowlists (no token request
# to an unlisted host), the regional token host and api_domain persisted in
# connection meta and used, refresh + invalid_code -> InvalidGrant, create /
# fill-empty duplicate / exists, the Last_Name and Company defaults, the
# DUPLICATE_DATA fallback, a 401 refreshing once, 429 and mandatory-field mapping.
from __future__ import annotations

import json
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

ZOHO_ID = "1000.zoho-client"
EU_ACCOUNTS = "https://accounts.zoho.eu"


class FakeZoho:
    def __init__(self) -> None:
        self.api_domain = "https://www.zohoapis.eu"
        self.leads: dict[str, dict] = {}
        self.valid: set[str] = set()
        self.issued = 0
        self.dead_refresh: set[str] = set()
        self.token_calls: list[tuple[str, dict]] = []
        self.api_calls: list[tuple[str, str, str, dict, dict]] = []
        self.rate_limited = False
        self.mandatory = ""  # a custom field the org requires
        self.search_lag = False
        self.unique_phone = False  # the org made Phone a unique field

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.startswith("accounts."):
            return self._token(host, request)
        assert host.startswith("www.zohoapis."), host
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme != "Zoho-oauthtoken" or token not in self.valid:
            return httpx.Response(401, json={"code": "INVALID_TOKEN", "status": "error"})
        path = request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        self.api_calls.append((host, request.method, path, params, body))
        if self.rate_limited:
            return httpx.Response(429, json={"code": "TOO_MANY_REQUESTS", "status": "error"})
        if path == "/crm/v8/org":
            return httpx.Response(
                200, json={"org": [{"id": "777", "company_name": "Acme Ltd", "zgid": "9"}]}
            )
        if path == "/crm/v8/Leads/search":
            assert params["fields"].split(",")[0] == "Email"
            # Zoho's email search matches every email field, Secondary_Email too.
            wanted = params["email"]
            hits = [
                r
                for r in self.leads.values()
                if wanted in (r["Email"].lower(), r.get("Secondary_Email", "").lower())
            ]
            if self.search_lag or not hits:
                return httpx.Response(204)
            return httpx.Response(200, json={"data": hits, "info": {"count": len(hits)}})
        if path == "/crm/v8/Leads" and request.method == "POST":
            [record] = body["data"]
            if self.mandatory:
                err = {"code": "MANDATORY_NOT_FOUND", "details": {"api_name": self.mandatory}}
                return httpx.Response(400, json={"data": [{**err, "status": "error"}]})
            dup = next(
                (
                    r
                    for r in self.leads.values()
                    if r["Email"] == record["Email"]
                    or (
                        self.unique_phone
                        and record.get("Phone")
                        and r.get("Phone") == record["Phone"]
                    )
                ),
                None,
            )
            if dup is not None:
                err = {
                    "code": "DUPLICATE_DATA",
                    "details": {"api_name": "Email", "duplicate_record": {"id": dup["id"]}},
                    "status": "error",
                }
                return httpx.Response(400, json={"data": [err]})
            lead_id = f"55{len(self.leads) + 1:04d}"
            self.leads[lead_id] = {"id": lead_id, **record}
            ok = {"code": "SUCCESS", "details": {"id": lead_id}, "status": "success"}
            return httpx.Response(201, json={"data": [ok]})
        lead_id = path.rsplit("/", 1)[1]
        if request.method == "GET":
            return httpx.Response(200, json={"data": [self.leads[lead_id]]})
        if request.method == "PUT":
            self.leads[lead_id].update(body["data"][0])
            ok = {"code": "SUCCESS", "details": {"id": lead_id}, "status": "success"}
            return httpx.Response(200, json={"data": [ok]})
        return httpx.Response(404)

    def _token(self, host: str, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/v2/token"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["client_id"] == ZOHO_ID and form["client_secret"] == "zoho-secret"
        self.token_calls.append((host, form))
        refreshing = form["grant_type"] == "refresh_token"
        dead = refreshing and form["refresh_token"] in self.dead_refresh
        if dead or form.get("code") == "used":
            # Zoho answers token errors with a 200.
            return httpx.Response(200, json={"error": "invalid_code"})
        self.issued += 1
        token = f"z-access-{self.issued}"
        self.valid.add(token)
        body = {
            "access_token": token,
            "expires_in": 3600,
            "api_domain": self.api_domain,
            "token_type": "Bearer",
        }
        if not refreshing:
            body["refresh_token"] = "z-refresh"
        return httpx.Response(200, json=body)


@pytest.fixture
def zoho(monkeypatch):
    from pocketpaw_ee.integrations.providers import oauth_http

    fake = FakeZoho()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_ZOHO_CLIENT_ID", ZOHO_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_ZOHO_CLIENT_SECRET", "zoho-secret")
    return fake


async def _connect(c, pid: str, accounts_server: str | None = EU_ACCOUNTS, code="c1") -> dict:
    started = await c.post("/api/v1/integrations/zoho_crm/connect", json={"pocket_id": pid})
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    params = {"code": code, "state": query["state"], "location": "eu"}
    if accounts_server is not None:
        params["accounts-server"] = accounts_server
    cb = await c.get("/api/v1/integrations/zoho_crm/callback", params=params)
    assert cb.status_code == 200
    return _message(cb.text)


async def _linked(c, zoho: FakeZoho) -> str:
    pid = await _pocket()
    site = await _site(pid)
    msg = await _connect(c, pid)
    assert msg["status"] == "active", msg
    return str(site.id)


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.find_one(IntegrationConnection.provider == "zoho_crm")


# ----------------------------------------------------------------- unit


def test_authorize_url_registry_and_availability(monkeypatch, zoho):
    from pocketpaw_ee.integrations.providers import get_provider

    provider = get_provider("zoho_crm")
    assert (provider.kind, provider.category, provider.plan) == ("connect", "crm", "site")
    assert provider.settings_fields == () and provider.supports_pkce is False
    assert set(provider.actions) == {"create_lead"}
    assert provider.actions["create_lead"].public and provider.actions["create_lead"].write
    split = urlsplit(provider.authorize_url("st-1", "https://connect.example.com/cb"))
    q = {k: v[0] for k, v in parse_qs(split.query).items()}
    assert split.scheme == "https" and split.netloc == "accounts.zoho.com"
    assert split.path == "/oauth/v2/auth"
    assert q == {
        "response_type": "code",
        "client_id": ZOHO_ID,
        "redirect_uri": "https://connect.example.com/cb",
        "scope": "ZohoCRM.modules.leads.CREATE,ZohoCRM.modules.leads.READ,"
        "ZohoCRM.modules.leads.UPDATE,ZohoCRM.org.READ,ZohoSearch.securesearch.READ",
        "access_type": "offline",
        "prompt": "consent",
        "state": "st-1",
    }
    assert provider.availability() == "available"
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_ZOHO_CLIENT_SECRET")
    assert provider.availability() == "unconfigured"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://accounts.zoho.com", "https://accounts.zoho.com"),
        ("https://accounts.zoho.eu/", "https://accounts.zoho.eu"),
        ("https://ACCOUNTS.ZOHO.IN", "https://accounts.zoho.in"),
        ("https://accounts.zoho.com.au", "https://accounts.zoho.com.au"),
        ("https://accounts.zohocloud.ca", "https://accounts.zohocloud.ca"),
        ("https://accounts.zoho.sa", "https://accounts.zoho.sa"),
        ("https://accounts.zoho.uk", "https://accounts.zoho.uk"),
        ("https://accounts.zoho.com.cn", "https://accounts.zoho.com.cn"),
        ("http://accounts.zoho.eu", None),
        ("https://accounts.zoho.eu.evil.com", None),
        ("https://evil.com/accounts.zoho.eu", None),
        ("https://accounts.zoho.eu@evil.com", None),
        ("https://user@accounts.zoho.eu", None),
        ("https://accounts.zoho.eu:8443", None),
        ("https://accounts.zoho.eu/oauth/v2/token", None),
        ("https://accounts.zoho.eu?x=1", None),
        ("https://evilzoho.com", None),
        ("https://accounts.zoho.evil", None),
        ("//accounts.zoho.eu", None),
        ("javascript:alert(1)", None),
        ("https://www.zohoapis.eu", None),
        ("", None),
    ],
)
def test_accounts_server_allowlist(value, expected):
    from pocketpaw_ee.integrations.providers.zoho_crm import accounts_origin

    assert accounts_origin(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.zohoapis.com", "https://www.zohoapis.com"),
        ("https://www.zohoapis.eu", "https://www.zohoapis.eu"),
        ("https://www.zohoapis.com.au", "https://www.zohoapis.com.au"),
        ("http://www.zohoapis.com", None),
        ("https://www.zohoapis.com.evil.io", None),
        ("https://accounts.zoho.com", None),
        ("https://www.zohoapis.com:444", None),
        ("https://www.zohoapis.com/crm", None),
    ],
)
def test_api_domain_allowlist(value, expected):
    from pocketpaw_ee.integrations.providers.zoho_crm import api_origin

    assert api_origin(value) == expected


@pytest.mark.asyncio
async def test_refresh_uses_the_stored_accounts_server_and_maps_invalid_code(zoho):
    from pocketpaw_ee.integrations.providers.base import InvalidGrant, ProviderError
    from pocketpaw_ee.integrations.providers.zoho_crm import ZohoCrmProvider

    provider = ZohoCrmProvider()
    grant = await provider.refresh(
        "z-refresh", meta={"accounts_server": "https://accounts.zoho.in"}
    )
    assert grant.access_token == "z-access-1" and grant.expires_in == 3600
    assert grant.refresh_token == ""  # Zoho does not rotate: the vault keeps the old one
    assert zoho.token_calls[-1][0] == "accounts.zoho.in"
    zoho.dead_refresh.add("gone")
    with pytest.raises(InvalidGrant):
        await provider.refresh("gone", meta={"accounts_server": "https://accounts.zoho.in"})
    calls = len(zoho.token_calls)
    for meta in ({}, {"accounts_server": "https://accounts.zoho.in.evil.com"}):
        with pytest.raises(ProviderError):
            await provider.refresh("z-refresh", meta=meta)
    assert len(zoho.token_calls) == calls  # never sent the secret anywhere


# ------------------------------------------------------------- connect flow


@pytest.mark.asyncio
async def test_connect_persists_region_and_uses_it(beanie_test_db, zoho):
    async with _client() as c:
        sid = await _linked(c, zoho)
        created = await _gw(c, sid, "zoho_crm", "create_lead", args={"email": "ada@example.com"})
    assert created.json() == {"ok": True, "result": {"status": "ok"}}, created.text
    [(host, form)] = zoho.token_calls
    assert host == "accounts.zoho.eu" and form["grant_type"] == "authorization_code"
    assert form["redirect_uri"].endswith("/api/v1/integrations/zoho_crm/callback")
    conn = await _conn()
    assert conn.account_ref == "777" and conn.account_label == "Acme Ltd"
    assert conn.meta["accounts_server"] == EU_ACCOUNTS
    assert conn.meta["api_domain"] == "https://www.zohoapis.eu"
    assert {call[0] for call in zoho.api_calls} == {"www.zohoapis.eu"}


@pytest.mark.asyncio
async def test_connect_without_accounts_server_uses_the_us_server(beanie_test_db, zoho):
    zoho.api_domain = "https://www.zohoapis.com"
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid, accounts_server=None)
    assert msg["status"] == "active", msg
    assert zoho.token_calls[0][0] == "accounts.zoho.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server",
    [
        "https://evil.com",
        "https://accounts.zoho.eu.evil.com",
        "http://accounts.zoho.eu",
        "https://accounts.zoho.eu@evil.com",
    ],
)
async def test_unlisted_accounts_server_gets_no_token_request(beanie_test_db, zoho, server):
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid, accounts_server=server)
    assert msg["status"] == "error" and msg["error"] == "provider_error"
    assert zoho.token_calls == [] and zoho.api_calls == []
    assert await _conn() is None


@pytest.mark.asyncio
async def test_unlisted_api_domain_is_refused(beanie_test_db, zoho):
    zoho.api_domain = "https://www.zohoapis.eu.attacker.io"
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid)
    assert msg["status"] == "error" and msg["error"] == "provider_error"
    assert zoho.api_calls == [] and await _conn() is None


@pytest.mark.asyncio
async def test_used_code_fails_the_callback(beanie_test_db, zoho):
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid, code="used")
    assert msg["status"] == "error" and msg["error"] == "provider_error"


# ----------------------------------------------------------------- create_lead


@pytest.mark.asyncio
async def test_create_then_fill_empty_never_overwrite(beanie_test_db, zoho):
    async with _client() as c:
        sid = await _linked(c, zoho)
        created = await _gw(
            c,
            sid,
            "zoho_crm",
            "create_lead",
            args={"email": " Ada@Example.com ", "first_name": "Ada\n", "message": "Hi\x00 there"},
        )
        updated = await _gw(
            c,
            sid,
            "zoho_crm",
            "create_lead",
            args={
                "email": "ada@example.com",
                "first_name": "Eve",
                "last_name": "Mallory",
                "phone": "+1 555",
                "company": "Analytical",
            },
        )
        unchanged = await _gw(
            c,
            sid,
            "zoho_crm",
            "create_lead",
            args={"email": "ada@example.com", "first_name": "Mallory", "message": "spam"},
        )
    assert created.json()["result"] == {"status": "ok"}, created.text
    assert updated.json()["result"] == {"status": "ok"}, updated.text
    assert unchanged.json()["result"] == {"status": "ok"}, unchanged.text
    [lead] = zoho.leads.values()
    # Defaults on insert (Last_Name = local part, Company placeholder); the second
    # call filled only the empty Phone, overwriting none of the stored values.
    assert {k: v for k, v in lead.items() if k != "id"} == {
        "Email": "ada@example.com",
        "First_Name": "Ada",
        "Last_Name": "ada",
        "Company": "Website visitor",
        "Description": "Hi there",
        "Phone": "+1 555",
    }
    [put] = [call for call in zoho.api_calls if call[1] == "PUT"]
    assert put[2] == f"/crm/v8/Leads/{lead['id']}"
    assert put[4] == {"data": [{"Phone": "+1 555"}]}


@pytest.mark.asyncio
async def test_supplied_names_and_company_win_over_defaults(beanie_test_db, zoho):
    async with _client() as c:
        sid = await _linked(c, zoho)
        resp = await _gw(
            c,
            sid,
            "zoho_crm",
            "create_lead",
            args={"email": "b@x.io", "last_name": "Lovelace", "company": "Engines"},
        )
    assert resp.json()["result"] == {"status": "ok"}
    [lead] = zoho.leads.values()
    assert (lead["Last_Name"], lead["Company"]) == ("Lovelace", "Engines")
    assert "First_Name" not in lead and "Description" not in lead


@pytest.mark.asyncio
async def test_duplicate_data_on_insert_takes_the_fill_empty_path(beanie_test_db, zoho):
    async with _client() as c:
        sid = await _linked(c, zoho)
        await _gw(c, sid, "zoho_crm", "create_lead", args={"email": "ada@example.com"})
        zoho.search_lag = True  # the search index has not caught up yet
        resp = await _gw(
            c, sid, "zoho_crm", "create_lead", args={"email": "ada@example.com", "phone": "42"}
        )
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    [lead] = zoho.leads.values()
    assert lead["Phone"] == "42" and lead["Last_Name"] == "ada"


@pytest.mark.asyncio
async def test_a_401_refreshes_once_at_the_regional_server(beanie_test_db, zoho):
    async with _client() as c:
        sid = await _linked(c, zoho)
        zoho.valid.clear()  # Zoho revoked the access token early
        resp = await _gw(c, sid, "zoho_crm", "create_lead", args={"email": "a@b.co"})
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    refreshes = [call for call in zoho.token_calls if call[1]["grant_type"] == "refresh_token"]
    assert [(h, f["refresh_token"]) for h, f in refreshes] == [("accounts.zoho.eu", "z-refresh")]


@pytest.mark.asyncio
async def test_dead_refresh_token_needs_reconnect(beanie_test_db, zoho):
    async with _client() as c:
        sid = await _linked(c, zoho)
        zoho.valid.clear()
        zoho.dead_refresh.add("z-refresh")  # the 21st grant pushed ours out
        resp = await _gw(c, sid, "zoho_crm", "create_lead", args={"email": "a@b.co"})
    assert resp.status_code == 409 and _code(resp) == "not_connected", resp.text
    assert (await _conn()).status == "needs_reconnect"


@pytest.mark.asyncio
async def test_validation_rate_limit_and_mandatory_fields(beanie_test_db, zoho):
    async with _client() as c:
        sid = await _linked(c, zoho)
        bad = [
            await _gw(c, sid, "zoho_crm", "create_lead", args=args)
            for args in (
                {"email": "nope"},
                {"first_name": "A"},
                {"email": "a@b.co", "Lead_Status": "x"},
                {"email": "a@b.co", "first_name": "a" * 41},
                {"email": "a@b.co", "last_name": "a" * 81},
                {"email": "a@b.co", "phone": "1" * 31},
                {"email": "a@b.co", "company": "c" * 201},
                {"email": "a@b.co", "message": "m" * 2001},
            )
        ]
        zoho.mandatory = "Industry"
        mandatory = await _gw(c, sid, "zoho_crm", "create_lead", args={"email": "a@b.co"})
        zoho.mandatory = ""
        zoho.rate_limited = True
        limited = await _gw(c, sid, "zoho_crm", "create_lead", args={"email": "a@b.co"})
    for resp in [*bad, mandatory]:
        assert resp.status_code == 422 and _code(resp) == "invalid_args", resp.text
    assert limited.status_code == 429 and _code(limited) == "rate_limited"
    assert zoho.leads == {}


@pytest.mark.asyncio
async def test_handler_refuses_a_connection_without_a_valid_api_domain(zoho):
    from pocketpaw_ee.integrations.providers.base import ActionContext, ProviderError
    from pocketpaw_ee.integrations.providers.zoho_crm import CreateLeadArgs, _create_lead

    async def _token(rejected: str = "") -> str:  # noqa: ARG001
        return "z-access-x"

    for meta in ({}, {"api_domain": "https://evil.example"}):
        ctx = ActionContext(
            workspace_id="w",
            pocket_id="p",
            site_id="s",
            provider="zoho_crm",
            action="create_lead",
            settings={},
            caller="site_key",
            get_token=_token,
            connection_meta=meta,
        )
        with pytest.raises(ProviderError):
            await _create_lead(ctx, CreateLeadArgs(email="a@b.co"))
    assert zoho.api_calls == []


@pytest.mark.asyncio
async def test_secondary_email_hit_is_not_the_lead(beanie_test_db, zoho):
    zoho.leads["551"] = {
        "id": "551",
        "Email": "boss@corp.com",
        "Secondary_Email": "Ada@Example.com",
        "Last_Name": "Boss",
        "Company": "Corp",
    }
    async with _client() as c:
        sid = await _linked(c, zoho)
        resp = await _gw(
            c, sid, "zoho_crm", "create_lead", args={"email": "ada@example.com", "phone": "42"}
        )
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    assert "Phone" not in zoho.leads["551"]  # the other person's lead is untouched
    assert not [call for call in zoho.api_calls if call[1] == "PUT"]
    [new] = [r for r in zoho.leads.values() if r["id"] != "551"]
    assert new["Email"] == "ada@example.com" and new["Phone"] == "42"


@pytest.mark.asyncio
async def test_duplicate_on_another_field_writes_nothing(beanie_test_db, zoho):
    zoho.unique_phone = True
    zoho.leads["551"] = {"id": "551", "Email": "eve@example.com", "Phone": "42", "Last_Name": "Eve"}
    async with _client() as c:
        sid = await _linked(c, zoho)
        resp = await _gw(
            c,
            sid,
            "zoho_crm",
            "create_lead",
            args={"email": "ada@example.com", "phone": "42", "company": "Engines"},
        )
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    assert zoho.leads["551"] == {
        "id": "551",
        "Email": "eve@example.com",
        "Phone": "42",
        "Last_Name": "Eve",
    }
    assert not [call for call in zoho.api_calls if call[1] == "PUT"]
