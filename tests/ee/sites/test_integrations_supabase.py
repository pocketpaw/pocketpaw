# tests/ee/sites/test_integrations_supabase.py: the ``supabase`` database provider.
#
# Runs through the real routes (connect -> /start -> Supabase's authorize page ->
# callback -> site link -> project pick -> site_setup -> unlink) against an
# in-memory fake of Supabase's OAuth server and Management API, swapped in as
# ``oauth_http._transport`` (an ``httpx.MockTransport``). No network anywhere.
#
# Covers: PKCE + Basic client auth on the token exchange, refresh rotation and a
# dead refresh, the organization as account_ref, the remote project options,
# site_setup writing exactly the three managed secrets (values never in views or
# logs), a project change overwriting them, teardown deleting only managed rows, a
# Management API 401 refreshing once and a 403 marking the site setup_required.
from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    OWNER,
    WS,
    _client,
    _env,
    _message,
    _pocket,
    _site,
)

CLIENT_ID = "supabase-client-id"
SECRET = "supabase-client-secret"
ORG_ID = "org-acme-1"
REF_A = "abcdefghijklmnopqrst"
REF_B = "tsrqponmlkjihgfedcba"
PUBLISHABLE_A = "sb_publishable_AAAAvalue"
SECRET_A = "sb_secret_AAAAvalue"
ANON_B = "eyJlegacyANONvalueB"
SECRET_VALUES = (PUBLISHABLE_A, SECRET_A, ANON_B, "sbp-at-", "sbp-rt-")


def _s256(verifier: str) -> str:
    from pocketpaw_ee.integrations.providers.oauth_http import s256

    return s256(verifier)


class FakeSupabase:
    """Supabase's OAuth token endpoint and the Management API calls we use."""

    def __init__(self) -> None:
        self.challenge = ""
        self.issued = 0
        self.access: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.token_forms: list[dict] = []
        self.key_calls: list[dict] = []
        self.deny_keys = False
        self.projects = [
            {"id": REF_A, "ref": REF_A, "name": "Shop DB", "region": "eu-west-1"},
            {"id": REF_B, "ref": REF_B, "name": "Old DB", "region": "us-east-1"},
            {"id": "BAD REF", "ref": "BAD REF", "name": "Weird", "region": "x"},
        ]
        self.keys = {
            REF_A: [
                {"name": "anon", "type": "legacy", "api_key": "eyJlegacyANONvalueA"},
                {"name": "service_role", "type": "legacy", "api_key": "eyJlegacySRvalueA"},
                {"name": "default", "type": "publishable", "api_key": PUBLISHABLE_A},
                {"name": "default", "type": "secret", "api_key": SECRET_A},
            ],
            REF_B: [{"name": "anon", "type": "legacy", "api_key": ANON_B}],
        }

    def _grant(self) -> dict:
        self.issued += 1
        access, refresh = f"sbp-at-{self.issued}", f"sbp-rt-{self.issued}"
        self.access.add(access)
        self.refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.supabase.com", request.url
        path = request.url.path
        if path == "/v1/oauth/token":
            basic = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
            assert request.headers["authorization"] == f"Basic {basic}"
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert "client_secret" not in form
            self.token_forms.append(form)
            if form["grant_type"] == "refresh_token":
                if form["refresh_token"] not in self.refresh_tokens:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refresh_tokens.discard(form["refresh_token"])  # rotation
                return httpx.Response(200, json=self._grant())
            if form.get("code") != "good-code" or _s256(form.get("code_verifier", "")) != (
                self.challenge
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._grant())
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.access:
            return httpx.Response(401, json={"message": "Unauthorized"})
        if path == "/v1/organizations":
            return httpx.Response(200, json=[{"id": ORG_ID, "slug": "acme", "name": "Acme"}])
        if path == "/v1/projects":
            return httpx.Response(200, json=self.projects)
        ref = path.removeprefix("/v1/projects/").removesuffix("/api-keys")
        self.key_calls.append({"ref": ref, "reveal": request.url.params.get("reveal")})
        if self.deny_keys:
            return httpx.Response(403, json={"message": "Forbidden"})
        if ref not in self.keys:
            return httpx.Response(404, json={"message": "Not found"})
        return httpx.Response(200, json=self.keys[ref])


