# tests/ee/sites/test_integrations_brevo.py: the brevo provider.
#
# Runs end to end through the real routes (connect -> /start -> Brevo -> callback
# -> list pick -> public gateway) against an in-memory fake of Brevo's partner
# OAuth realm and API v3, swapped in as ``oauth_http._transport``. No network.
# Covers: scopes + account ref/label, a grant without contacts access refused,
# refresh (rotating) + invalid_grant -> needs_reconnect, the paged contact-list
# remote select, subscribe (always answering ok) new / duplicate joins the list and
# fills empty names, blacklisted and list-unsubscribed contacts never re-added, attribute
# fallback (an existing contact with bad names answers ok like a new one), scopes
# read from the JWT when the token response has none (fail closed), the list
# missing, 400 / 429 mapping and arg caps.
from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, unquote, urlsplit

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

BREVO_ID = "brevo-client"
LIST = 7


class FakeBrevo:
    def __init__(self) -> None:
        self.lists = {LIST: "Website signups", 3: "Customers"}
        self.contacts: dict[str, dict] = {}
        self.valid: set[str] = set()
        self.issued = 0
        self.refresh_tokens: set[str] = set()
        self.token_calls: list[dict] = []
        self.api_calls: list[tuple[str, str, dict, dict]] = []
        self.scope = "contacts:read contacts:write account:read"
        self.jwt_scope: str | None = None  # set: tokens are JWTs carrying this claim
        self.rate_limited = False
        self.attributes = {"FIRSTNAME", "LASTNAME"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "oauth.brevo.com":
            return self._token(request)
        assert host == "api.brevo.com", host
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or token not in self.valid:
            return httpx.Response(401, json={"code": "unauthorized", "message": "Key not found"})
        path = request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        self.api_calls.append((request.method, path, params, body))
        if self.rate_limited:
            return httpx.Response(429, json={"code": "too_many_requests", "message": "slow"})
        if path == "/v3/account":
            return httpx.Response(
                200,
                json={
                    "organization_id": "5fa2b8c1",
                    "user_id": 1234567,
                    "email": "owner@bakery.co",
                    "companyName": "Bakery Co",
                    "firstName": "O",
                    "lastName": "W",
                },
            )
        if path == "/v3/contacts/lists":
            limit, offset = int(params["limit"]), int(params["offset"])
            assert limit <= 50
            ids = sorted(self.lists, reverse=True)[offset : offset + limit]
            lists = [{"id": i, "name": self.lists[i], "folderId": 1} for i in ids]
            return httpx.Response(200, json={"lists": lists, "count": len(self.lists)})
        if path == "/v3/contacts" and request.method == "POST":
            assert body["updateEnabled"] is False
            if any(i not in self.lists for i in body["listIds"]):
                return httpx.Response(404, json={"code": "document_not_found", "message": "x"})
            if body["email"] in self.contacts:
                return httpx.Response(
                    400, json={"code": "duplicate_parameter", "message": "Contact already exist"}
                )
            if set(body.get("attributes") or {}) - self.attributes:
                return httpx.Response(400, json={"code": "invalid_parameter", "message": "attr"})
            self.contacts[body["email"]] = {
                "email": body["email"],
                "emailBlacklisted": False,
                "attributes": dict(body.get("attributes") or {}),
                "listIds": list(body["listIds"]),
            }
            return httpx.Response(201, json={"id": len(self.contacts)})
        email = unquote(path.rsplit("/", 1)[1])
        assert params.get("identifierType") == "email_id"
        contact = self.contacts.get(email)
        if contact is None:
            return httpx.Response(404, json={"code": "document_not_found", "message": "x"})
        if request.method == "GET":
            return httpx.Response(200, json=contact)
        if request.method == "PUT":
            if set(body.get("attributes") or {}) - self.attributes:
                return httpx.Response(400, json={"code": "invalid_parameter", "message": "attr"})
            contact["attributes"].update(body.get("attributes") or {})
            contact["listIds"] = sorted(set(contact["listIds"]) | set(body.get("listIds") or []))
            return httpx.Response(204)
        return httpx.Response(405)

    def _token(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/realms/partner/oauth/token"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["client_id"] == BREVO_ID and form["client_secret"] == "brevo-secret"
        self.token_calls.append(form)
        if form["grant_type"] == "refresh_token":
            if form["refresh_token"] not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refresh_tokens.discard(form["refresh_token"])  # rotates
        elif form.get("code") == "used":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        token, refresh = f"b-access-{self.issued}", f"b-refresh-{self.issued}"
        if self.jwt_scope is not None:
            claims = json.dumps({"sub": "u", "scope": self.jwt_scope}).encode()
            payload = base64.urlsafe_b64encode(claims).decode().rstrip("=")
            token = f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig{self.issued}"
        self.valid.add(token)
        self.refresh_tokens.add(refresh)
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "refresh_token": refresh,
                "expires_in": 3600,
                "token_type": "Bearer",
                **({"scope": self.scope} if self.scope else {}),
            },
        )

    def writes(self) -> list[tuple[str, str, dict, dict]]:
        return [c for c in self.api_calls if c[0] in ("PUT", "POST")]


