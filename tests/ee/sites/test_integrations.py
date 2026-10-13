# tests/ee/sites/test_integrations.py: Sites integrations wave 0 (pocketpaw_ee.integrations).
#
# Covers the vault (encryption, never in a DTO, leased refresh with rotation,
# needs_reconnect), hosted OAuth (single-use state, callback links the site), owner
# routes and their authz, the public gateway's gate order (every error code,
# Turnstile on writes only, the PAW_GATEWAY_TOKEN path), the built-in newsletter
# (dedupe + CSV), webhook dedupe + orders, and the agent tools' SSE payload.
# The ``demo`` provider is the fake connect provider; no network anywhere.
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

WS = "ws_int"
OTHER_WS = "ws_other"
OWNER = "user-owner"
EDITOR = "user-editor"
SITE_KEY = "site_key_" + "k" * 32
SITE_HOST = "shop.example.com"
ORIGIN = f"https://{SITE_HOST}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.integrations import gateway

    from pocketpaw.security.rate_limiter import RateLimiter

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_DEMO", "1")
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_DEMO_WEBHOOK_SECRET", "whsec-test")
    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_CALLBACK_BASE", "https://connect.example.com")
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(gateway, "_ip_limiter", RateLimiter(rate=1000.0, capacity=1000))
    monkeypatch.setattr(gateway, "_turnstile_configured", lambda: False)


async def _pocket(*, workspace: str = WS, owner: str = OWNER) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=workspace,
        name="Shop",
        type="site",
        owner=owner,
        visibility="workspace",
        engine="html",
        widgets=[],
    )
    await doc.insert()
    return str(doc.id)


async def _site(pocket_id: str, *, workspace: str = WS):
    from pocketpaw_ee.cloud.models.site import Site

    site = Site(
        workspace=workspace,
        pocket_id=pocket_id,
        owner=OWNER,
        script_name="shop-abc",
        signed_key=SITE_KEY,
        allowed_origins=[SITE_HOST],
    )
    await site.insert()
    return site


class _Membership:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.role = "member"


def _app(workspace_id: str, user_id: str) -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.integrations.router import public_router, router

    user = SimpleNamespace(
        id=user_id, active_workspace=workspace_id, workspaces=[_Membership(workspace_id)]
    )
    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.include_router(public_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client(workspace_id: str = WS, user_id: str = OWNER) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=_app(workspace_id, user_id)), base_url="https://t"
    )


async def _start(c: AsyncClient, pocket_id: str | None = None):
    """connect -> /start. Returns (/start response, code, state)."""
    body = {"pocket_id": pocket_id} if pocket_id else {}
    started = await c.post("/api/v1/integrations/demo/connect", json=body)
    assert started.status_code == 200, started.text
    url = started.json()["authorize_url"]
    assert url.startswith("https://connect.example.com/api/v1/integrations/demo/start?t=")
    split = urlsplit(url)
    resp = await c.get(split.path, params=parse_qs(split.query))
    assert resp.status_code == 302, resp.text
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    return resp, query["code"][0], query["state"][0]


async def _connect_demo(c: AsyncClient, pocket_id: str | None = None) -> tuple[str, str]:
    """Run the demo OAuth dance in one browser. Returns (callback html, state)."""
    _, code, state = await _start(c, pocket_id)
    cb = await c.get("/api/v1/integrations/demo/callback", params={"code": code, "state": state})
    assert cb.status_code == 200
    return cb.text, state


async def _connected(pocket_id: str | None = None) -> str:
    async with _client() as c:
        page, _ = await _connect_demo(c, pocket_id)
    return _message(page)["connection_id"]


def _cookie(state: str) -> dict:
    from pocketpaw_ee.integrations.oauth import state_hash

    return {"cookie": f"paw_int_demo={state_hash(state)}"}


def _message(page: str) -> dict:
    line = next(ln for ln in page.splitlines() if "var msg =" in ln)
    return json.loads(line.split("var msg =", 1)[1].strip().rstrip(";"))


async def _gw(c: AsyncClient, site_id: str, provider: str, action: str, *, args=None, **kw):
    headers = kw.pop("headers", None)
    if headers is None:
        headers = {"x-paw-site-key": SITE_KEY, "origin": ORIGIN}
    body = {"args": args or {}, **kw}
    return await c.post(
        f"/api/v1/sites/{site_id}/x/{provider}/{action}", json=body, headers=headers
    )


def _code(resp) -> str:
    body = resp.json()
    assert body["ok"] is False, body
    return body["error"]["code"]


# ------------------------------------------------------------------- vault