@pytest.fixture
def supa(monkeypatch):
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers import oauth_http

    fake = FakeSupabase()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_SUPABASE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_SUPABASE_CLIENT_SECRET", SECRET)
    field_options.clear_cache()
    yield fake
    field_options.clear_cache()


async def _connect(c, fake: FakeSupabase, pid: str | None) -> dict:
    body = {"pocket_id": pid} if pid else {}
    started = await c.post("/api/v1/integrations/supabase/connect", json=body)
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    location = urlsplit(start.headers["location"])
    assert (location.netloc, location.path) == ("api.supabase.com", "/v1/oauth/authorize")
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    fake.challenge = query["code_challenge"]
    cb = await c.get(
        "/api/v1/integrations/supabase/callback",
        params={"code": "good-code", "state": query["state"]},
    )
    assert cb.status_code == 200
    return {"authorize": query, "message": _message(cb.text)}


async def _linked_site(c, fake: FakeSupabase) -> str:
    pid = await _pocket()
    await _site(pid)
    msg = (await _connect(c, fake, pid))["message"]
    assert msg["status"] == "active" and "error" not in msg, msg
    return pid


def _put(pid: str) -> str:
    return f"/api/v1/sites/by-pocket/{pid}/integrations/supabase"


async def _deployed(pid: str) -> dict[str, str]:
    """What the Worker gets, minus the gateway token every link mints."""
    from pocketpaw_ee.sites import site_secrets

    found = await site_secrets.secrets_for_deploy(SimpleNamespace(workspace=WS, pocket_id=pid))
    return {k: v for k, v in found.items() if k != "PAW_GATEWAY_TOKEN"}


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn

    return await Conn.find_one(Conn.provider == "supabase")


# ------------------------------------------------------------------ connect


def test_unconfigured_without_both_env_vars(monkeypatch, supa):
    from pocketpaw_ee.integrations.providers.supabase import SupabaseProvider

    assert SupabaseProvider().availability() == "available"
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_SUPABASE_CLIENT_SECRET")
    assert SupabaseProvider().availability() == "unconfigured"


@pytest.mark.asyncio
async def test_connect_uses_pkce_and_the_organization_is_the_account(beanie_test_db, supa):
    async with _client() as c:
        [view] = [
            p
            for p in (await c.get("/api/v1/integrations/providers")).json()
            if p["id"] == "supabase"
        ]
        result = await _connect(c, supa, None)
        conns = (await c.get("/api/v1/integrations/connections")).json()
    q = result["authorize"]
    assert view["kind"] == "connect" and view["category"] == "database"
    assert view["plan"] == "site" and view["actions"] == []
    assert [f["key"] for f in view["settings_fields"]] == [
        "project_ref",
        "project_url",
        "admin_key",
        "kept_secrets",
    ]
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256" and q["code_challenge"]
    [exchange] = supa.token_forms
    assert exchange["grant_type"] == "authorization_code" and exchange["code_verifier"]
    conn = await _conn()
    assert conn.account_ref == ORG_ID and conn.account_label == "Acme"
    assert conn.expires_at is not None
    assert not any(v in json.dumps(conns) for v in SECRET_VALUES)