@pytest.fixture
def brevo(monkeypatch):
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers import oauth_http

    field_options.clear_cache()
    fake = FakeBrevo()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_BREVO_CLIENT_ID", BREVO_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_BREVO_CLIENT_SECRET", "brevo-secret")
    yield fake
    field_options.clear_cache()


async def _connect(c, pid: str, code: str = "c1") -> dict:
    started = await c.post("/api/v1/integrations/brevo/connect", json={"pocket_id": pid})
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    cb = await c.get(
        "/api/v1/integrations/brevo/callback", params={"code": code, "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text)


def _put_url(pid: str) -> str:
    return f"/api/v1/sites/by-pocket/{pid}/integrations/brevo"


async def _ready(c) -> tuple[str, str]:
    pid = await _pocket()
    site = await _site(pid)
    msg = await _connect(c, pid)
    assert msg["status"] == "active", msg
    put = await c.put(_put_url(pid), json={"settings": {"list_id": str(LIST)}})
    assert put.status_code == 200, put.text
    return pid, str(site.id)


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.find_one(IntegrationConnection.provider == "brevo")


async def _sub(c, sid: str, **args):
    return await _gw(c, sid, "brevo", "subscribe", args=args)


# ----------------------------------------------------------------- unit


def test_registry_authorize_url_and_availability(monkeypatch, brevo):
    from pocketpaw_ee.integrations.providers import get_provider

    provider = get_provider("brevo")
    assert (provider.kind, provider.category, provider.plan) == ("connect", "email", "site")
    assert provider.supports_pkce is False and set(provider.actions) == {"subscribe"}
    assert provider.actions["subscribe"].public and provider.actions["subscribe"].write
    # Single opt-in only: Brevo's double opt-in needs an owner-made template.
    [field] = provider.settings_fields
    assert (field.key, field.options_source, field.required) == ("list_id", "remote", True)
    url = provider.authorize_url("st-1", "https://connect.example.com/cb")
    split = urlsplit(url)
    assert (split.netloc, split.path) == ("oauth.brevo.com", "/realms/partner/oauth/authorize")
    assert "scope=contacts%3Aread%20contacts%3Awrite%20account%3Aread" in split.query
    assert {k: v[0] for k, v in parse_qs(split.query).items()} == {
        "response_type": "code",
        "client_id": BREVO_ID,
        "redirect_uri": "https://connect.example.com/cb",
        "scope": "contacts:read contacts:write account:read",
        "state": "st-1",
    }
    assert provider.availability() == "available"
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_BREVO_CLIENT_ID")
    assert provider.availability() == "unconfigured"


@pytest.mark.asyncio
async def test_refresh_rotates_and_a_dead_token_is_invalid_grant(brevo):
    from pocketpaw_ee.integrations.providers.base import InvalidGrant
    from pocketpaw_ee.integrations.providers.brevo import BrevoProvider

    brevo.refresh_tokens.add("b-refresh-old")
    grant = await BrevoProvider().refresh("b-refresh-old")
    assert (grant.access_token, grant.refresh_token, grant.expires_in) == (
        "b-access-1",
        "b-refresh-1",
        3600,
    )
    with pytest.raises(InvalidGrant):
        await BrevoProvider().refresh("b-refresh-old")  # spent, or 30 days unused


# ------------------------------------------------------------- connect flow


@pytest.mark.asyncio
async def test_connect_reads_the_account_and_pages_contact_lists(beanie_test_db, brevo):
    brevo.lists.update({i: f"List {i}" for i in range(100, 160)})  # 62 lists: two pages
    async with _client() as c:
        pid = await _pocket()
        await _site(pid)
        msg = await _connect(c, pid)
        options = await c.get(f"{_put_url(pid)}/fields/list_id/options")
        bad = await c.put(_put_url(pid), json={"settings": {"list_id": "999"}})
        ok = await c.put(_put_url(pid), json={"settings": {"list_id": str(LIST)}})
    assert msg["status"] == "active", msg
    conn = await _conn()
    assert conn.account_ref == "5fa2b8c1" and conn.account_label == "Bakery Co"
    assert set(conn.scopes) == {"contacts:read", "contacts:write", "account:read"}
    assert conn.expires_at is not None
    values = [o["value"] for o in options.json()["options"]]
    assert len(values) == 62 and str(LIST) in values and "159" in values
    offsets = [c[2]["offset"] for c in brevo.api_calls if c[1] == "/v3/contacts/lists"]
    assert offsets == ["0", "50"]
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_settings")
    assert ok.status_code == 200 and ok.json()["settings"]["list_id"] == str(LIST)


@pytest.mark.asyncio
async def test_a_grant_without_contacts_write_is_refused(beanie_test_db, brevo):
    brevo.scope = "contacts:read account:read"
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid)
    assert msg["status"] == "error" and msg["error"] == "provider_error"
    assert await _conn() is None


# ---------------------------------------------------------------- subscribe


@pytest.mark.asyncio
async def test_subscribe_new_then_duplicate_joins_and_fills_empty_only(beanie_test_db, brevo):
    brevo.contacts["old@example.com"] = {
        "email": "old@example.com",
        "emailBlacklisted": False,
        "attributes": {"FIRSTNAME": "Olga"},
        "listIds": [3],
    }
    async with _client() as c:
        _, sid = await _ready(c)
        new = await _sub(c, sid, email=" Ada@Example.com ", first_name="Ada\x00")
        joined = await _sub(c, sid, email="old@example.com", first_name="Eve", last_name="Lace")
        filled = await _sub(c, sid, email="ada@example.com", last_name="Lovelace")
        same = await _sub(c, sid, email="ada@example.com", first_name="Mallory")
    assert new.json() == {"ok": True, "result": {"status": "ok"}}, new.text
    assert joined.json()["result"] == {"status": "ok"}, joined.text
    assert filled.json()["result"] == {"status": "ok"}, filled.text
    assert same.json()["result"] == {"status": "ok"}, same.text
    ada = brevo.contacts["ada@example.com"]
    assert ada["attributes"] == {"FIRSTNAME": "Ada", "LASTNAME": "Lovelace"}
    assert ada["listIds"] == [LIST]
    old = brevo.contacts["old@example.com"]
    assert old["attributes"] == {"FIRSTNAME": "Olga", "LASTNAME": "Lace"}
    assert old["listIds"] == [3, LIST]
    puts = [w for w in brevo.writes() if w[0] == "PUT"]
    assert puts[0][3] == {"listIds": [LIST], "attributes": {"LASTNAME": "Lace"}}
    assert puts[1][3] == {"attributes": {"LASTNAME": "Lovelace"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contact",
    [
        {"emailBlacklisted": True, "listIds": [3]},
        {"emailBlacklisted": False, "listIds": [3], "listUnsubscribed": [LIST]},
    ],
)
async def test_blacklisted_or_unsubscribed_contacts_are_never_re_added(
    beanie_test_db, brevo, contact
):
    brevo.contacts["gone@example.com"] = {
        "email": "gone@example.com",
        "attributes": {},
        **contact,
    }
    async with _client() as c:
        _, sid = await _ready(c)
        resp = await _sub(c, sid, email="gone@example.com", first_name="Gone")
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    assert [w[0] for w in brevo.writes()] == ["POST"]  # the duplicate create, nothing after
    assert brevo.contacts["gone@example.com"]["listIds"] == [3]
    assert brevo.contacts["gone@example.com"]["attributes"] == {}


@pytest.mark.asyncio
async def test_accounts_with_other_attribute_names_still_subscribe(beanie_test_db, brevo):
    brevo.attributes = {"PRENOM", "NOM"}
    brevo.contacts["old@example.com"] = {
        "email": "old@example.com",
        "emailBlacklisted": False,
        "attributes": {},
        "listIds": [],
    }
    async with _client() as c:
        _, sid = await _ready(c)
        new = await _sub(c, sid, email="a@b.co", first_name="A")
        old = await _sub(c, sid, email="old@example.com", first_name="O")
    assert new.json()["result"] == {"status": "ok"}, new.text
    assert old.json()["result"] == {"status": "ok"}, old.text
    assert brevo.contacts["a@b.co"]["listIds"] == [LIST]
    assert brevo.contacts["old@example.com"]["listIds"] == [LIST]


@pytest.mark.asyncio
async def test_no_list_or_a_deleted_one_is_setup_required(beanie_test_db, brevo):
    async with _client() as c:
        pid = await _pocket()
        site = await _site(pid)
        await _connect(c, pid)
        unpicked = await _sub(c, str(site.id), email="a@b.co")
        await c.put(_put_url(pid), json={"settings": {"list_id": str(LIST)}})
        del brevo.lists[LIST]
        deleted = await _sub(c, str(site.id), email="a@b.co")
    for resp in (unpicked, deleted):
        assert resp.status_code == 409 and _code(resp) == "setup_required", resp.text
    assert brevo.contacts == {}


@pytest.mark.asyncio
async def test_validation_and_rate_limit(beanie_test_db, brevo):
    async with _client() as c:
        _, sid = await _ready(c)
        bad = [
            await _gw(c, sid, "brevo", "subscribe", args=args)
            for args in (
                {"email": "nope"},
                {"first_name": "A"},
                {"email": "a@b.co", "listIds": [1]},
                {"email": "a@b.co", "first_name": "a" * 101},
                {"email": "a@b.co", "last_name": "a" * 101},
            )
        ]
        brevo.rate_limited = True
        limited = await _sub(c, sid, email="a@b.co")
    for resp in bad:
        assert resp.status_code == 422 and _code(resp) == "invalid_args", resp.text
    assert limited.status_code == 429 and _code(limited) == "rate_limited"
    assert brevo.contacts == {}


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_a_dead_refresh_needs_reconnect(beanie_test_db, brevo):
    async with _client() as c:
        _, sid = await _ready(c)
        brevo.valid.clear()  # access token revoked early
        ok = await _sub(c, sid, email="a@b.co")
        brevo.valid.clear()
        brevo.refresh_tokens.clear()  # 30 days unused, or the app was removed
        dead = await _sub(c, sid, email="b@b.co")
    assert ok.json()["result"] == {"status": "ok"}, ok.text
    grants = [f["grant_type"] for f in brevo.token_calls]
    assert grants == ["authorization_code", "refresh_token", "refresh_token"]
    assert brevo.token_calls[1]["refresh_token"] == "b-refresh-1"
    assert dead.status_code == 409 and _code(dead) == "not_connected", dead.text
    assert (await _conn()).status == "needs_reconnect"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("jwt_scope", "active"),
    [
        ("contacts:read contacts:write account:read", True),
        ("contacts:read account:read", False),
        (None, False),  # no scope anywhere: fail closed
    ],
)
async def test_a_grant_without_a_scope_field_is_checked_from_the_jwt(
    beanie_test_db, brevo, jwt_scope, active
):
    brevo.scope = ""
    brevo.jwt_scope = jwt_scope
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid)
    assert (msg["status"] == "active") is active, msg
    conn = await _conn()
    if active:
        assert set(conn.scopes) == {"contacts:read", "contacts:write", "account:read"}
    else:
        assert conn is None and msg["error"] == "provider_error"


