# tests/ee/sites/test_integrations_sheets_hubspot.py: the google_sheets + hubspot providers.
#
# Both run end to end through the real routes (connect -> /start -> provider page
# -> callback -> site link -> site_setup -> public gateway) against an in-memory
# fake of Google's and HubSpot's HTTP APIs, swapped in as ``oauth_http._transport``
# (an ``httpx.MockTransport``). No network anywhere.
#
# google_sheets: authorize URL params incl. PKCE, scope refusal, sheet creation,
# idempotent setup + a missing tab, append_row header growth / caps / RAW / control
# characters, a 401 refreshing once, 403 -> setup_required, invalid_grant ->
# needs_reconnect. hubspot: authorize params, token-info identity, create, the 409
# upsert path, validation, 429 -> rate_limited.
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    WS,
    _client,
    _code,
    _env,
    _gw,
    _message,
    _pocket,
    _site,
)

CLIENT_ID = "google-client-id"
HUBSPOT_ID = "hubspot-client-id"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"


def _s256(verifier: str) -> str:
    import hashlib

    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeGoogle:
    """Google's token endpoint + the slice of the Sheets API the provider uses."""

    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}
        self.scope = f"openid {DRIVE_FILE} https://www.googleapis.com/auth/userinfo.email"
        self.valid: set[str] = set()
        self.issued = 0
        self.refreshes = 0
        self.dead_refresh: set[str] = set()
        self.sheets: dict[str, dict] = {}
        # A canned Sheets error answer (status code, error body) for every API call.
        self.fail: tuple[int, dict] | None = None
        self.puts: list[tuple[str, list]] = []
        self.creates = 0
        self.calls: list[tuple[str, str, dict]] = []

    def _token(self) -> dict:
        self.issued += 1
        token = f"g-access-{self.issued}"
        self.valid.add(token)
        return {"access_token": token, "expires_in": 3599, "token_type": "Bearer"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "oauth2.googleapis.com":
            return self._token_endpoint(request)
        assert url.host == "sheets.googleapis.com", url
        if request.headers.get("authorization", "")[7:] not in self.valid:
            return httpx.Response(401, json={"error": {"code": 401}})
        if self.fail is not None:
            return httpx.Response(self.fail[0], json={"error": self.fail[1]})
        path = unquote(url.raw_path.decode().split("?")[0]).removeprefix("/v4/spreadsheets")
        params = dict(url.params)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, params))
        if path == "" and request.method == "POST":
            self.creates += 1
            sid = f"sheet{self.creates:08d}xyz"
            self.sheets[sid] = {
                "title": body["properties"]["title"],
                "tabs": {s["properties"]["title"]: [] for s in body["sheets"]},
            }
            return httpx.Response(200, json={"spreadsheetId": sid, "spreadsheetUrl": "x"})
        sid, _, rest = path.lstrip("/").partition("/")
        sid, _, verb = sid.partition(":")
        sheet = self.sheets.get(sid)
        if sheet is None:
            return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND"}})
        if verb == "batchUpdate":
            for req in body["requests"]:
                sheet["tabs"][req["addSheet"]["properties"]["title"]] = []
            return httpx.Response(200, json={})
        if not rest:
            props = [{"properties": {"title": t}} for t in sheet["tabs"]]
            return httpx.Response(200, json={"spreadsheetId": sid, "sheets": props})
        rng = rest.removeprefix("values/")
        rng, append = (rng[: -len(":append")], True) if rng.endswith(":append") else (rng, False)
        tab_quoted, _, cells = rng.rpartition("!")
        tab = tab_quoted[1:-1].replace("''", "'")
        if tab not in sheet["tabs"]:
            return httpx.Response(400, json={"error": {"message": f"Unable to parse range: {rng}"}})
        rows = sheet["tabs"][tab]
        if request.method == "GET":
            return httpx.Response(200, json={"values": rows[:1]} if rows else {})
        assert params.get("valueInputOption") == "RAW"
        if append:
            assert params.get("insertDataOption") == "INSERT_ROWS"
            rows.extend(body["values"])
        else:
            # A single-row write at <col>1: only those cells change.
            col = 0
            for ch in cells.rstrip("0123456789"):
                col = col * 26 + ord(ch) - 64
            if not rows:
                rows.append([])
            header = rows[0]
            new = body["values"][0]
            self.puts.append((cells, list(new)))
            header.extend([""] * max(0, col - 1 + len(new) - len(header)))
            header[col - 1 : col - 1 + len(new)] = new
        return httpx.Response(200, json={})

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["client_id"] == CLIENT_ID and form["client_secret"] == "google-secret"
        if form["grant_type"] == "refresh_token":
            self.refreshes += 1
            if form["refresh_token"] in self.dead_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._token())
        challenge = self.challenges.get(form["code"])
        if not challenge or _s256(form.get("code_verifier", "")) != challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "1234567890",
            "email": "owner@example.com",
        }
        return httpx.Response(
            200,
            json={
                **self._token(),
                "refresh_token": "g-refresh",
                "scope": self.scope,
                "id_token": _jwt(claims),
            },
        )


