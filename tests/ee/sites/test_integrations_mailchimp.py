# tests/ee/sites/test_integrations_mailchimp.py: the mailchimp provider.
#
# Runs end to end through the real routes (connect -> /start -> Mailchimp ->
# callback -> audience pick -> public gateway) against an in-memory fake of
# Mailchimp's login server and Marketing API v3, swapped in as
# ``oauth_http._transport``. No network. Covers: the metadata dc / api_endpoint
# check (a mismatched endpoint is refused, no token sent there), no-expiry tokens
# and refresh -> InvalidGrant (a 401 is needs_reconnect), the audiences remote
# select, subscribe (always answering ok) new (pending vs subscribed), fill-empty merge
# fields, unsubscribed / cleaned / archived never re-subscribed (also when added
# after the read: create-only POST), bad names and history 400s answering ok, a transactional
# contact opting in, the audience missing, 400 / 429 mapping and arg caps.
from __future__ import annotations

import hashlib
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

MC_ID = "mc-client"
LIST = "a1b2c3d4e5"


def _hash(email: str) -> str:
    return hashlib.md5(email.lower().encode()).hexdigest()  # noqa: S324 - Mailchimp's id


class FakeMailchimp:
    def __init__(self) -> None:
        self.dc = "us6"
        self.api_endpoint = "https://us6.api.mailchimp.com"
        self.lists = {LIST: "Newsletter", "f0f0f0f0f0": "Customers"}
        self.members: dict[tuple[str, str], dict] = {}
        self.valid: set[str] = set()
        self.issued = 0
        self.token_calls: list[dict] = []
        self.meta_auth: list[str] = []
        self.api_calls: list[tuple[str, str, str, dict]] = []
        self.rate_limited = False
        self.forgotten: set[str] = set()
        self.no_names = False  # the audience has no FNAME / LNAME merge fields
        self.compliance: set[str] = set()
        self.fake_looking: set[str] = set()
        self.race: dict | None = None  # a member created between our GET and POST

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "login.mailchimp.com":
            return self._login(request)
        assert host == f"{self.dc}.api.mailchimp.com", host
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or token not in self.valid:
            return httpx.Response(401, json={"title": "API Key Invalid", "status": 401})
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.api_calls.append((request.method, path, dict(request.url.params), body))
        if self.rate_limited:
            return httpx.Response(429, json={"title": "Too Many Requests", "status": 429})
        if path == "/3.0/lists":
            assert request.url.params["fields"] == "lists.id,lists.name"
            lists = [{"id": k, "name": v} for k, v in self.lists.items()]
            return httpx.Response(200, json={"lists": lists})
        parts = path.split("/")  # '', '3.0', 'lists', id, 'members'[, hash]
        list_id = parts[3]
        member = parts[5] if len(parts) > 5 else _hash(body.get("email_address", ""))
        if list_id not in self.lists:
            return httpx.Response(404, json={"title": "Resource Not Found", "status": 404})
        key = (list_id, member)
        stored = self.members.get(key)
        if request.method == "GET":
            if stored is None:
                return httpx.Response(404, json={"title": "Resource Not Found", "status": 404})
            return httpx.Response(200, json=stored)
        if "merge_fields" in body and self.no_names:
            return httpx.Response(400, json={"title": "Invalid Resource", "status": 400})
        if request.method == "POST":
            assert len(parts) == 5, path  # create only: never an upsert
            email = body["email_address"]
            if email in self.forgotten:
                return self._bad("Forgotten Email Not Subscribed")
            if email in self.compliance:
                return self._bad("Member In Compliance State")
            if email in self.fake_looking:
                return self._bad("Invalid Resource")
            if self.race is not None:
                self.members[key] = self.race
                self.race = None
                stored = self.members[key]
            if stored is not None:
                return self._bad("Member Exists")
            stored = {
                "email_address": email,
                "status": body["status"],
                "merge_fields": {"FNAME": "", "LNAME": "", **(body.get("merge_fields") or {})},
            }
            self.members[key] = stored
            return httpx.Response(200, json=stored)
        if request.method == "PATCH":
            stored["merge_fields"].update(body.get("merge_fields") or {})
            if "status" in body:
                stored["status"] = body["status"]
            return httpx.Response(200, json=stored)
        return httpx.Response(405)

    @staticmethod
    def _bad(title: str) -> httpx.Response:
        return httpx.Response(400, json={"title": title, "status": 400})

    def _login(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/metadata":
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            self.meta_auth.append(scheme)
            if scheme != "OAuth" or token not in self.valid:
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(
                200,
                json={
                    "dc": self.dc,
                    "role": "owner",
                    "accountname": "Bakery Co",
                    "user_id": 4242,
                    "login": {"email": "o@bakery.co", "login_id": 99, "login_email": "o@b.co"},
                    "login_url": "https://login.mailchimp.com",
                    "api_endpoint": self.api_endpoint,
                },
            )
        assert request.url.path == "/oauth2/token"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["client_id"] == MC_ID and form["client_secret"] == "mc-secret"
        self.token_calls.append(form)
        if form.get("code") == "used":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        token = f"mc-access-{self.issued}"
        self.valid.add(token)
        return httpx.Response(200, json={"access_token": token, "expires_in": 0, "scope": None})

    def writes(self) -> list[tuple[str, str, str, dict]]:
        return [c for c in self.api_calls if c[0] in ("PUT", "PATCH", "POST")]


@pytest.fixture
def mc(monkeypatch):
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers import oauth_http

    field_options.clear_cache()
    fake = FakeMailchimp()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_MAILCHIMP_CLIENT_ID", MC_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_MAILCHIMP_CLIENT_SECRET", "mc-secret")
    yield fake
    field_options.clear_cache()