@pytest.mark.asyncio
async def test_bad_names_answer_ok_for_new_and_existing_alike(beanie_test_db, brevo):
    async with _client() as c:
        _, sid = await _ready(c)
        brevo.contacts["in@example.com"] = {
            "email": "in@example.com",
            "emailBlacklisted": False,
            "attributes": {},
            "listIds": [LIST],
        }
        brevo.contacts["out@example.com"] = {
            "email": "out@example.com",
            "emailBlacklisted": False,
            "attributes": {},
            "listIds": [],
        }
        # Duplicate first, then the PUT refuses names the account doesn't have.
        brevo.attributes = {"FIRSTNAME", "LASTNAME"}
        new = await _sub(c, sid, email="new@example.com", first_name="N")
        brevo.attributes = set()
        dup_in = await _sub(c, sid, email="in@example.com", last_name="I")
        dup_out = await _sub(c, sid, email="out@example.com", last_name="O")
    for resp in (new, dup_in, dup_out):
        assert resp.json() == {"ok": True, "result": {"status": "ok"}}, resp.text
    assert brevo.contacts["out@example.com"]["listIds"] == [LIST]
    assert brevo.contacts["in@example.com"]["attributes"] == {}
    puts = [w[3] for w in brevo.writes() if w[0] == "PUT"]
    assert puts == [
        {"attributes": {"LASTNAME": "I"}},
        {"listIds": [LIST], "attributes": {"LASTNAME": "O"}},
        {"listIds": [LIST]},
    ]