class FakeHubSpot:
    def __init__(self) -> None:
        self.contacts: dict[str, dict] = {}
        self.valid: set[str] = set()
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.rate_limited = False
        self.scopes = ["oauth", "crm.objects.contacts.read", "crm.objects.contacts.write"]
        # path -> whether OTel httpx instrumentation was live for that request.
        self.traced: dict[str, bool] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        from opentelemetry.instrumentation.utils import is_http_instrumentation_enabled

        path = request.url.path
        self.traced[path.rsplit("/", 1)[0]] = is_http_instrumentation_enabled()
        if path == "/oauth/v1/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("refresh_token") == "BAD":
                return httpx.Response(400, json={"status": "BAD_REFRESH_TOKEN"})
            self.valid.add("hs-access")
            return httpx.Response(
                200,
                json={
                    "access_token": "hs-access",
                    "refresh_token": "hs-refresh",
                    "expires_in": 1800,
                },
            )
        if path.startswith("/oauth/v1/access-tokens/"):
            return httpx.Response(
                200,
                json={
                    "hub_id": 4242,
                    "hub_domain": "acme.hubspot.com",
                    "user": "owner@acme.com",
                    "scopes": self.scopes,
                },
            )
        if request.headers.get("authorization", "")[7:] not in self.valid:
            return httpx.Response(401, json={})
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, dict(request.url.params), body))
        if self.rate_limited:
            return httpx.Response(429, json={"status": "error"})
        props = body.get("properties", {})
        if request.method == "POST" and path == "/crm/v3/objects/contacts":
            if props["email"] in self.contacts:
                return httpx.Response(409, json={"status": "error", "category": "CONFLICT"})
            self.contacts[props["email"]] = dict(props)
            return httpx.Response(201, json={"id": "1"})
        if request.method == "GET":
            email = unquote(path.rsplit("/", 1)[1])
            assert request.url.params["idProperty"] == "email"
            wanted = request.url.params["properties"].split(",")
            stored = self.contacts[email]
            return httpx.Response(200, json={"properties": {k: stored.get(k) for k in wanted}})
        if request.method == "PATCH":
            email = unquote(path.rsplit("/", 1)[1])
            self.contacts[email].update(props)
            return httpx.Response(200, json={"id": "1"})
        return httpx.Response(404)


@pytest.fixture
def fakes(monkeypatch):
    from pocketpaw_ee.integrations.providers import oauth_http

    google, hubspot = FakeGoogle(), FakeHubSpot()

    def _route(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("hubapi.com"):
            return hubspot.handle(request)
        return google.handle(request)

    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(_route))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_GOOGLE_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_HUBSPOT_CLIENT_ID", HUBSPOT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_HUBSPOT_CLIENT_SECRET", "hubspot-secret")
    return google, hubspot


def _sites_url(pid: str, provider: str = "") -> str:
    base = f"/api/v1/sites/by-pocket/{pid}/integrations"
    return f"{base}/{provider}" if provider else base