async def _connect(c, pid: str, code: str = "c1") -> dict:
    started = await c.post("/api/v1/integrations/mailchimp/connect", json={"pocket_id": pid})
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    cb = await c.get(
        "/api/v1/integrations/mailchimp/callback", params={"code": code, "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text)


def _put_url(pid: str) -> str:
    return f"/api/v1/sites/by-pocket/{pid}/integrations/mailchimp"


async def _ready(c, double_opt_in: str | None = None) -> tuple[str, str]:
    pid = await _pocket()
    site = await _site(pid)
    msg = await _connect(c, pid)
    assert msg["status"] == "active", msg
    settings = {"list_id": LIST}
    if double_opt_in is not None:
        settings["double_opt_in"] = double_opt_in
    put = await c.put(_put_url(pid), json={"settings": settings})
    assert put.status_code == 200, put.text
    return pid, str(site.id)


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.find_one(IntegrationConnection.provider == "mailchimp")


async def _sub(c, sid: str, **args):
    return await _gw(c, sid, "mailchimp", "subscribe", args=args)


# ----------------------------------------------------------------- unit


def test_registry_authorize_url_and_availability(monkeypatch, mc):
    from pocketpaw_ee.integrations.providers import get_provider

    provider = get_provider("mailchimp")
    assert (provider.kind, provider.category, provider.plan) == ("connect", "email", "site")
    assert provider.supports_pkce is False and set(provider.actions) == {"subscribe"}
    assert provider.actions["subscribe"].public and provider.actions["subscribe"].write
    fields = {f.key: f for f in provider.settings_fields}
    assert fields["list_id"].options_source == "remote" and fields["list_id"].required
    assert [o.value for o in fields["double_opt_in"].options] == ["yes", "no"]
    split = urlsplit(provider.authorize_url("st-1", "https://connect.example.com/cb"))
    assert (split.netloc, split.path) == ("login.mailchimp.com", "/oauth2/authorize")
    assert {k: v[0] for k, v in parse_qs(split.query).items()} == {
        "response_type": "code",
        "client_id": MC_ID,
        "redirect_uri": "https://connect.example.com/cb",
        "state": "st-1",
    }
    assert provider.availability() == "available"
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_MAILCHIMP_CLIENT_SECRET")
    assert provider.availability() == "unconfigured"


@pytest.mark.parametrize(
    ("dc", "endpoint", "expected"),
    [
        ("us6", "https://us6.api.mailchimp.com", "https://us6.api.mailchimp.com/3.0"),
        ("us21", "https://us21.api.mailchimp.com/", "https://us21.api.mailchimp.com/3.0"),
        ("us6", "https://us7.api.mailchimp.com", None),
        ("us6", "http://us6.api.mailchimp.com", None),
        ("us6", "https://us6.api.mailchimp.com.evil.io", None),
        ("us6", "https://evil.io", None),
        ("us6", "https://us6.api.mailchimp.com:8443", None),
        ("us6", "https://us6.api.mailchimp.com/3.0", None),
        ("us6.evil", "https://us6.evil.api.mailchimp.com", None),
        ("US6", "https://US6.api.mailchimp.com", None),
        ("", "https://.api.mailchimp.com", None),
        ("us6", "", None),
    ],
)
def test_api_base_requires_a_matching_data_center(dc, endpoint, expected):
    from pocketpaw_ee.integrations.providers.mailchimp import api_base

    assert api_base({"dc": dc, "api_endpoint": endpoint}) == expected


@pytest.mark.asyncio
async def test_refresh_is_always_a_dead_grant(mc):
    from pocketpaw_ee.integrations.providers.base import InvalidGrant
    from pocketpaw_ee.integrations.providers.mailchimp import MailchimpProvider

    with pytest.raises(InvalidGrant):
        await MailchimpProvider().refresh("")
    assert mc.token_calls == []


# ------------------------------------------------------------- connect flow


@pytest.mark.asyncio
async def test_connect_stores_dc_and_lists_audiences(beanie_test_db, mc):
    async with _client() as c:
        pid = await _pocket()
        await _site(pid)
        msg = await _connect(c, pid)
        options = await c.get(f"{_put_url(pid)}/fields/list_id/options")
        bad = await c.put(_put_url(pid), json={"settings": {"list_id": "zzzzzz"}})
        ok = await c.put(_put_url(pid), json={"settings": {"list_id": LIST}})
    assert msg["status"] == "active", msg
    assert mc.meta_auth == ["OAuth"]
    conn = await _conn()
    assert conn.account_ref == "4242" and conn.account_label == "Bakery Co"
    assert conn.meta == {"dc": "us6", "api_endpoint": "https://us6.api.mailchimp.com"}
    assert conn.expires_at is None  # Mailchimp tokens don't expire
    assert options.status_code == 200, options.text
    body = options.json()
    listed = body["options"] if isinstance(body, dict) else body
    assert [(o["value"], o["label"]) for o in listed] == [
        (LIST, "Newsletter"),
        ("f0f0f0f0f0", "Customers"),
    ]
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_settings")
    assert ok.status_code == 200 and ok.json()["settings"]["list_id"] == LIST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["https://evil.io", "https://us6.api.mailchimp.com.evil.io", "https://us7.api.mailchimp.com"],
)
async def test_a_mismatched_api_endpoint_refuses_the_connect(beanie_test_db, mc, endpoint):
    mc.api_endpoint = endpoint
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid)
    assert msg["status"] == "error" and msg["error"] == "provider_error"
    assert mc.api_calls == [] and await _conn() is None