@pytest.mark.asyncio
async def test_refresh_rotates_and_a_dead_refresh_needs_reconnect(beanie_test_db, supa):
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.integrations import vault

    async with _client() as c:
        await _connect(c, supa, None)
    conn = await _conn()
    for expected in ("sbp-at-2", "sbp-at-3"):
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await conn.save()
        assert await vault.get_valid_token(conn) == expected
        conn = await _conn()
    assert crypto.decrypt_json(conn.token_ciphertext)["refresh_token"] == "sbp-rt-3"
    refreshes = [f for f in supa.token_forms if f["grant_type"] == "refresh_token"]
    assert [f["refresh_token"] for f in refreshes] == ["sbp-rt-1", "sbp-rt-2"]

    supa.refresh_tokens.clear()
    conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await conn.save()
    with pytest.raises(vault.NotConnected):
        await vault.get_valid_token(conn)
    assert (await _conn()).status == "needs_reconnect"


# ------------------------------------------------------------ settings + setup


@pytest.mark.asyncio
async def test_project_options_come_from_the_owners_projects(beanie_test_db, supa):
    async with _client() as c:
        pid = await _linked_site(c, supa)
        resp = await c.get(f"{_put(pid)}/fields/project_ref/options")
        bad = await c.put(_put(pid), json={"settings": {"project_ref": "zzzzzzzzzzzzzzzzzzzz"}})
        [view] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert resp.status_code == 200, resp.text
    assert resp.json()["options"] == [
        {"value": REF_A, "label": "Shop DB (eu-west-1)"},
        {"value": REF_B, "label": "Old DB (us-east-1)"},
    ]
    assert bad.status_code == 422
    assert view["status"] == "setup_required"  # no project picked yet
    assert await _deployed(pid) == {}


@pytest.mark.asyncio
async def test_setup_writes_the_url_and_anon_key_and_never_shows_them(beanie_test_db, supa, caplog):
    from pocketpaw_ee.sites import site_secrets

    caplog.set_level(logging.DEBUG)
    async with _client() as c:
        pid = await _linked_site(c, supa)
        saved = await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
        [view] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert saved.status_code == 200, saved.text
    assert view["status"] == "connected"
    assert view["settings"] == {
        "project_ref": REF_A,
        "project_url": f"https://{REF_A}.supabase.co",
        "kept_secrets": "",
    }
    assert supa.key_calls[-1] == {"ref": REF_A, "reveal": "true"}
    # Admin access is off by default: no service role key.
    assert await _deployed(pid) == {
        "SUPABASE_URL": f"https://{REF_A}.supabase.co",
        "SUPABASE_ANON_KEY": PUBLISHABLE_A,
    }
    listed = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert {s.name: s.managed_by for s in listed.secrets if s.managed_by} == {
        "SUPABASE_ANON_KEY": "integration:supabase",
        "SUPABASE_URL": "integration:supabase",
    }
    shown = json.dumps([view, listed.model_dump(mode="json"), saved.json()])
    logged = caplog.text
    for value in (PUBLISHABLE_A, SECRET_A):
        assert value not in shown and value not in logged


@pytest.mark.asyncio
async def test_admin_access_is_opt_in_and_switching_it_off_removes_the_key(
    beanie_test_db, supa, caplog
):
    caplog.set_level(logging.DEBUG)
    async with _client() as c:
        pid = await _linked_site(c, supa)
        await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
        on = await c.put(_put(pid), json={"settings": {"admin_key": "on"}})
        assert on.status_code == 200, on.text
        assert await _deployed(pid) == {
            "SUPABASE_URL": f"https://{REF_A}.supabase.co",
            "SUPABASE_ANON_KEY": PUBLISHABLE_A,
            "SUPABASE_SERVICE_ROLE_KEY": SECRET_A,
        }
        bad = await c.put(_put(pid), json={"settings": {"admin_key": "maybe"}})
        off = await c.put(_put(pid), json={"settings": {"admin_key": "off"}})
    assert bad.status_code == 422
    assert off.status_code == 200, off.text
    assert "SUPABASE_SERVICE_ROLE_KEY" not in await _deployed(pid)
    assert SECRET_A not in json.dumps([on.json(), off.json()]) and SECRET_A not in caplog.text


