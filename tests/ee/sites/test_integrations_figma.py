# tests/ee/sites/test_integrations_figma.py: the workspace-only ``figma`` provider.
#
# Runs the real connect routes (connect -> /start -> Figma -> callback) against an
# in-memory fake of Figma's OAuth endpoints and ``/v1/me``, swapped in as
# ``oauth_http._transport``. No network, no real Figma app. Covers: fail-closed
# without the client id/secret, PKCE S256 and the exact read scopes (variables
# only on Enterprise), Basic-auth form token calls, the grant mapping (user id,
# handle, 90-day expiry), non-rotating refresh through the vault (the old refresh
# token is kept, the old access token dies), invalid_grant -> needs_reconnect,
# no public actions, and every site link path refusing the provider.
from __future__ import annotations

import base64
import hashlib
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

CLIENT_ID = "figma-client"
SECRET = "figma-secret"
USER_ID = "1234567890"


class FakeFigma:
    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}
        self.valid: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.issued = 0
        self.token_calls: list[tuple[str, dict]] = []

    def _issue(self) -> str:
        self.issued += 1
        access = f"f-access-{self.issued}"
        # Figma keeps one access token per app per user: a new one kills the old.
        self.valid = {access}
        return access

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.figma.com"
        path = request.url.path
        if path in ("/v1/oauth/token", "/v1/oauth/refresh"):
            basic = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
            assert request.headers["authorization"] == f"Basic {basic}"
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_calls.append((path, body))
            if path == "/v1/oauth/refresh":
                assert set(body) == {"refresh_token"}
                if body["refresh_token"] not in self.refresh_tokens:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(
                    200,
                    json={
                        "access_token": self._issue(),
                        "token_type": "bearer",
                        "expires_in": 7776000,
                    },
                )
            assert body["grant_type"] == "authorization_code"
            challenge = self.challenges.get(body["code"])
            digest = hashlib.sha256(body.get("code_verifier", "").encode()).digest()
            if challenge != base64.urlsafe_b64encode(digest).decode().rstrip("="):
                return httpx.Response(400, json={"error": "invalid_grant"})
            refresh = "f-refresh-1"
            self.refresh_tokens.add(refresh)
            return httpx.Response(
                200,
                json={
                    "user_id_string": USER_ID,
                    "user_id": int(USER_ID),
                    "access_token": self._issue(),
                    "token_type": "bearer",
                    "expires_in": 7776000,
                    "refresh_token": refresh,
                },
            )
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return httpx.Response(403, json={"status": 403, "err": "Invalid token"})
        if path == "/v1/me":
            return httpx.Response(
                200, json={"id": USER_ID, "handle": "Dana Designer", "email": "d@example.com"}
            )
        return httpx.Response(404)


@pytest.fixture
def figma(monkeypatch):
    from pocketpaw_ee.integrations.providers import oauth_http

    fake = FakeFigma()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_FIGMA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_FIGMA_CLIENT_SECRET", SECRET)
    return fake