async def _connect(c, provider: str, pid: str, fake_google: FakeGoogle | None = None) -> dict:
    """connect -> /start -> (provider page) -> callback. Returns the postMessage."""
    started = await c.post(f"/api/v1/integrations/{provider}/connect", json={"pocket_id": pid})
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    code = f"code-{provider}"
    if fake_google is not None:
        fake_google.challenges[code] = query["code_challenge"]
    cb = await c.get(
        f"/api/v1/integrations/{provider}/callback", params={"code": code, "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text)


async def _sheets_site(c, google: FakeGoogle):
    pid = await _pocket()
    site = await _site(pid)
    msg = await _connect(c, "google_sheets", pid, google)
    assert msg["status"] == "active", msg
    return pid, str(site.id)


async def _binding(pid: str, provider: str):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    return await SiteIntegration.find_one(
        SiteIntegration.pocket_id == pid, SiteIntegration.provider == provider
    )


# ------------------------------------------------------------ google_sheets


def test_sheets_is_unconfigured_without_env_and_builds_a_pkce_url(monkeypatch, fakes):
    from pocketpaw_ee.integrations.providers.google_sheets import GoogleSheetsProvider

    provider = GoogleSheetsProvider()
    assert provider.availability() == "available"
    url = provider.authorize_url("st-1", "https://connect.example.com/cb", code_challenge="ch-1")
    split = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(split.query).items()}
    assert f"{split.scheme}://{split.netloc}{split.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert q == {
        "client_id": CLIENT_ID,
        "redirect_uri": "https://connect.example.com/cb",
        "response_type": "code",
        "scope": f"{DRIVE_FILE} openid email",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": "st-1",
        "code_challenge": "ch-1",
        "code_challenge_method": "S256",
    }
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_GOOGLE_CLIENT_SECRET")
    assert provider.availability() == "unconfigured"


@pytest.mark.asyncio
async def test_sheets_connect_creates_the_sheet_and_hides_its_id(beanie_test_db, fakes):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    google, _ = fakes
    async with _client() as c:
        pid, _ = await _sheets_site(c, google)
        [view] = (await c.get(_sites_url(pid))).json()
    conn = await IntegrationConnection.find_one(IntegrationConnection.provider == "google_sheets")
    assert conn.account_ref == "1234567890" and conn.account_label == "owner@example.com"
    assert DRIVE_FILE in conn.scopes
    [(sid, sheet)] = google.sheets.items()
    assert sheet == {"title": "Shop — responses", "tabs": {"Responses": []}}
    assert view["status"] == "connected"
    assert view["settings"] == {
        "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    }
    assert view["enabled_actions"] == ["append_row"]


@pytest.mark.asyncio
async def test_sheets_refuses_a_grant_without_drive_file(beanie_test_db, fakes):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    google, _ = fakes
    google.scope = "openid email"
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, "google_sheets", pid, google)
    assert msg["status"] == "error" and msg["error"] == "provider_error"
    assert (
        await IntegrationConnection.find_one(IntegrationConnection.provider == "google_sheets")
        is None
    )


@pytest.mark.asyncio
async def test_sheets_setup_is_idempotent_adds_a_missing_tab_and_replaces_a_lost_sheet(
    beanie_test_db, fakes
):
    google, _ = fakes
    async with _client() as c:
        pid, _ = await _sheets_site(c, google)
        [first_id] = google.sheets
        renamed = await c.put(
            _sites_url(pid, "google_sheets"), json={"settings": {"sheet_name": "Leads"}}
        )
        assert renamed.status_code == 200, renamed.text
        assert google.creates == 1
        assert set(google.sheets[first_id]["tabs"]) == {"Responses", "Leads"}
        # Rerun with the tab present: nothing new.
        await c.put(
            _sites_url(pid, "google_sheets"), json={"settings": {"sheet_name": "Responses"}}
        )
        assert google.creates == 1 and len(google.sheets[first_id]["tabs"]) == 2
        # The owner deleted the spreadsheet: the next setup makes a fresh one.
        del google.sheets[first_id]
        again = await c.put(
            _sites_url(pid, "google_sheets"), json={"settings": {"sheet_name": "Leads"}}
        )
    assert google.creates == 2
    [second_id] = google.sheets
    assert google.sheets[second_id]["tabs"] == {"Leads": []}
    assert again.json()["status"] == "connected"
    assert second_id in again.json()["settings"]["spreadsheet_url"]