@pytest.mark.asyncio
async def test_tokens_are_encrypted_and_never_in_a_view(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection
    from pocketpaw_ee.integrations import vault
    from pocketpaw_ee.integrations.providers.base import TokenGrant

    grant = TokenGrant(
        access_token="AT-SECRET-1",
        refresh_token="RT-SECRET-1",
        expires_in=3600,
        scopes=["a"],
        account_label="Acme",
        account_ref="acct_1",
    )
    conn = await vault.store_grant(workspace_id=WS, provider="demo", grant=grant, user_id=OWNER)
    row = await IntegrationConnection.get(conn.id)
    assert "AT-SECRET-1" not in row.token_ciphertext and "RT-SECRET" not in row.token_ciphertext
    assert await vault.get_valid_token(row) == "AT-SECRET-1"
    view = vault.to_view(row).model_dump_json()
    assert "SECRET" not in view and "token" not in view

    async with _client() as c:
        listed = await c.get("/api/v1/integrations/connections")
    assert listed.status_code == 200
    assert "SECRET" not in listed.text and "ciphertext" not in listed.text
    assert listed.json()[0]["account_label"] == "Acme"


async def _expired_conn(refresh_token: str = "RT-OLD"):
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    conn = IntegrationConnection(
        workspace=WS,
        provider="demo",
        account_ref="demo-account",
        token_ciphertext=crypto.encrypt_json(
            {"access_token": "AT-OLD", "refresh_token": refresh_token}
        ),
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        created_by=OWNER,
    )
    await conn.insert()
    return conn


@pytest.mark.asyncio
async def test_concurrent_refresh_runs_once_and_keeps_the_newest_refresh_token(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection
    from pocketpaw_ee.integrations import vault
    from pocketpaw_ee.integrations.providers.base import TokenGrant
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    calls: list[str] = []

    async def _refresh(self, refresh_token):
        calls.append(refresh_token)
        await asyncio.sleep(0.2)
        return TokenGrant(access_token="AT-NEW", refresh_token="RT-NEW", expires_in=3600)

    monkeypatch.setattr(DemoProvider, "refresh", _refresh)
    conn = await _expired_conn()
    copies = [await IntegrationConnection.get(conn.id) for _ in range(3)]
    tokens = await asyncio.gather(*(vault.get_valid_token(c) for c in copies))

    assert tokens == ["AT-NEW"] * 3
    assert calls == ["RT-OLD"]
    row = await IntegrationConnection.get(conn.id)
    assert crypto.decrypt_json(row.token_ciphertext)["refresh_token"] == "RT-NEW"
    assert row.refresh_lease_owner is None


@pytest.mark.asyncio
async def test_a_provider_that_does_not_rotate_keeps_the_old_refresh_token(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection
    from pocketpaw_ee.integrations import vault
    from pocketpaw_ee.integrations.providers.base import TokenGrant
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    async def _refresh(self, refresh_token):
        return TokenGrant(access_token="AT-2", expires_in=3600)

    monkeypatch.setattr(DemoProvider, "refresh", _refresh)
    conn = await _expired_conn()
    assert await vault.get_valid_token(conn) == "AT-2"
    row = await IntegrationConnection.get(conn.id)
    assert crypto.decrypt_json(row.token_ciphertext)["refresh_token"] == "RT-OLD"


@pytest.mark.asyncio
async def test_invalid_grant_flips_needs_reconnect_and_tells_the_site(
    beanie_test_db, _recording_bus_for_sites
):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection, SiteIntegration
    from pocketpaw_ee.integrations import vault

    pid = await _pocket()
    conn = await _expired_conn(refresh_token="RT-revoked")
    await SiteIntegration(
        workspace=WS,
        pocket_id=pid,
        provider="demo",
        connection_id=str(conn.id),
        enabled_actions=["echo", "record"],
    ).insert()

    with pytest.raises(vault.NotConnected):
        await vault.get_valid_token(conn)
    row = await IntegrationConnection.get(conn.id)
    assert row.status == "needs_reconnect"
    events = [e for e in _recording_bus_for_sites.events if e.type == "site.integration_changed"]
    assert events and events[-1].data["status"] == "needs_reconnect"
    assert events[-1].data["pocket_id"] == pid

    async with _client() as c:
        views = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert views[0]["status"] == "needs_reconnect"


# ------------------------------------------------------------------- oauth


@pytest.mark.asyncio
async def test_connect_callback_links_the_site_and_state_is_single_use(
    beanie_test_db, _recording_bus_for_sites
):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    pid = await _pocket()
    async with _client() as c:
        page, state = await _connect_demo(c, pid)
        msg = _message(page)
        assert msg["type"] == "paw:integration-connected"
        assert msg["provider"] == "demo" and msg["status"] == "active"
        assert msg["connection_id"]
        assert "demo-access" not in page and "demo-refresh" not in page
        assert '"*"' not in page and "https://app.example.com" in page

        views = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        assert views == [
            {
                "provider": "demo",
                "connection_id": msg["connection_id"],
                "status": "connected",
                "settings": {},
                "enabled_actions": ["echo", "record"],
                "updated_at": views[0]["updated_at"],
            }
        ]
        conns = (await c.get("/api/v1/integrations/connections")).json()
        assert conns[0]["site_count"] == 1 and conns[0]["status"] == "active"

        replay = await c.get(
            "/api/v1/integrations/demo/callback",
            params={"code": "demo_x", "state": state},
            headers=_cookie(state),
        )
    replayed = _message(replay.text)
    assert replayed["status"] == "error" and replayed["error"] == "invalid_state"
    assert await IntegrationConnection.find_all().count() == 1
    changed = [e for e in _recording_bus_for_sites.events if e.type == "site.integration_changed"]
    assert any(e.data["status"] == "connected" for e in changed)


@pytest.mark.asyncio
async def test_state_for_one_provider_cannot_finish_another(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.auth import _oauth_state

    state = await _oauth_state.issue(
        "integrations", {"workspace_id": WS, "user_id": OWNER, "provider": "other", "pocket_id": ""}
    )
    async with _client() as c:
        page = await c.get(
            "/api/v1/integrations/demo/callback",
            params={"code": "demo_x", "state": state},
            headers=_cookie(state),
        )
    assert _message(page.text)["error"] == "invalid_state"
    assert "script-src 'nonce-" in page.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_start_sets_a_browser_bound_cookie_and_the_ticket_is_single_use(beanie_test_db):
    async with _client() as c:
        started = await c.post("/api/v1/integrations/demo/connect", json={})
        split = urlsplit(started.json()["authorize_url"])
        first = await c.get(split.path, params=parse_qs(split.query))
        second = await c.get(split.path, params=parse_qs(split.query))
    cookie = first.headers["set-cookie"].lower()
    assert first.status_code == 302
    for attr in (
        "paw_int_demo=",
        "httponly",
        "secure",
        "samesite=lax",
        "path=/api/v1/integrations/",
    ):
        assert attr in cookie
    assert _message(second.text)["error"] == "invalid_ticket"


@pytest.mark.asyncio
async def test_callback_in_another_browser_is_refused_and_does_not_burn_the_flow(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    pid = await _pocket()
    async with _client() as owner_browser:
        _, code, state = await _start(owner_browser, pid)
        async with _client() as victim_browser:
            stolen = await victim_browser.get(
                "/api/v1/integrations/demo/callback", params={"code": code, "state": state}
            )
        assert _message(stolen.text)["error"] == "invalid_state"
        assert await IntegrationConnection.find_all().count() == 0
        ok = await owner_browser.get(
            "/api/v1/integrations/demo/callback", params={"code": code, "state": state}
        )
    assert _message(ok.text)["status"] == "active"
    assert "paw_int_demo=" in ok.headers["set-cookie"]  # cleared
    assert "max-age=0" in ok.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_pkce_verifier_rides_the_state_and_a_wrong_challenge_fails(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    async with _client() as c:
        _, code, state = await _start(c)
        tampered = code.rsplit(".", 1)[0] + ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        page = await c.get(
            "/api/v1/integrations/demo/callback", params={"code": tampered, "state": state}
        )
    assert _message(page.text)["error"] == "provider_error"
    assert await IntegrationConnection.find_all().count() == 0


@pytest.mark.asyncio
async def test_builtins_cannot_be_connected_and_unknown_providers_404(beanie_test_db):
    async with _client() as c:
        builtin = await c.post("/api/v1/integrations/newsletter/connect", json={})
        unknown = await c.post("/api/v1/integrations/nope/connect", json={})
        providers = (await c.get("/api/v1/integrations/providers")).json()
    assert builtin.status_code == 422
    assert unknown.status_code == 404
    assert {p["id"] for p in providers} == {"newsletter", "calendly", "cal_com", "demo"}
    demo = next(p for p in providers if p["id"] == "demo")
    assert demo["kind"] == "connect" and demo["availability"] == "available"
    assert {a["name"]: (a["public"], a["write"]) for a in demo["actions"]} == {
        "echo": (True, False),
        "record": (True, True),
    }


@pytest.mark.asyncio
async def test_demo_is_absent_without_the_env_switch(beanie_test_db, monkeypatch):
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_DEMO")
    async with _client() as c:
        providers = (await c.get("/api/v1/integrations/providers")).json()
        connect = await c.post("/api/v1/integrations/demo/connect", json={})
    assert [p["id"] for p in providers] == ["newsletter", "calendly", "cal_com"]
    assert connect.status_code == 404


# -------------------------------------------------------------- owner routes


@pytest.mark.asyncio
async def test_put_unlink_and_revoke(beanie_test_db):
    pid = await _pocket()
    url = f"/api/v1/sites/by-pocket/{pid}/integrations"
    async with _client() as c:
        page, _ = await _connect_demo(c)
        conn_id = _message(page)["connection_id"]
        put = await c.put(
            f"{url}/demo",
            json={
                "connection_id": conn_id,
                "settings": {"note": "A"},
                "enabled_actions": ["echo"],
            },
        )
        assert put.status_code == 200, put.text
        assert put.json()["status"] == "connected"
        assert put.json()["enabled_actions"] == ["echo"]
        bad = await c.put(f"{url}/demo", json={"enabled_actions": ["drop_tables"]})
        assert bad.status_code == 422

        revoked = await c.delete(f"/api/v1/integrations/connections/{conn_id}")
        assert revoked.status_code == 204
        after = (await c.get(url)).json()
        assert after[0]["status"] == "needs_connection" and after[0]["connection_id"] is None

        unlinked = await c.delete(f"{url}/demo")
        missing = await c.delete(f"{url}/demo")
        assert unlinked.status_code == 204 and missing.status_code == 404
        assert (await c.get(url)).json() == []


@pytest.mark.asyncio
async def test_another_workspace_sees_404_and_cannot_revoke(beanie_test_db):
    pid = await _pocket()
    async with _client() as c:
        page, _ = await _connect_demo(c, pid)
        conn_id = _message(page)["connection_id"]
    async with _client(OTHER_WS, OWNER) as c:
        listed = await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")
        put = await c.put(f"/api/v1/sites/by-pocket/{pid}/integrations/newsletter", json={})
        revoke = await c.delete(f"/api/v1/integrations/connections/{conn_id}")
        orders = await c.get(f"/api/v1/sites/by-pocket/{pid}/orders")
    assert (listed.status_code, put.status_code, revoke.status_code, orders.status_code) == (
        404,
        404,
        404,
        404,
    )


@pytest.mark.asyncio
async def test_linking_someone_elses_grant_needs_its_creator_or_an_admin(beanie_test_db):
    pid = await _pocket()
    conn_id = await _connected()
    url = f"/api/v1/sites/by-pocket/{pid}/integrations/demo"
    async with _client(WS, EDITOR) as c:
        denied = await c.put(url, json={"connection_id": conn_id})
    async with _client() as c:
        allowed = await c.put(url, json={"connection_id": conn_id})
    assert (denied.status_code, denied.json()["error"]["code"]) == (
        403,
        "integrations.not_connection_owner",
    )
    assert allowed.status_code == 200 and allowed.json()["status"] == "connected"


@pytest.mark.asyncio
async def test_only_the_creator_or_an_admin_revokes(beanie_test_db):
    async with _client() as c:
        page, _ = await _connect_demo(c)
        conn_id = _message(page)["connection_id"]
    async with _client(WS, EDITOR) as c:
        denied = await c.delete(f"/api/v1/integrations/connections/{conn_id}")
    assert denied.status_code == 403


# ----------------------------------------------------------------- gateway


async def _link(pocket_id: str, provider: str, **body) -> dict:
    async with _client() as c:
        resp = await c.put(
            f"/api/v1/sites/by-pocket/{pocket_id}/integrations/{provider}", json=body
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_gateway_auth_gates(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid)
    await _link(pid, "newsletter")
    sid = str(site.id)
    async with _client() as c:
        none = await _gw(c, sid, "newsletter", "subscribe", headers={"origin": ORIGIN})
        bad_key = await _gw(
            c,
            sid,
            "newsletter",
            "subscribe",
            headers={"x-paw-site-key": "x" * 40, "origin": ORIGIN},
        )
        wrong_origin = await _gw(
            c,
            sid,
            "newsletter",
            "subscribe",
            headers={"x-paw-site-key": SITE_KEY, "origin": "https://evil.test"},
        )
        no_origin = await _gw(
            c, sid, "newsletter", "subscribe", headers={"x-paw-site-key": SITE_KEY}
        )
        other_site = await _gw(c, "some-other-site", "newsletter", "subscribe")
        by_script = await _gw(c, "shop-abc", "newsletter", "subscribe", args={"email": "a@b.co"})
    assert (none.status_code, _code(none)) == (401, "unauthorized")
    assert (bad_key.status_code, _code(bad_key)) == (401, "unauthorized")
    assert (wrong_origin.status_code, _code(wrong_origin)) == (403, "origin_denied")
    assert (no_origin.status_code, _code(no_origin)) == (403, "origin_denied")
    assert (other_site.status_code, _code(other_site)) == (401, "unauthorized")
    assert by_script.status_code == 200 and by_script.json() == {
        "ok": True,
        "result": {"subscribed": True},
    }


@pytest.mark.asyncio
async def test_gateway_binding_gates(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid)
    sid = str(site.id)
    async with _client() as c:
        unbound = await _gw(c, sid, "newsletter", "subscribe", args={"email": "a@b.co"})
        unknown_provider = await _gw(c, sid, "nope", "x")
        await _link(pid, "newsletter", enabled_actions=[])
        disabled = await _gw(c, sid, "newsletter", "subscribe", args={"email": "a@b.co"})
        unknown_action = await _gw(c, sid, "newsletter", "export")
        await _link(pid, "demo")  # connect kind, no connection yet
        no_grant = await _gw(c, sid, "demo", "echo", args={"message": "hi"})
    assert (unbound.status_code, _code(unbound)) == (409, "not_connected")
    assert (unknown_provider.status_code, _code(unknown_provider)) == (409, "not_connected")
    assert (disabled.status_code, _code(disabled)) == (403, "action_disabled")
    assert (unknown_action.status_code, _code(unknown_action)) == (403, "action_disabled")
    assert (no_grant.status_code, _code(no_grant)) == (409, "not_connected")


@pytest.mark.asyncio
async def test_turnstile_on_writes_only_and_gateway_token_skips_it(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core import turnstile
    from pocketpaw_ee.cloud.models.integration import IntegrationDemoRecord
    from pocketpaw_ee.integrations import gateway
    from pocketpaw_ee.sites import site_secrets

    verified: list[str] = []

    async def _verify(token, remote_ip, *, code, transport=None):
        from pocketpaw_ee.cloud._core.errors import BadRequest

        verified.append(token)
        if token != "good":
            raise BadRequest(code, "no")

    monkeypatch.setattr(gateway, "_turnstile_configured", lambda: True)
    monkeypatch.setattr(turnstile, "verify_turnstile", _verify)

    pid = await _pocket()
    site = await _site(pid)
    sid = str(site.id)
    async with _client() as c:
        page, _ = await _connect_demo(c, pid)
        read = await _gw(c, sid, "demo", "echo", args={"message": "hi"})
        missing = await _gw(c, sid, "demo", "record", args={"label": "x"})
        wrong = await _gw(c, sid, "demo", "record", args={"label": "x"}, turnstile_token="bad")
        ok = await _gw(c, sid, "demo", "record", args={"label": "x"}, turnstile_token="good")

        token = (await site_secrets.secrets_for_deploy(site))["PAW_GATEWAY_TOKEN"]
        worker = await _gw(
            c,
            sid,
            "demo",
            "record",
            args={"label": "w"},
            headers={"authorization": f"Bearer {token}"},
        )
        bad_bearer = await _gw(
            c, sid, "demo", "record", args={"label": "w"}, headers={"authorization": "Bearer nope"}
        )
    assert read.status_code == 200 and read.json()["result"] == {"echo": "hi"}
    assert (missing.status_code, _code(missing)) == (403, "turnstile_required")
    assert (wrong.status_code, _code(wrong)) == (403, "turnstile_required")
    assert ok.status_code == 200 and ok.json()["result"]["recorded"] is True
    assert worker.status_code == 200, worker.text
    assert (bad_bearer.status_code, _code(bad_bearer)) == (401, "unauthorized")
    assert verified == ["bad", "good"]
    assert [r.args["label"] for r in await IntegrationDemoRecord.find_all().to_list()] == ["x", "w"]
    for resp in (read, ok, worker):
        assert "demo-access" not in resp.text and token not in resp.text


@pytest.mark.asyncio
async def test_gateway_args_rate_limit_and_provider_error(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import ActionSpec, ProviderError
    from pocketpaw_ee.integrations.providers.demo import DemoProvider, EchoArgs

    pid = await _pocket()
    site = await _site(pid)
    sid = str(site.id)
    await _link(pid, "newsletter")
    async with _client() as c:
        extra = await _gw(c, sid, "newsletter", "subscribe", args={"email": "a@b.co", "x": 1})
        bad_email = await _gw(c, sid, "newsletter", "subscribe", args={"email": "not-an-email"})
        too_long = await _gw(c, sid, "newsletter", "subscribe", args={"email": "a" * 3000})
        statuses = [
            (await _gw(c, sid, "newsletter", "subscribe", args={"email": f"p{i}@b.co"})).status_code
            for i in range(10)
        ]

        async def _boom(ctx, args):
            raise ProviderError("upstream 500")

        monkeypatch.setitem(
            DemoProvider.actions,
            "echo",
            ActionSpec(args=EchoArgs, handler=_boom, description="x", public=True),
        )
        await _connect_demo(c, pid)
        failed = await _gw(c, sid, "demo", "echo", args={"message": "hi"})
    assert (extra.status_code, _code(extra)) == (422, "invalid_args")
    assert (bad_email.status_code, _code(bad_email)) == (422, "invalid_args")
    assert (too_long.status_code, _code(too_long)) == (422, "invalid_args")
    # 3 invalid calls already spent 3 of the 10/min per-IP write slots.
    assert statuses.count(200) == 7 and statuses[-1] == 429
    assert (failed.status_code, _code(failed)) == (502, "provider_error")
    assert "upstream" not in failed.text


@pytest.mark.asyncio
async def test_gateway_per_ip_bucket_runs_first(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import gateway

    from pocketpaw.security.rate_limiter import RateLimiter

    monkeypatch.setattr(gateway, "_ip_limiter", RateLimiter(rate=0.0001, capacity=1))
    async with _client() as c:
        first = await _gw(c, "nope", "newsletter", "subscribe")
        second = await _gw(c, "nope", "newsletter", "subscribe")
    assert _code(first) == "unauthorized"
    assert (second.status_code, _code(second)) == (429, "rate_limited")


@pytest.mark.asyncio
async def test_newsletter_dedupes_and_exports_csv_to_the_owner_only(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import NewsletterSubscriber

    pid = await _pocket()
    site = await _site(pid)
    await _link(pid, "newsletter")
    async with _client() as c:
        a = await _gw(c, str(site.id), "newsletter", "subscribe", args={"email": "Ann@Example.com"})
        b = await _gw(
            c, str(site.id), "newsletter", "subscribe", args={"email": " ann@example.com "}
        )
        c2 = await _gw(c, str(site.id), "newsletter", "subscribe", args={"email": "=cmd@x.io"})
        csv = await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations/newsletter/export.csv")
    assert a.json() == b.json() == {"ok": True, "result": {"subscribed": True}}
    assert c2.status_code == 200
    assert await NewsletterSubscriber.find_all().count() == 2
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0] == "email,subscribed_at"
    assert lines[1].startswith("ann@example.com,")
    assert lines[2].startswith("'=cmd@x.io,")

    async with _client(WS, EDITOR) as c:
        denied = await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations/newsletter/export.csv")
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_gateway_cors_answers_preflight_for_the_gateway_path_only():
    from pocketpaw_ee.integrations.gateway import GatewayCors

    inner_calls: list[str] = []

    async def inner(scope, receive, send):
        inner_calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = GatewayCors(inner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        pre = await c.options(
            "/api/v1/sites/s1/x/newsletter/subscribe",
            headers={"origin": ORIGIN, "access-control-request-method": "POST"},
        )
        post = await c.post("/api/v1/sites/s1/x/newsletter/subscribe", headers={"origin": ORIGIN})
        other = await c.post("/api/v1/integrations/connections", headers={"origin": ORIGIN})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in pre.headers
    assert post.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-origin" not in other.headers
    assert inner_calls == [
        "/api/v1/sites/s1/x/newsletter/subscribe",
        "/api/v1/integrations/connections",
    ]


# ---------------------------------------------------------- webhooks + orders


def _event(
    event_id: str, site_id: str, account_ref: str = "demo-account", **data
) -> tuple[bytes, dict]:
    from pocketpaw_ee.integrations.providers.demo import sign

    body = json.dumps(
        {
            "id": event_id,
            "type": data.pop("type", "order.paid"),
            "site_id": site_id,
            "account_ref": account_ref,
            "data": data,
        }
    ).encode()
    return body, {"x-demo-signature": sign(body), "content-type": "application/json"}


@pytest.mark.asyncio
async def test_webhooks_verify_dedupe_and_record_orders(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import SiteOrder
    from pocketpaw_ee.cloud.shared.events import event_bus

    seen: list[dict] = []

    async def _on(data):
        seen.append(data)

    event_bus.subscribe("integrations.event", _on)
    try:
        pid = await _pocket()
        site = await _site(pid)
        await _connected(pid)
        sid = str(site.id)
        url = "/api/v1/integrations/webhooks/demo"
        order = {
            "order_id": "ord_1",
            "amount_minor": 2599,
            "currency": "usd",
            "customer_email": "buyer@x.io",
            "items": [{"ref": "sku_1", "name": "Mug", "qty": 1, "amount_minor": 2599}],
        }
        async with _client() as c:
            body, headers = _event("evt_1", sid, **order)
            first = await c.post(url, content=body, headers=headers)
            again = await c.post(url, content=body, headers=headers)
            forged = await c.post(
                url, content=body, headers={**headers, "x-demo-signature": "0" * 64}
            )
            body2, headers2 = _event("evt_2", sid, type="order.refunded", order_id="ord_1")
            refunded = await c.post(url, content=body2, headers=headers2)
            body3, headers3 = _event("evt_3", sid, type="order.pending", order_id="ord_1")
            late = await c.post(url, content=body3, headers=headers3)
            orders = (await c.get(f"/api/v1/sites/by-pocket/{pid}/orders")).json()
        async with _client(WS, EDITOR) as c:
            masked = (await c.get(f"/api/v1/sites/by-pocket/{pid}/orders")).json()
    finally:
        event_bus.unsubscribe("integrations.event", _on)

    assert first.json() == {"ok": True}
    assert again.json() == {"ok": True, "duplicate": True}
    assert (forged.status_code, forged.json()["error"]["code"]) == (401, "unauthorized")
    assert refunded.status_code == 200 and late.status_code == 200
    assert await SiteOrder.find_all().count() == 1
    [view] = orders["items"]
    assert orders["next_cursor"] is None
    assert view["external_id"] == "ord_1" and view["status"] == "refunded"
    assert (view["amount_minor"], view["currency"]) == (2599, "USD")
    assert view["items"] == [{"ref": "sku_1", "name": "Mug", "qty": 1, "amount_minor": 2599}]
    assert {d["event_id"] for d in seen} == {"evt_1", "evt_2", "evt_3"}
    assert all("buyer@x.io" not in json.dumps(d) for d in seen)
    # Owner sees the email; an editor gets it masked.
    assert view["customer_email"] == "buyer@x.io"
    assert masked["items"][0]["customer_email"] == "b***@x.io"


@pytest.mark.asyncio
async def test_unbound_sites_get_no_orders_and_orders_paginate(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    pid = await _pocket()
    site = await _site(pid)
    sid = str(site.id)
    async with _client() as c:
        body, headers = _event("evt_x", sid, order_id="o_x", amount_minor=1)
        unrouted = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
        assert unrouted.json() == {"ok": True, "routed": False}
        assert await SiteOrder.find_all().count() == 0

        await _connected(pid)
        for i in range(3):
            body, headers = _event(f"evt_{i}", sid, order_id=f"o_{i}", amount_minor=i)
            await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
        page1 = (await c.get(f"/api/v1/sites/by-pocket/{pid}/orders", params={"limit": 2})).json()
        page2 = (
            await c.get(
                f"/api/v1/sites/by-pocket/{pid}/orders",
                params={"limit": 2, "cursor": page1["next_cursor"]},
            )
        ).json()
    assert [o["external_id"] for o in page1["items"]] == ["o_2", "o_1"]
    assert [o["external_id"] for o in page2["items"]] == ["o_0"]
    assert page2["next_cursor"] is None


# ------------------------------------------------------------------ agent


@pytest.fixture
def _agent(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import sites_create

    monkeypatch.setattr(sites_create, "_identity", lambda: (WS, EDITOR))
    pushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.push_sse_event",
        lambda kind, data: pushed.append((kind, data)),
    )
    return sites_create, pushed


def _payload(result: dict) -> dict:
    assert not result.get("is_error"), result
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_connect_tool_pushes_the_card_and_creates_the_binding(beanie_test_db, _agent):
    from pocketpaw_ee.sites import site_secrets

    sites_create, pushed = _agent
    pid = await _pocket()
    out = _payload(
        await sites_create._connect_site_integration_handler(
            {"pocket_id": pid, "provider": "demo", "purpose": "Take payments for the mugs"}
        )
    )
    assert out["site_integration"]["status"] == "needs_connection"
    assert "never see" in out["message"] and "data-paw-checkout" in out["message"]

    [(kind, data)] = pushed
    assert kind == "site_integration_requested"
    assert set(data) == {"pocket_id", "provider", "purpose", "site_integration"}
    assert data["pocket_id"] == pid and data["purpose"] == "Take payments for the mugs"
    assert set(data["provider"]) == {
        "id",
        "name",
        "kind",
        "category",
        "plan",
        "availability",
        "actions",
        "settings_fields",
        "requires_connection",
    }
    assert set(data["site_integration"]) == {
        "provider",
        "connection_id",
        "status",
        "settings",
        "enabled_actions",
        "updated_at",
    }
    # Linking minted the gateway token as a site secret (a Worker gets it on deploy).
    names = (
        await site_secrets.secrets_for_deploy(SimpleNamespace(workspace=WS, pocket_id=pid))
    ).keys()
    assert list(names) == ["PAW_GATEWAY_TOKEN"]

    listed = _payload(await sites_create._list_site_integrations_handler({"pocket_id": pid}))
    assert [i["provider"] for i in listed["integrations"]] == ["demo"]
    assert {p["id"] for p in listed["providers"]} == {"newsletter", "calendly", "cal_com", "demo"}

    builtin = _payload(
        await sites_create._connect_site_integration_handler(
            {"pocket_id": pid, "provider": "newsletter", "purpose": "Collect signups"}
        )
    )
    assert builtin["site_integration"]["status"] == "connected"
    unknown = await sites_create._connect_site_integration_handler(
        {"pocket_id": pid, "provider": "stripe", "purpose": "x"}
    )
    assert unknown.get("is_error") and "newsletter" in unknown["content"][0]["text"]


def test_integration_tools_ride_the_sites_allowlist():
    from pocketpaw_ee.agent.mcp_servers.sites import (
        CONNECT_SITE_INTEGRATION_TOOL_ID,
        LIST_SITE_INTEGRATIONS_TOOL_ID,
        SITES_TOOL_IDS,
    )

    assert CONNECT_SITE_INTEGRATION_TOOL_ID in SITES_TOOL_IDS
    assert LIST_SITE_INTEGRATIONS_TOOL_ID in SITES_TOOL_IDS


@pytest.mark.asyncio
async def test_webhook_for_a_site_bound_to_another_account_is_refused(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    pid = await _pocket()
    site = await _site(pid)
    await _connected(pid)  # bound through account "demo-account"
    async with _client() as c:
        body, headers = _event("evt_a", str(site.id), account_ref="acct_attacker", order_id="o")
        forged = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
        body, headers = _event("evt_b", str(site.id), account_ref="", order_id="o")
        missing = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
    assert forged.json() == missing.json() == {"ok": True, "routed": False}
    assert await SiteOrder.find_all().count() == 0


@pytest.mark.asyncio
async def test_account_ref_fallback_routes_one_match_and_refuses_ambiguity(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    pid_a = await _pocket()
    await _site(pid_a)
    await _connected(pid_a)
    async with _client() as c:
        body, headers = _event("evt_1", "", order_id="o_1")
        one = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
    assert one.json() == {"ok": True}
    assert (await SiteOrder.find_one(SiteOrder.external_id == "o_1")).pocket_id == pid_a

    pid_b = await _pocket()
    await _connected(pid_b)  # same demo account, now serving two sites
    async with _client() as c:
        body, headers = _event("evt_2", "", order_id="o_2")
        two = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
    assert two.json() == {"ok": True, "routed": False}
    assert await SiteOrder.find_one(SiteOrder.external_id == "o_2") is None


@pytest.mark.asyncio
async def test_demo_webhooks_need_an_explicit_secret(beanie_test_db, monkeypatch):
    pid = await _pocket()
    site = await _site(pid)
    body, headers = _event("evt_s", str(site.id), order_id="o")
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_DEMO_WEBHOOK_SECRET")
    async with _client() as c:
        resp = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_a_failed_order_write_is_retried_not_acked_as_duplicate(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.integration import IntegrationWebhookEvent, SiteOrder
    from pocketpaw_ee.integrations import webhooks

    pid = await _pocket()
    site = await _site(pid)
    await _connected(pid)
    real = webhooks._apply_order
    calls = {"n": 0}

    async def _flaky(event, binding, site):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("mongo blip")
        await real(event, binding, site)

    monkeypatch.setattr(webhooks, "_apply_order", _flaky)
    async with _client() as c:
        body, headers = _event("evt_r", str(site.id), order_id="o_r", amount_minor=5)
        failed = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
        assert await IntegrationWebhookEvent.find_all().count() == 0
        retried = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
        again = await c.post("/api/v1/integrations/webhooks/demo", content=body, headers=headers)
    assert failed.status_code == 500 and "mongo" not in failed.text
    assert retried.json() == {"ok": True}
    assert again.json() == {"ok": True, "duplicate": True}
    assert (await SiteOrder.find_one(SiteOrder.external_id == "o_r")).amount_minor == 5


@pytest.mark.asyncio
async def test_site_delete_cascade_purges_integration_rows(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import (
        IntegrationConnection,
        IntegrationDemoRecord,
        NewsletterSubscriber,
        SiteIntegration,
        SiteOrder,
    )
    from pocketpaw_ee.sites import delete_cascade

    pid = await _pocket()
    other = await _pocket()
    site = await _site(pid)
    await _connected(pid)
    await _link(pid, "newsletter")
    await _link(other, "newsletter")
    await NewsletterSubscriber(workspace=WS, pocket_id=pid, email="a@b.co").insert()
    await SiteOrder(workspace=WS, pocket_id=pid, provider="demo", external_id="o").insert()
    await IntegrationDemoRecord(workspace=WS, pocket_id=pid, args={}).insert()

    await delete_cascade._purge_integrations(site)

    for doc in (SiteIntegration, NewsletterSubscriber, SiteOrder, IntegrationDemoRecord):
        assert await doc.find(doc.pocket_id == pid).count() == 0
    assert await SiteIntegration.find(SiteIntegration.pocket_id == other).count() == 1
    assert await IntegrationConnection.find_all().count() == 1  # the grant survives