async def _connect(c, fake: FakeFigma, body: dict | None = None) -> tuple[dict, dict]:
    started = await c.post("/api/v1/integrations/figma/connect", json=body or {})
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    location = urlsplit(start.headers["location"])
    assert (location.scheme, location.netloc, location.path) == ("https", "www.figma.com", "/oauth")
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    fake.challenges["code-1"] = query["code_challenge"]
    cb = await c.get(
        "/api/v1/integrations/figma/callback", params={"code": "code-1", "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text), query


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.find_one(IntegrationConnection.provider == "figma")


# ----------------------------------------------------------------- unit


def test_registered_workspace_only_with_no_public_actions(figma):
    from pocketpaw_ee.integrations.providers import get_provider

    provider = get_provider("figma")
    assert (provider.kind, provider.category, provider.plan) == ("connect", "design", "site")
    assert provider.supports_pkce is True
    assert provider.site_bindable is False
    assert provider.actions == {}
    assert provider.availability() == "available"


def test_unconfigured_fails_closed(monkeypatch):
    from pocketpaw_ee.integrations.providers import get_provider
    from pocketpaw_ee.integrations.providers.base import ProviderError

    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_FIGMA_CLIENT_ID", raising=False)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_FIGMA_CLIENT_SECRET", "only-the-secret")
    provider = get_provider("figma")
    assert provider.availability() == "unconfigured"
    with pytest.raises(ProviderError):
        provider.authorize_url("s", "https://cb", code_challenge="c")


def test_authorize_url_needs_pkce(figma):
    from pocketpaw_ee.integrations.providers import get_provider
    from pocketpaw_ee.integrations.providers.base import ProviderError

    with pytest.raises(ProviderError):
        get_provider("figma").authorize_url("s", "https://cb", code_challenge=None)


def test_low_seat_attention():
    from types import SimpleNamespace

    from pocketpaw_ee.integrations.providers.figma import LOW_SEAT_NOTE, FigmaProvider

    provider = FigmaProvider()
    assert provider.connection_attention(SimpleNamespace(meta={})) is None
    low = SimpleNamespace(meta={"rate_limit_type": "low"})
    assert provider.connection_attention(low) == LOW_SEAT_NOTE


# ------------------------------------------------------------- the flow


@pytest.mark.asyncio
async def test_connect_asks_read_scopes_and_stores_the_grant(beanie_test_db, figma):
    async with _client() as c:
        msg, query = await _connect(c, figma)
    assert msg["status"] == "active", msg
    assert query["code_challenge_method"] == "S256"
    assert query["response_type"] == "code"
    assert query["scope"].split() == [
        "file_content:read",
        "file_metadata:read",
        "current_user:read",
    ]
    assert "files:read" not in query["scope"].split()
    conn = await _conn()
    assert conn.account_ref == USER_ID
    assert conn.account_label == "Dana Designer"
    assert conn.meta["handle"] == "Dana Designer"
    assert conn.meta["variables"] is False
    assert "email" not in conn.meta
    expires = conn.expires_at.replace(tzinfo=UTC)
    assert timedelta(days=89) < expires - datetime.now(UTC) < timedelta(days=91)
    assert "f-access" not in conn.token_ciphertext
    _, body = figma.token_calls[0]
    assert "client_secret" not in body and "client_id" not in body


@pytest.mark.asyncio
async def test_enterprise_adds_the_variables_scope(beanie_test_db, figma):
    async with _client() as c:
        msg, query = await _connect(c, figma, {"params": {"plan": "Enterprise"}})
    assert msg["status"] == "active", msg
    assert query["scope"].split()[-1] == "file_variables:read"
    conn = await _conn()
    assert "file_variables:read" in conn.scopes
    assert conn.meta["variables"] is True
    assert conn.meta["plan_tier"] == "enterprise"
    assert conn.meta["connect_params"] == {"plan": "enterprise"}


@pytest.mark.asyncio
async def test_a_bad_verifier_saves_nothing(beanie_test_db, figma):
    figma.challenges.clear()
    async with _client() as c:
        started = await c.post("/api/v1/integrations/figma/connect", json={})
        split = urlsplit(started.json()["authorize_url"])
        start = await c.get(split.path, params=parse_qs(split.query))
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        figma.challenges["code-1"] = "not-the-challenge"
        cb = await c.get(
            "/api/v1/integrations/figma/callback",
            params={"code": "code-1", "state": query["state"]},
        )
    assert _message(cb.text)["error"] == "provider_error"
    assert await _conn() is None


@pytest.mark.asyncio
async def test_refresh_keeps_the_refresh_token_and_kills_the_old_access(beanie_test_db, figma):
    from pocketpaw_ee.integrations import vault

    async with _client() as c:
        await _connect(c, figma)
    conn = await _conn()
    first = await vault.get_valid_token(conn)
    assert first == "f-access-1"
    conn.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await conn.save()
    second = await vault.get_valid_token(await _conn())
    assert second == "f-access-2"
    assert figma.valid == {"f-access-2"}  # the old access token no longer works
    path, body = figma.token_calls[-1]
    assert (path, body) == ("/v1/oauth/refresh", {"refresh_token": "f-refresh-1"})
    # Non-rotating: the vault still holds the original refresh token.
    from pocketpaw_ee.integrations.vault import _tokens

    assert _tokens(await _conn())["refresh_token"] == "f-refresh-1"
    # A provider 401 on the new token refreshes again with the same refresh token.
    third = await vault.get_valid_token(await _conn(), rejected=second)
    assert third == "f-access-3"
    assert figma.token_calls[-1][1] == {"refresh_token": "f-refresh-1"}


@pytest.mark.asyncio
async def test_dead_refresh_token_needs_reconnect(beanie_test_db, figma):
    from pocketpaw_ee.integrations import vault

    async with _client() as c:
        await _connect(c, figma)
    figma.refresh_tokens.clear()
    conn = await _conn()
    conn.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await conn.save()
    with pytest.raises(vault.NotConnected):
        await vault.get_valid_token(conn)
    assert (await _conn()).status == "needs_reconnect"


# ------------------------------------------------------------ no site links


@pytest.mark.asyncio
async def test_connect_with_a_pocket_never_links_a_site(beanie_test_db, figma):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    pid = await _pocket()
    await _site(pid)
    async with _client() as c:
        msg, _ = await _connect(c, figma, {"pocket_id": pid})
    assert msg["status"] == "active" and "error" not in msg, msg
    assert await SiteIntegration.find_one(SiteIntegration.provider == "figma") is None


@pytest.mark.asyncio
async def test_site_put_refuses_figma(beanie_test_db, figma):
    pid = await _pocket()
    await _site(pid)
    async with _client() as c:
        await _connect(c, figma)
        conn = await _conn()
        resp = await c.put(
            f"/api/v1/sites/by-pocket/{pid}/integrations/figma",
            json={"connection_id": str(conn.id)},
        )
    assert resp.status_code == 422, resp.text
    assert "not_site_bindable" in resp.text


@pytest.mark.asyncio
async def test_agent_cannot_request_figma_for_a_site(beanie_test_db, figma):
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.integrations import bindings

    pid = await _pocket()
    with pytest.raises(ValidationError):
        await bindings.request_from_agent(
            workspace_id="ws_int",
            user_id="user-owner",
            pocket_id=pid,
            provider_id="figma",
            purpose="read the design",
        )


@pytest.mark.asyncio
async def test_the_gateway_has_nothing_to_call(beanie_test_db, figma):
    from tests.ee.sites.test_integrations import _code, _gw

    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        resp = await _gw(c, str(site.id), "figma", "get_file", args={})
    assert resp.status_code == 403
    assert _code(resp) == "action_disabled"