@pytest.mark.asyncio
async def test_append_row_grows_the_header_writes_raw_and_strips_controls(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)
        one = await _gw(
            c,
            sid,
            "google_sheets",
            "append_row",
            args={"fields": {"name": "Ada\x07", "email": "ada@example.com", "submitted_at": "x"}},
        )
        two = await _gw(
            c,
            sid,
            "google_sheets",
            "append_row",
            args={"fields": {"email": "bob@example.com", "note": '=HYPERLINK("x")\r\nline 2'}},
        )
    assert one.json() == {"ok": True, "result": {"appended": True}}, one.text
    assert two.json()["ok"] is True, two.text
    [sheet] = google.sheets.values()
    rows = sheet["tabs"]["Responses"]
    assert rows[0] == ["submitted_at", "name", "email", "note"]
    assert rows[1][1:] == ["Ada", "ada@example.com"]
    assert rows[1][0].endswith("Z") and rows[1][0] != "x"
    assert rows[2][1:] == ["", "bob@example.com", '=HYPERLINK("x")\nline 2']
    appends = [p for m, path, p in google.calls if path.endswith(":append")]
    assert appends and all(p["valueInputOption"] == "RAW" for p in appends)


@pytest.mark.asyncio
async def test_append_row_validates_and_normalises_keys(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)

        async def _send(fields):
            return await _gw(c, sid, "google_sheets", "append_row", args={"fields": fields})

        too_many = await _send({f"k{i}": "v" for i in range(51)})
        long_key = await _send({"k" * 81: "v"})
        long_value = await _send({"k": "v" * 2001})
        not_text = await _send({"k": 3})
        punctuation = await _send({"?!-": "v"})
        spaced = await _send({"  First \t\n  name ": "Ada"})
    for resp in (too_many, long_key, long_value, not_text, punctuation):
        assert resp.status_code == 422 and _code(resp) == "invalid_args", resp.text
    assert spaced.json()["ok"] is True, spaced.text
    [sheet] = google.sheets.values()
    assert sheet["tabs"]["Responses"][0] == ["submitted_at", "First name"]


@pytest.mark.asyncio
async def test_new_keys_are_budgeted_and_known_keys_keep_working(beanie_test_db, fakes):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)

        async def _send(fields):
            return await _gw(c, sid, "google_sheets", "append_row", args={"fields": fields})

        first = await _send({f"k{i}": str(i) for i in range(7)})  # 5 per request
        second = await _send({f"k{i}": str(i) for i in range(5, 11)})  # 5 left today
        known = await _send({"k0": "again", "late": "dropped"})  # budget spent
        only_new = await _send({"late": "x"})
        row = await _binding(pid, "google_sheets")
        await SiteIntegration.get_pymongo_collection().update_one(
            {"_id": row.id},
            {"$set": {"provider_state.new_keys": {"day": "2000-01-01", "used": 10}}},
        )
        next_day = await _send({"late": "y"})
    for resp in (first, second, known, next_day):
        assert resp.json()["ok"] is True, resp.text
    assert only_new.status_code == 422 and _code(only_new) == "invalid_args"
    [sheet] = google.sheets.values()
    rows = sheet["tabs"]["Responses"]
    keys = [f"k{i}" for i in range(10)]
    assert rows[0] == ["submitted_at", *keys, "late"]
    assert rows[1][1:6] == ["0", "1", "2", "3", "4"]
    assert rows[2][6:11] == ["5", "6", "7", "8", "9"]
    assert rows[3][1] == "again" and "dropped" not in rows[3]
    assert rows[4][-1] == "y"


@pytest.mark.asyncio
async def test_header_growth_stops_at_sixty_columns(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)
        [sheet] = google.sheets.values()
        sheet["tabs"]["Responses"].append(["submitted_at", *[f"c{i}" for i in range(58)]])
        resp = await _gw(
            c, sid, "google_sheets", "append_row", args={"fields": {"x": "1", "y": "2", "c0": "3"}}
        )
    assert resp.json()["ok"] is True, resp.text
    header = sheet["tabs"]["Responses"][0]
    assert len(header) == 60 and header[-1] == "x" and "y" not in header