@pytest.mark.asyncio
async def test_used_code_fails_the_callback(beanie_test_db, mc):
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid, code="used")
    assert msg["status"] == "error" and await _conn() is None


# ---------------------------------------------------------------- subscribe


@pytest.mark.asyncio
async def test_new_member_is_pending_by_default_and_subscribed_without_double_opt_in(
    beanie_test_db, mc
):
    async with _client() as c:
        pid, sid = await _ready(c)
        pending = await _sub(c, sid, email=" Ada@Example.com ", first_name="Ada\n")
        await c.put(_put_url(pid), json={"settings": {"double_opt_in": "no"}})
        direct = await _sub(c, sid, email="bob@example.com")
    assert pending.json() == {"ok": True, "result": {"status": "ok"}}, pending.text
    assert direct.json()["result"] == {"status": "ok"}, direct.text
    ada = mc.members[(LIST, _hash("ada@example.com"))]
    assert ada["status"] == "pending" and ada["merge_fields"] == {"FNAME": "Ada", "LNAME": ""}
    assert mc.members[(LIST, _hash("bob@example.com"))]["status"] == "subscribed"
    puts = [w for w in mc.writes() if w[0] == "POST"]
    assert puts[0][1] == f"/3.0/lists/{LIST}/members"
    assert puts[0][3] == {
        "email_address": "ada@example.com",
        "status": "pending",
        "merge_fields": {"FNAME": "Ada"},
    }
    assert "merge_fields" not in puts[1][3]  # no names given, none sent


