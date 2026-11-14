# tests/ee/sites/test_integrations_resend.py: the resend provider.
#
# Runs through the real routes (connect -> /start -> Resend -> callback -> site
# link -> settings PUT) against an in-memory fake of Resend's OAuth 2.1 server,
# swapped in as ``oauth_http._transport``. No network. Covers: catalog entry
# (connect, email, no public actions, unconfigured without a client id), PKCE S256
# with a public client (client_id in the body) and a confidential one (Basic),
# the emails:send scope check, rotating refresh (the vault keeps the newest),
# invalid_grant -> needs_reconnect, the required sender setting, and an invalid
# sender leaving the site setup_required.
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    _client,
    _env,
    _message,
    _pocket,
    _site,
)

CLIENT_ID = "6f0c1a52-0000-4000-8000-000000000001"


def _s256(verifier: str) -> str:
    from pocketpaw_ee.integrations.providers.oauth_http import s256

    return s256(verifier)


class FakeResend:
    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}
        self.token_calls: list[tuple[dict, dict]] = []
        self.live_refresh: set[str] = set()
        self.issued = 0
        self.scope = "emails:send"

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.resend.com", request.url
        assert request.url.path == "/oauth/token", request.url
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_calls.append((form, dict(request.headers)))
        if form.get("grant_type") == "authorization_code":
            challenge = self.challenges.get(form.get("code", ""))
            if challenge is None or _s256(form.get("code_verifier", "")) != challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self._issue()
        if form.get("grant_type") == "refresh_token":
            rt = form.get("refresh_token", "")
            if rt not in self.live_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.live_refresh.discard(rt)  # single use
            return self._issue()
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _issue(self) -> httpx.Response:
        self.issued += 1
        rt = f"rt-{self.issued}"
        self.live_refresh.add(rt)
        return httpx.Response(
            200,
            json={
                "access_token": f"at-{self.issued}",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_token": rt,
                "scope": self.scope,
            },
        )


@pytest.fixture
def fake(monkeypatch):
    from pocketpaw_ee.integrations.providers import oauth_http

    server = FakeResend()
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RESEND_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_RESEND_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(server.handle))
    return server


async def _connect(c, fake: FakeResend, pid: str | None = None) -> dict:
    body = {"pocket_id": pid} if pid else {}
    started = await c.post("/api/v1/integrations/resend/connect", json=body)
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    location = urlsplit(start.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        "https://api.resend.com/oauth/authorize"
    )
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert query["client_id"] == CLIENT_ID
    assert query["scope"] == "emails:send"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"]
    fake.challenges["code-1"] = query["code_challenge"]
    cb = await c.get(
        "/api/v1/integrations/resend/callback", params={"code": "code-1", "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text)


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    rows = await IntegrationConnection.find(IntegrationConnection.provider == "resend").to_list()
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_resend_is_an_email_connect_provider_without_public_actions(
    beanie_test_db, monkeypatch
):
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_RESEND_CLIENT_ID", raising=False)
    async with _client() as c:
        views = (await c.get("/api/v1/integrations/providers")).json()
    resend = next(v for v in views if v["id"] == "resend")
    assert resend["kind"] == "connect" and resend["category"] == "email"
    assert resend["availability"] == "unconfigured"
    assert resend["actions"] == []
    keys = {f["key"]: f for f in resend["settings_fields"]}
    assert keys["from_address"]["required"] is True
    assert "from_name" in keys


@pytest.mark.asyncio
async def test_connect_uses_pkce_and_a_public_client(beanie_test_db, fake):
    pid = await _pocket()
    await _site(pid)
    async with _client() as c:
        msg = await _connect(c, fake, pid)
    assert msg["status"] == "active", msg
    form, headers = fake.token_calls[0]
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == CLIENT_ID and form["code_verifier"]
    assert "authorization" not in headers
    conn = await _conn()
    assert conn.account_label == "Resend" and conn.scopes == ["emails:send"]
    assert "at-1" not in conn.token_ciphertext


@pytest.mark.asyncio
async def test_a_confidential_client_authenticates_with_basic(beanie_test_db, fake, monkeypatch):
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RESEND_CLIENT_SECRET", "s3cret")
    async with _client() as c:
        msg = await _connect(c, fake)
    assert msg["status"] == "active", msg
    form, headers = fake.token_calls[0]
    assert "client_id" not in form
    expected = base64.b64encode(f"{CLIENT_ID}:s3cret".encode()).decode()
    assert headers["authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_a_grant_without_the_send_scope_is_refused(beanie_test_db, fake):
    fake.scope = "domains:read"
    async with _client() as c:
        msg = await _connect(c, fake)
    assert msg["status"] == "error"


@pytest.mark.asyncio
async def test_refresh_rotates_and_a_dead_grant_needs_reconnect(beanie_test_db, fake):
    from pocketpaw_ee.integrations import vault

    async with _client() as c:
        await _connect(c, fake)
    conn = await _conn()
    conn.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await conn.save()
    assert await vault.get_valid_token(conn) == "at-2"
    stored = vault._tokens(await _conn())
    assert stored["refresh_token"] == "rt-2"
    form, _ = fake.token_calls[-1]
    assert form == {"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": CLIENT_ID}

    fake.live_refresh.clear()
    conn = await _conn()
    conn.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await conn.save()
    with pytest.raises(vault.NotConnected):
        await vault.get_valid_token(conn)
    assert (await _conn()).status == "needs_reconnect"


@pytest.mark.asyncio
async def test_the_site_needs_a_valid_sender(beanie_test_db, fake):
    pid = await _pocket()
    await _site(pid)
    url = f"/api/v1/sites/by-pocket/{pid}/integrations/resend"
    async with _client() as c:
        await _connect(c, fake, pid)
        view = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        assert next(v for v in view if v["provider"] == "resend")["status"] == "setup_required"

        bad = await c.put(url, json={"settings": {"from_address": "a@b.co\r\nBcc: x@y.z"}})
        assert bad.status_code == 200, bad.text
        assert bad.json()["status"] == "setup_required"

        good = await c.put(
            url, json={"settings": {"from_address": "leads@shop.example", "from_name": "Shop"}}
        )
        assert good.status_code == 200, good.text
        assert good.json()["status"] == "connected"


def test_sender_header_strips_what_could_forge_a_header():
    from pocketpaw_ee.integrations.providers.resend import sender

    assert sender({"from_address": "leads@shop.example"}) == "leads@shop.example"
    assert sender({"from_address": "leads@shop.example", "from_name": 'Shop "<x>"\r\n'}) == (
        '"Shop x" <leads@shop.example>'
    )
    assert sender({"from_address": "Shop <leads@shop.example>"}) == ""
    assert sender({"from_address": "a@b.co\nBcc: c@d.ee"}) == ""