@pytest.mark.asyncio
async def test_concurrent_forms_never_clobber_the_header(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)
        one, two = await asyncio.gather(
            _gw(c, sid, "google_sheets", "append_row", args={"fields": {"a": "1", "b": "2"}}),
            _gw(c, sid, "google_sheets", "append_row", args={"fields": {"c": "3", "d": "4"}}),
        )
    assert one.json()["ok"] is True and two.json()["ok"] is True
    [sheet] = google.sheets.values()
    header, *rows = sheet["tabs"]["Responses"]
    assert sorted(header) == ["a", "b", "c", "d", "submitted_at"] and header[0] == "submitted_at"
    by_col = [dict(zip(header, r, strict=False)) for r in rows]
    assert any(r.get("a") == "1" and r.get("b") == "2" for r in by_col)
    assert any(r.get("c") == "3" and r.get("d") == "4" for r in by_col)
    # Only new cells were ever written: exactly one write started at A1.
    assert [cells for cells, _ in google.puts].count("A1") == 1


@pytest.mark.asyncio
async def test_append_row_refreshes_once_on_401(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)
        google.valid.clear()  # Google revoked the access token early
        resp = await _gw(c, sid, "google_sheets", "append_row", args={"fields": {"a": "1"}})
    assert resp.json()["ok"] is True, resp.text
    assert google.refreshes == 1


@pytest.mark.asyncio
async def test_google_errors_map_by_status_and_a_good_append_clears_setup(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)

        async def _send():
            return await _gw(c, sid, "google_sheets", "append_row", args={"fields": {"a": "1"}})

        google.fail = (403, {"code": 403, "status": "PERMISSION_DENIED"})
        denied = await _send()
        [view] = (await c.get(_sites_url(pid))).json()
        google.fail = (
            403,
            {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "errors": [{"reason": "rateLimitExceeded"}],
            },
        )
        quota = await _send()
        google.fail = (429, {"code": 429, "status": "RESOURCE_EXHAUSTED"})
        exhausted = await _send()
        google.fail = (500, {"code": 500, "status": "INTERNAL"})
        internal = await _send()
        google.fail = None
        ok = await _send()
        [healed] = (await c.get(_sites_url(pid))).json()
    assert denied.status_code == 409 and _code(denied) == "setup_required"
    assert view["status"] == "setup_required"
    assert quota.status_code == 429 and _code(quota) == "rate_limited"
    assert exhausted.status_code == 429 and _code(exhausted) == "rate_limited"
    assert internal.status_code == 502 and _code(internal) == "provider_error"
    assert ok.json()["ok"] is True
    assert (await _binding(pid, "google_sheets")).setup_error == ""
    assert healed["status"] == "connected"


@pytest.mark.asyncio
async def test_a_missing_tab_is_setup_required_and_setup_adds_it_back(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)
        [sheet] = google.sheets.values()
        sheet["tabs"].clear()
        gone_tab = await _gw(c, sid, "google_sheets", "append_row", args={"fields": {"a": "1"}})
        fixed = await c.put(
            _sites_url(pid, "google_sheets"), json={"settings": {"sheet_name": "Responses"}}
        )
    assert gone_tab.status_code == 409 and _code(gone_tab) == "setup_required"
    assert fixed.json()["status"] == "connected" and "Responses" in sheet["tabs"]
    assert google.creates == 1


@pytest.mark.asyncio
async def test_site_setup_never_creates_a_second_sheet_on_403(beanie_test_db, fakes):
    google, _ = fakes
    async with _client() as c:
        pid, _ = await _sheets_site(c, google)
        google.fail = (403, {"code": 403, "status": "PERMISSION_DENIED"})
        rerun = await c.put(
            _sites_url(pid, "google_sheets"), json={"settings": {"sheet_name": "Leads"}}
        )
    assert google.creates == 1
    assert rerun.json()["status"] == "setup_required"


@pytest.mark.asyncio
async def test_invalid_grant_on_refresh_flips_needs_reconnect(beanie_test_db, fakes):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    google, _ = fakes
    async with _client() as c:
        pid, sid = await _sheets_site(c, google)
        conn = await IntegrationConnection.find_one(
            IntegrationConnection.provider == "google_sheets"
        )
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await conn.save()
        google.dead_refresh.add("g-refresh")
        resp = await _gw(c, sid, "google_sheets", "append_row", args={"fields": {"a": "1"}})
        [view] = (await c.get(_sites_url(pid))).json()
    assert resp.status_code == 409 and _code(resp) == "not_connected"
    assert (await IntegrationConnection.get(conn.id)).status == "needs_reconnect"
    assert view["status"] == "needs_reconnect"