@pytest.mark.asyncio
async def test_an_owner_value_is_never_overwritten_and_survives_unlink(beanie_test_db, supa):
    from pocketpaw_ee.sites import site_secrets

    async with _client() as c:
        pid = await _linked_site(c, supa)
        await site_secrets.set_secret(
            workspace_id=WS,
            user_id=OWNER,
            pocket_id=pid,
            name="SUPABASE_URL",
            value="https://db.example.com",
        )
        # The agent asked for the anon key before the project was picked: filled.
        await site_secrets.request_secret(
            workspace_id=WS,
            user_id=OWNER,
            pocket_id=pid,
            name="SUPABASE_ANON_KEY",
            description="anon key",
        )
        saved = await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
        # A settings change re-runs setup: the owner's value still stands.
        rerun = await c.put(_put(pid), json={"settings": {"admin_key": "on"}})
        assert await _deployed(pid) == {
            "SUPABASE_URL": "https://db.example.com",
            "SUPABASE_ANON_KEY": PUBLISHABLE_A,
            "SUPABASE_SERVICE_ROLE_KEY": SECRET_A,
        }
        gone = await c.delete(_put(pid))
    assert saved.json()["settings"]["kept_secrets"] == "SUPABASE_URL"
    assert rerun.json()["settings"]["kept_secrets"] == "SUPABASE_URL"
    assert gone.status_code == 204
    assert await _deployed(pid) == {"SUPABASE_URL": "https://db.example.com"}


async def _setup_direct(pid: str):
    """The bound row, its provider and a setup context, to call the hook directly."""
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers import get_provider

    row = await SiteIntegration.find_one(
        SiteIntegration.pocket_id == pid, SiteIntegration.provider == "supabase"
    )
    ctx = field_options.setup_context(
        workspace_id=WS, pocket_id=pid, user_id=OWNER, provider="supabase", conn=await _conn()
    )
    return row, get_provider("supabase"), ctx


@pytest.mark.asyncio
async def test_no_project_drops_the_managed_secrets(beanie_test_db, supa):
    async with _client() as c:
        pid = await _linked_site(c, supa)
        await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
    row, provider, ctx = await _setup_direct(pid)
    row.settings = {}  # what a relink's drop_remote leaves
    assert await provider.site_setup(row, ctx) == {"kept_secrets": ""}
    assert await _deployed(pid) == {}


@pytest.mark.asyncio
async def test_a_setup_that_outlives_its_binding_undoes_its_writes(beanie_test_db, supa):
    async with _client() as c:
        pid = await _linked_site(c, supa)
        await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
    row, provider, ctx = await _setup_direct(pid)
    stale = row.model_copy()
    row.connection_id = "another-connection"  # relinked mid-setup
    await row.save()
    assert await provider.site_setup(stale, ctx) == {}
    assert await _deployed(pid) == {}

    await row.delete()  # unlinked mid-setup
    assert await provider.site_setup(stale, ctx) == {}
    assert await _deployed(pid) == {}


@pytest.mark.asyncio
async def test_managed_writes_and_deletes_are_audited_by_name(beanie_test_db, supa, monkeypatch):
    from pocketpaw_ee.guards import audit

    seen: list[dict] = []
    monkeypatch.setattr(audit, "log_privileged_action", lambda **kw: seen.append(kw))
    async with _client() as c:
        pid = await _linked_site(c, supa)
        await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
        await c.delete(_put(pid))
    managed = [e for e in seen if "managed" in e["action"]]
    assert sorted((e["action"], e["secret_name"]) for e in managed) == [
        ("sites.secret.delete_managed", "SUPABASE_ANON_KEY"),
        ("sites.secret.delete_managed", "SUPABASE_URL"),
        ("sites.secret.set_managed", "SUPABASE_ANON_KEY"),
        ("sites.secret.set_managed", "SUPABASE_URL"),
    ]
    assert all(e["actor"] == "integration:supabase" for e in managed)
    assert PUBLISHABLE_A not in json.dumps(seen, default=str)