@pytest.mark.asyncio
async def test_existing_member_gets_empty_merge_fields_only(beanie_test_db, mc):
    mc.members[(LIST, _hash("ada@example.com"))] = {
        "status": "subscribed",
        "merge_fields": {"FNAME": "Ada", "LNAME": ""},
    }
    async with _client() as c:
        _, sid = await _ready(c)
        filled = await _sub(c, sid, email="ada@example.com", first_name="Eve", last_name="Lace")
        same = await _sub(c, sid, email="ada@example.com", first_name="Mallory", last_name="X")
    assert filled.json()["result"] == {"status": "ok"}, filled.text
    assert same.json()["result"] == {"status": "ok"}, same.text
    member = mc.members[(LIST, _hash("ada@example.com"))]
    assert member == {"status": "subscribed", "merge_fields": {"FNAME": "Ada", "LNAME": "Lace"}}
    [patch] = mc.writes()
    assert patch[0] == "PATCH" and patch[3] == {"merge_fields": {"LNAME": "Lace"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unsubscribed", "cleaned", "archived"])
async def test_opted_out_members_are_never_resubscribed(beanie_test_db, mc, status):
    mc.members[(LIST, _hash("gone@example.com"))] = {
        "status": status,
        "merge_fields": {"FNAME": "", "LNAME": ""},
    }
    async with _client() as c:
        _, sid = await _ready(c, double_opt_in="no")
        resp = await _sub(c, sid, email="gone@example.com", first_name="Gone")
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    assert mc.writes() == []
    assert mc.members[(LIST, _hash("gone@example.com"))]["status"] == status


@pytest.mark.asyncio
async def test_a_transactional_contact_opts_in(beanie_test_db, mc):
    mc.members[(LIST, _hash("t@example.com"))] = {
        "status": "transactional",
        "merge_fields": {"FNAME": "", "LNAME": "Stored"},
    }
    async with _client() as c:
        _, sid = await _ready(c)
        resp = await _sub(c, sid, email="t@example.com", first_name="Tia", last_name="New")
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    [patch] = mc.writes()
    assert patch[3] == {"merge_fields": {"FNAME": "Tia"}, "status": "pending"}


@pytest.mark.asyncio
async def test_an_audience_without_name_fields_still_subscribes(beanie_test_db, mc):
    mc.no_names = True
    async with _client() as c:
        _, sid = await _ready(c)
        resp = await _sub(c, sid, email="a@b.co", first_name="A")
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    assert mc.members[(LIST, _hash("a@b.co"))]["status"] == "pending"


@pytest.mark.asyncio
async def test_no_audience_or_a_deleted_one_is_setup_required(beanie_test_db, mc):
    async with _client() as c:
        pid = await _pocket()
        site = await _site(pid)
        await _connect(c, pid)
        unpicked = await _sub(c, str(site.id), email="a@b.co")
        await c.put(_put_url(pid), json={"settings": {"list_id": LIST}})
        del mc.lists[LIST]
        deleted = await _sub(c, str(site.id), email="a@b.co")
    for resp in (unpicked, deleted):
        assert resp.status_code == 409 and _code(resp) == "setup_required", resp.text
    assert mc.members == {}


@pytest.mark.asyncio
async def test_validation_quiet_history_400s_and_rate_limit(beanie_test_db, mc):
    mc.forgotten.add("gdpr@example.com")
    mc.compliance.add("held@example.com")
    mc.fake_looking.add("fake@example.com")
    async with _client() as c:
        _, sid = await _ready(c)
        bad = [
            await _gw(c, sid, "mailchimp", "subscribe", args=args)
            for args in (
                {"email": "nope"},
                {"first_name": "A"},
                {"email": "a@b.co", "status": "subscribed"},
                {"email": "a@b.co", "first_name": "a" * 101},
                {"email": "a@b.co", "last_name": "a" * 101},
                {"email": ("a" * 250) + "@b.co"},
            )
        ]
        forgotten = await _sub(c, sid, email="gdpr@example.com", first_name="G")
        held = await _sub(c, sid, email="held@example.com")
        fake = await _sub(c, sid, email="fake@example.com")
        mc.rate_limited = True
        limited = await _sub(c, sid, email="a@b.co")
    for resp in (forgotten, held):
        # An address's history on the audience answers like a new signup.
        assert resp.json() == {"ok": True, "result": {"status": "ok"}}, resp.text
    for resp in [*bad, fake]:
        assert resp.status_code == 422 and _code(resp) == "invalid_args", resp.text
    assert "Invalid Resource" not in fake.text  # generic message only
    assert limited.status_code == 429 and _code(limited) == "rate_limited"
    assert mc.members == {}


@pytest.mark.asyncio
async def test_a_revoked_token_needs_reconnect(beanie_test_db, mc):
    async with _client() as c:
        _, sid = await _ready(c)
        mc.valid.clear()  # the owner removed the app in Mailchimp
        resp = await _sub(c, sid, email="a@b.co")
    assert resp.status_code == 409 and _code(resp) == "not_connected", resp.text
    assert (await _conn()).status == "needs_reconnect"
    assert mc.token_calls and all(f["grant_type"] == "authorization_code" for f in mc.token_calls)


@pytest.mark.asyncio
async def test_bad_names_answer_ok_for_new_and_existing_alike(beanie_test_db, mc):
    mc.no_names = True
    mc.members[(LIST, _hash("old@example.com"))] = {
        "status": "subscribed",
        "merge_fields": {"FNAME": "", "LNAME": ""},
    }
    mc.members[(LIST, _hash("t@example.com"))] = {
        "status": "transactional",
        "merge_fields": {"FNAME": "", "LNAME": ""},
    }
    async with _client() as c:
        _, sid = await _ready(c)
        new = await _sub(c, sid, email="new@example.com", first_name="N")
        old = await _sub(c, sid, email="old@example.com", first_name="O")
        tx = await _sub(c, sid, email="t@example.com", first_name="T")
    for resp in (new, old, tx):
        assert resp.json() == {"ok": True, "result": {"status": "ok"}}, resp.text
    assert mc.members[(LIST, _hash("new@example.com"))]["status"] == "pending"
    assert mc.members[(LIST, _hash("t@example.com"))]["status"] == "pending"
    patches = [w[3] for w in mc.writes() if w[0] == "PATCH"]
    # old: one refused PATCH, nothing retried; transactional: retried with status only.
    assert patches == [
        {"merge_fields": {"FNAME": "O"}},
        {"merge_fields": {"FNAME": "T"}, "status": "pending"},
        {"status": "pending"},
    ]


@pytest.mark.asyncio
async def test_a_member_added_after_the_read_is_filled_never_overwritten(beanie_test_db, mc):
    mc.race = {"status": "subscribed", "merge_fields": {"FNAME": "Ada", "LNAME": ""}}
    async with _client() as c:
        _, sid = await _ready(c)
        resp = await _sub(c, sid, email="ada@example.com", first_name="Eve", last_name="Lace")
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    member = mc.members[(LIST, _hash("ada@example.com"))]
    assert member == {"status": "subscribed", "merge_fields": {"FNAME": "Ada", "LNAME": "Lace"}}
    assert [w[0] for w in mc.writes()] == ["POST", "PATCH"]


@pytest.mark.asyncio
async def test_an_opted_out_member_added_after_the_read_stays_out(beanie_test_db, mc):
    mc.race = {"status": "unsubscribed", "merge_fields": {"FNAME": "", "LNAME": ""}}
    async with _client() as c:
        _, sid = await _ready(c, double_opt_in="no")
        resp = await _sub(c, sid, email="gone@example.com", first_name="G")
    assert resp.json()["result"] == {"status": "ok"}, resp.text
    assert mc.members[(LIST, _hash("gone@example.com"))]["status"] == "unsubscribed"
    assert [w[0] for w in mc.writes()] == ["POST"]