# ------------------------------------------------------------------ hubspot


def test_hubspot_authorize_url_and_availability(monkeypatch, fakes):
    from pocketpaw_ee.integrations.providers.hubspot import HubSpotProvider

    provider = HubSpotProvider()
    split = urlsplit(provider.authorize_url("st-2", "https://connect.example.com/cb"))
    q = {k: v[0] for k, v in parse_qs(split.query).items()}
    assert split.netloc == "app.hubspot.com" and split.path == "/oauth/authorize"
    assert q == {
        "client_id": HUBSPOT_ID,
        "redirect_uri": "https://connect.example.com/cb",
        "scope": "oauth crm.objects.contacts.read crm.objects.contacts.write",
        "state": "st-2",
    }
    assert provider.supports_pkce is False and provider.settings_fields == ()
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_HUBSPOT_CLIENT_ID")
    assert provider.availability() == "unconfigured"


@pytest.mark.asyncio
async def test_hubspot_refresh_maps_bad_refresh_token_to_invalid_grant(fakes):
    from pocketpaw_ee.integrations.providers.base import InvalidGrant
    from pocketpaw_ee.integrations.providers.hubspot import HubSpotProvider

    grant = await HubSpotProvider().refresh("good")
    assert grant.access_token == "hs-access" and grant.expires_in == 1800
    with pytest.raises(InvalidGrant):
        await HubSpotProvider().refresh("BAD")


@pytest.mark.asyncio
async def test_hubspot_connect_create_and_upsert(beanie_test_db, fakes):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    _, hub = fakes
    pid = await _pocket()
    site = await _site(pid)
    sid = str(site.id)
    async with _client() as c:
        msg = await _connect(c, "hubspot", pid)
        assert msg["status"] == "active", msg
        created = await _gw(
            c,
            sid,
            "hubspot",
            "create_contact",
            args={"email": " Ada@Example.com ", "firstname": "Ada", "message": "Hi\x00 there"},
        )
        updated = await _gw(
            c,
            sid,
            "hubspot",
            "create_contact",
            args={"email": "ada@example.com", "firstname": "Eve", "company": "Analytical"},
        )
        unchanged = await _gw(
            c,
            sid,
            "hubspot",
            "create_contact",
            args={"email": "ada@example.com", "firstname": "Mallory", "message": "spam"},
        )
    conn = await IntegrationConnection.find_one(IntegrationConnection.provider == "hubspot")
    assert conn.account_ref == "4242" and conn.account_label == "acme.hubspot.com"
    assert created.json() == {"ok": True, "result": {"status": "ok"}}, created.text
    assert updated.json() == {"ok": True, "result": {"status": "ok"}}, updated.text
    assert unchanged.json() == {"ok": True, "result": {"status": "ok"}}, unchanged.text
    # A public caller filled the empty company and overwrote nothing.
    assert hub.contacts["ada@example.com"] == {
        "email": "ada@example.com",
        "firstname": "Ada",
        "message": "Hi there",
        "company": "Analytical",
    }
    [patch] = [call for call in hub.calls if call[0] == "PATCH"]
    assert patch[1] == "/crm/v3/objects/contacts/ada@example.com"
    assert patch[2] == {"idProperty": "email"}
    assert patch[3] == {"properties": {"company": "Analytical"}}
    # The token-info URL carries the token: no httpx span for it, spans elsewhere.
    assert hub.traced["/oauth/v1/access-tokens"] is False
    assert hub.traced["/oauth/v1"] is True and hub.traced["/crm/v3/objects"] is True