@pytest.mark.asyncio
async def test_a_project_change_overwrites_and_drops_what_the_new_project_lacks(
    beanie_test_db, supa
):
    async with _client() as c:
        pid = await _linked_site(c, supa)
        await c.put(_put(pid), json={"settings": {"project_ref": REF_A, "admin_key": "on"}})
        again = await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
        assert again.status_code == 200 and await _deployed(pid) == {
            "SUPABASE_URL": f"https://{REF_A}.supabase.co",
            "SUPABASE_ANON_KEY": PUBLISHABLE_A,
            "SUPABASE_SERVICE_ROLE_KEY": SECRET_A,
        }
        moved = await c.put(_put(pid), json={"settings": {"project_ref": REF_B}})
    assert moved.status_code == 200, moved.text
    assert moved.json()["settings"]["project_url"] == f"https://{REF_B}.supabase.co"
    assert await _deployed(pid) == {
        "SUPABASE_URL": f"https://{REF_B}.supabase.co",
        "SUPABASE_ANON_KEY": ANON_B,
    }


@pytest.mark.asyncio
async def test_unlink_deletes_only_the_secrets_it_wrote(beanie_test_db, supa):
    from pocketpaw_ee.sites import site_secrets

    async with _client() as c:
        pid = await _linked_site(c, supa)
        await site_secrets.set_secret(
            workspace_id=WS, user_id=OWNER, pocket_id=pid, name="RESEND_API_KEY", value="re_x"
        )
        await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
        # The owner takes one over: it is theirs from now on.
        await site_secrets.set_secret(
            workspace_id=WS,
            user_id=OWNER,
            pocket_id=pid,
            name="SUPABASE_URL",
            value="https://db.example.com",
        )
        gone = await c.delete(_put(pid))
    assert gone.status_code == 204
    assert await _deployed(pid) == {
        "RESEND_API_KEY": "re_x",
        "SUPABASE_URL": "https://db.example.com",
    }


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_a_403_is_setup_required(beanie_test_db, supa):
    async with _client() as c:
        pid = await _linked_site(c, supa)
        supa.access.clear()  # Supabase stops accepting the access token
        ok = await c.put(_put(pid), json={"settings": {"project_ref": REF_A}})
        assert ok.status_code == 200, ok.text
        assert [f["grant_type"] for f in supa.token_forms][-1] == "refresh_token"
        assert (await _deployed(pid))["SUPABASE_ANON_KEY"] == PUBLISHABLE_A

        supa.deny_keys = True
        denied = await c.put(_put(pid), json={"settings": {"project_ref": REF_B}})
        [view] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert denied.status_code == 200, denied.text
    assert view["status"] == "setup_required"
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    binding = await SiteIntegration.find_one(
        SiteIntegration.pocket_id == pid, SiteIntegration.provider == "supabase"
    )
    assert binding.setup_error == "supabase_keys_forbidden"


@pytest.mark.asyncio
async def test_the_agent_tool_points_at_the_recipe(beanie_test_db, supa, monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import sites_create

    async def _plan_ok(_ws, **_kw):
        return None

    monkeypatch.setattr(sites_create, "_identity", lambda: (WS, OWNER))
    monkeypatch.setattr(sites_create, "_require_sites_plan_or_error", _plan_ok)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.push_sse_event", lambda *a, **k: None
    )
    pid = await _pocket()
    result = await sites_create._connect_site_integration_handler(
        {"pocket_id": pid, "provider": "supabase", "purpose": "Store sign-ups."}
    )
    text = result["content"][0]["text"]
    assert "SUPABASE_ANON_KEY" in text and "supabase recipe" in text
    assert "Admin access" in text and "Paw.call" not in text