@pytest.mark.asyncio
async def test_hubspot_validation_and_rate_limit(beanie_test_db, fakes):
    _, hub = fakes
    pid = await _pocket()
    site = await _site(pid)
    sid = str(site.id)
    async with _client() as c:
        await _connect(c, "hubspot", pid)
        bad_email = await _gw(c, sid, "hubspot", "create_contact", args={"email": "nope"})
        missing = await _gw(c, sid, "hubspot", "create_contact", args={"firstname": "A"})
        extra = await _gw(
            c, sid, "hubspot", "create_contact", args={"email": "a@b.co", "lifecyclestage": "x"}
        )
        long_name = await _gw(
            c, sid, "hubspot", "create_contact", args={"email": "a@b.co", "firstname": "a" * 101}
        )
        hub.rate_limited = True
        limited = await _gw(c, sid, "hubspot", "create_contact", args={"email": "a@b.co"})
    for resp in (bad_email, missing, extra, long_name):
        assert resp.status_code == 422 and _code(resp) == "invalid_args", resp.text
    assert limited.status_code == 429 and _code(limited) == "rate_limited"


@pytest.mark.asyncio
async def test_hubspot_refuses_a_grant_without_contacts_write(beanie_test_db, fakes):
    _, hub = fakes
    hub.scopes = ["oauth", "crm.objects.contacts.read"]
    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, "hubspot", pid)
    assert msg["status"] == "error" and msg["error"] == "provider_error"


def test_registry_lists_both_providers(fakes):
    from pocketpaw_ee.integrations.providers import get_provider

    sheets, hub = get_provider("google_sheets"), get_provider("hubspot")
    assert (sheets.kind, sheets.category) == ("connect", "data")
    assert (hub.kind, hub.category) == ("connect", "crm")
    sheet_fields = {f.key: (f.type, f.readonly, f.hidden) for f in sheets.settings_fields}
    assert sheet_fields == {
        "spreadsheet_url": ("link", True, False),
        "sheet_name": ("text", False, False),
        "spreadsheet_id": ("text", True, True),
    }
    assert WS  # the shared fixtures module is wired


# ------------------------------------------------------- vault: refused tokens


async def _fresh_demo_conn():
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    conn = IntegrationConnection(
        workspace=WS,
        provider="demo",
        account_ref="demo-account",
        token_ciphertext=crypto.encrypt_json({"access_token": "AT-CUR", "refresh_token": "RT"}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        created_by="user-owner",
    )
    await conn.insert()
    return conn


def _counting_refresh(monkeypatch) -> list[str]:
    from pocketpaw_ee.integrations.providers.base import TokenGrant
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    calls: list[str] = []

    async def _refresh(self, refresh_token):
        calls.append(refresh_token)
        await asyncio.sleep(0.2)
        return TokenGrant(access_token=f"AT-NEW-{len(calls)}", expires_in=3600)

    monkeypatch.setattr(DemoProvider, "refresh", _refresh)
    return calls


@pytest.mark.asyncio
async def test_two_concurrent_401s_refresh_once_and_share_the_new_token(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection
    from pocketpaw_ee.integrations import vault

    calls = _counting_refresh(monkeypatch)
    conn = await _fresh_demo_conn()
    copies = [await IntegrationConnection.get(conn.id) for _ in range(2)]
    tokens = await asyncio.gather(*(vault.get_valid_token(c, rejected="AT-CUR") for c in copies))
    assert tokens == ["AT-NEW-1", "AT-NEW-1"]
    assert calls == ["RT"]


@pytest.mark.asyncio
async def test_a_holder_that_keeps_the_refused_token_does_not_hand_it_back(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection
    from pocketpaw_ee.integrations import vault

    calls = _counting_refresh(monkeypatch)
    conn = await _fresh_demo_conn()
    # Someone else holds the lease and will finish without a refresh.
    coll = IntegrationConnection.get_pymongo_collection()
    await coll.update_one(
        {"_id": conn.id},
        {
            "$set": {
                "refresh_lease_owner": "other",
                "refresh_lease_until": datetime.now(UTC) + timedelta(seconds=30),
            }
        },
    )

    async def _release():
        await asyncio.sleep(0.2)
        await coll.update_one(
            {"_id": conn.id}, {"$set": {"refresh_lease_owner": None, "refresh_lease_until": None}}
        )

    token, _ = await asyncio.gather(vault.get_valid_token(conn, rejected="AT-CUR"), _release())
    assert token == "AT-NEW-1" and calls == ["RT"]
