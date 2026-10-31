# tests/ee/sites/test_owner_sign_in.py: "Open owner page" (integrations/owner_sign_in.py).
#
# A Paw user with edit access to a published site gets a one-time link that signs them
# in to the site's own better-auth as its owner (the pocket owner) or staff (anyone
# else with edit access). The site's Worker trades the ticket for the identity with its
# PAW_GATEWAY_TOKEN. Negative paths: replay, another site's ticket, a wrong or missing
# token, an expired or tampered ticket, an open redirect, no access, unpublished.
# Plus the deploy side: a Worker holding the gateway token gets PAW_API_BASE and
# PAW_SITE_ID, and a build that requests the token gets one minted.
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    EDITOR,
    OTHER_WS,
    OWNER,
    WS,
    _env,
    _Membership,
    _pocket,
)

HOST = "pickles.pawsites.example"
CALLBACK = "/api/paw-owner/callback"


async def _site(pocket_id: str, *, url: str = f"https://{HOST}", script: str = "pickles-1"):
    from pocketpaw_ee.cloud.models.site import Site

    site = Site(workspace=WS, pocket_id=pocket_id, owner=OWNER, script_name=script, url=url)
    await site.insert()
    return site


def _app(user_id: str, *, workspace_id: str = WS, email: str = "asha@example.com") -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.integrations.router import public_router, router

    user = SimpleNamespace(
        id=user_id,
        email=email,
        full_name="Asha Rao",
        active_workspace=workspace_id,
        workspaces=[_Membership(workspace_id)],
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


def _client(user_id: str = OWNER, **kw) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(user_id, **kw)), base_url="https://t")


async def _link(c: AsyncClient, pocket_id: str, **body):
    return await c.post(f"/api/v1/sites/by-pocket/{pocket_id}/owner-sign-in", json=body)


def _ticket(resp) -> str:
    assert resp.status_code == 200, resp.text
    return parse_qs(urlsplit(resp.json()["url"]).query)["ticket"][0]


async def _token(site) -> str:
    from pocketpaw_ee.sites import site_secrets

    return (await site_secrets.secrets_for_deploy(site))["PAW_GATEWAY_TOKEN"]


async def _redeem(c: AsyncClient, token: str | None, **body):
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await c.post("/api/v1/integrations/owner-sign-in/redeem", json=body, headers=headers)


def _err(resp) -> str:
    body = resp.json()
    assert body["ok"] is False, body
    return body["error"]["code"]


# --------------------------------------------------------------------- issue


@pytest.mark.asyncio
async def test_the_owner_gets_a_one_time_link_to_the_sites_own_callback(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        resp = await _link(c, pid, next="/admin")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    url = urlsplit(body["url"])
    assert (url.scheme, url.netloc, url.path) == ("https", HOST, CALLBACK)
    q = parse_qs(url.query)
    assert q["next"] == ["/admin"] and len(q["ticket"][0]) >= 32
    assert body["role"] == "owner" and body["expires_in"] == 60
    assert resp.headers["cache-control"] == "no-store"
    # The site needs its gateway token to redeem: minted with the first link.
    assert await _token(site)


@pytest.mark.asyncio
async def test_a_live_custom_domain_is_preferred(beanie_test_db):
    from pocketpaw_ee.cloud.models.site import SiteDomain

    pid = await _pocket()
    site = await _site(pid)
    site.domains = [
        SiteDomain(hostname="pending.asha.test", status="pending"),
        SiteDomain(hostname="shop.asha.test", status="live"),
    ]
    await site.save()
    async with _client() as c:
        resp = await _link(c, pid)
    assert urlsplit(resp.json()["url"]).netloc == "shop.asha.test"
    assert parse_qs(urlsplit(resp.json()["url"]).query)["next"] == ["/"]


@pytest.mark.asyncio
async def test_an_editor_gets_a_staff_link(beanie_test_db):
    pid = await _pocket()
    await _site(pid)
    async with _client(EDITOR, email="ravi@example.com") as c:
        ticket = _ticket(await _link(c, pid))
        assert (await _link(c, pid)).json()["role"] == "staff"
    site = await _site_doc(pid)
    async with _client() as c:
        resp = await _redeem(c, await _token(site), site_id=str(site.id), ticket=ticket)
    assert resp.json()["result"]["role"] == "staff"
    assert resp.json()["result"]["email"] == "ravi@example.com"


async def _site_doc(pid: str):
    from pocketpaw_ee.cloud.models.site import Site

    return await Site.find_one(Site.pocket_id == pid)


@pytest.mark.asyncio
async def test_no_link_without_access_or_for_another_workspace(beanie_test_db):
    private = await _pocket()
    await _site(private)
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = await Pocket.get(private)
    doc.visibility = "private"
    await doc.save()
    foreign = await _pocket(workspace=OTHER_WS)
    async with _client(EDITOR) as c:
        denied = await _link(c, private)
    async with _client() as c:
        missing = await _link(c, foreign)
        malformed = await _link(c, "not-an-id")
    assert denied.status_code == 403
    assert missing.status_code == 404 and malformed.status_code == 404


@pytest.mark.asyncio
async def test_an_unpublished_site_has_no_owner_page(beanie_test_db):
    pid = await _pocket()
    async with _client() as c:
        none = await _link(c, pid)
    await _site(pid, url="")
    async with _client() as c:
        no_url = await _link(c, pid)
    assert none.status_code == 409 and no_url.status_code == 409
    assert none.json()["error"]["code"] == "sites.owner_page_unpublished"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["https://evil.test/", "//evil.test", "/\\evil.test", "admin"])
async def test_next_must_be_a_path_on_the_site(beanie_test_db, bad):
    pid = await _pocket()
    await _site(pid)
    async with _client() as c:
        resp = await _link(c, pid, next=bad)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_an_account_without_an_email_cannot_open_the_owner_page(beanie_test_db):
    pid = await _pocket()
    await _site(pid)
    async with _client(email="") as c:
        resp = await _link(c, pid)
    assert resp.status_code == 422


# -------------------------------------------------------------------- redeem


@pytest.mark.asyncio
async def test_redeem_returns_the_owner_once(beanie_test_db, caplog):
    caplog.set_level(logging.DEBUG)
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        ticket = _ticket(await _link(c, pid))
        token = await _token(site)
        first = await _redeem(c, token, site_id=str(site.id), ticket=ticket)
        replay = await _redeem(c, token, site_id=str(site.id), ticket=ticket)
    assert first.status_code == 200, first.text
    result = first.json()["result"]
    assert result["role"] == "owner"
    assert result["email"] == "asha@example.com" and result["name"] == "Asha Rao"
    assert len(result["account_id"]) == 64 and OWNER not in result["account_id"]
    assert first.headers["cache-control"] == "no-store"
    assert (replay.status_code, _err(replay)) == (400, "invalid_ticket")
    # Neither the ticket nor the token is ever logged.
    assert ticket not in caplog.text and token not in caplog.text


@pytest.mark.asyncio
async def test_the_account_id_is_stable_per_site_and_person(beanie_test_db):
    async def _account(user: str, pid: str, site) -> str:
        async with _client(user) as c:
            ticket = _ticket(await _link(c, pid))
            resp = await _redeem(c, await _token(site), site_id=str(site.id), ticket=ticket)
        return resp.json()["result"]["account_id"]

    pid_a, pid_b = await _pocket(), await _pocket()
    site_a = await _site(pid_a)
    site_b = await _site(pid_b, url="https://other.pawsites.example", script="other-1")
    a1 = await _account(OWNER, pid_a, site_a)
    assert a1 == await _account(OWNER, pid_a, site_a)
    assert a1 != await _account(OWNER, pid_b, site_b)
    assert a1 != await _account(EDITOR, pid_a, site_a)


@pytest.mark.asyncio
async def test_another_sites_ticket_is_refused_and_burned(beanie_test_db):
    pid_a, pid_b = await _pocket(), await _pocket()
    site_a = await _site(pid_a)
    site_b = await _site(pid_b, url="https://other.pawsites.example", script="other-1")
    async with _client() as c:
        ticket = _ticket(await _link(c, pid_a))
        await _link(c, pid_b)  # site B has its own gateway token
        cross = await _redeem(c, await _token(site_b), site_id=str(site_b.id), ticket=ticket)
        after = await _redeem(c, await _token(site_a), site_id=str(site_a.id), ticket=ticket)
    assert (cross.status_code, _err(cross)) == (400, "invalid_ticket")
    assert (after.status_code, _err(after)) == (400, "invalid_ticket")


@pytest.mark.asyncio
async def test_redeem_needs_the_sites_own_gateway_token(beanie_test_db):
    pid_a, pid_b = await _pocket(), await _pocket()
    site_a = await _site(pid_a)
    site_b = await _site(pid_b, url="https://other.pawsites.example", script="other-1")
    async with _client() as c:
        ticket = _ticket(await _link(c, pid_a))
        await _link(c, pid_b)
        sid = str(site_a.id)
        none = await _redeem(c, None, site_id=sid, ticket=ticket)
        wrong = await _redeem(c, "x" * 43, site_id=sid, ticket=ticket)
        other = await _redeem(c, await _token(site_b), site_id=sid, ticket=ticket)
        # A refused token never spends the ticket: the real site still can.
        ok = await _redeem(c, await _token(site_a), site_id=sid, ticket=ticket)
    for resp in (none, wrong, other):
        assert (resp.status_code, _err(resp)) == (401, "unauthorized")
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_an_expired_or_tampered_ticket_is_refused(beanie_test_db):
    from pocketpaw_ee.cloud._core import redis_client

    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        expired = _ticket(await _link(c, pid))
        await redis_client.get_redis().delete(f"site_owner_ticket_state:{expired}")  # TTL elapsed
        live = _ticket(await _link(c, pid))
        token = await _token(site)
        sid = str(site.id)
        r_expired = await _redeem(c, token, site_id=sid, ticket=expired)
        r_tampered = await _redeem(c, token, site_id=sid, ticket=live[:-2] + "zz")
        r_bad = await _redeem(c, token, site_id=sid)
    assert (r_expired.status_code, _err(r_expired)) == (400, "invalid_ticket")
    assert (r_tampered.status_code, _err(r_tampered)) == (400, "invalid_ticket")
    assert (r_bad.status_code, _err(r_bad)) == (400, "invalid_request")


@pytest.mark.asyncio
async def test_the_ticket_lives_sixty_seconds(beanie_test_db):
    from pocketpaw_ee.cloud._core import redis_client

    pid = await _pocket()
    await _site(pid)
    async with _client() as c:
        ticket = _ticket(await _link(c, pid))
    ttl = await redis_client.get_redis().ttl(f"site_owner_ticket_state:{ticket}")
    assert 0 < ttl <= 60


@pytest.mark.asyncio
async def test_a_revoked_site_cannot_redeem(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        ticket = _ticket(await _link(c, pid))
        token = await _token(site)
        site.revoked = True
        await site.save()
        resp = await _redeem(c, token, site_id=str(site.id), ticket=ticket)
    assert (resp.status_code, _err(resp)) == (401, "unauthorized")


@pytest.mark.asyncio
async def test_redeem_is_capped_per_site(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import owner_sign_in

    monkeypatch.setattr(owner_sign_in, "REDEEM_SITE_PER_MIN", 2)
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        await _link(c, pid)
        token = await _token(site)
        codes = [
            (await _redeem(c, token, site_id=str(site.id), ticket="t" * 43)).status_code
            for _ in range(3)
        ]
    assert codes == [400, 400, 429]


# -------------------------------------------------------------------- deploy


def _build(root: Path, requests: list[dict]) -> Path:
    (root / "worker").mkdir(parents=True, exist_ok=True)
    (root / "worker" / "index.js").write_text("export default { fetch(r, env) {} };\n")
    manifest = {
        "workerEntry": "worker/index.js",
        "mainModule": "index.js",
        "workerModules": ["worker/index.js"],
        "compat": {"date": "2026-09-01", "flags": []},
        "bindingRequests": requests,
    }
    (root / "paw-build.json").write_text(json.dumps(manifest))
    return root


class _CF:
    def __init__(self):
        self.puts: list[dict] = []

    async def put_worker(self, **kw):
        self.puts.append(kw)
        return True


@pytest.mark.asyncio
async def test_a_build_asking_for_the_gateway_token_gets_it_and_the_paw_vars(
    beanie_test_db, tmp_path
):
    from pocketpaw_ee.sites import service as sites_service

    pid = await _pocket()
    site = await _site(pid)
    cf = _CF()
    await sites_service.deploy_bundle(
        site,
        _build(tmp_path, [{"type": "secret", "name": "PAW_GATEWAY_TOKEN", "required": True}]),
        cloudflare=cf,
    )
    by_name = {b["name"]: b for b in cf.puts[0]["bindings"]}
    assert by_name["PAW_GATEWAY_TOKEN"]["type"] == "secret_text"
    assert by_name["PAW_GATEWAY_TOKEN"]["text"] == await _token(site)
    assert by_name["PAW_SITE_ID"] == {
        "type": "plain_text",
        "name": "PAW_SITE_ID",
        "text": str(site.id),
    }
    assert by_name["PAW_API_BASE"]["text"] == "https://connect.example.com/api/v1"


@pytest.mark.asyncio
async def test_a_worker_without_the_gateway_token_gets_no_paw_vars(beanie_test_db, tmp_path):
    from pocketpaw_ee.sites import service as sites_service

    pid = await _pocket()
    site = await _site(pid)
    cf = _CF()
    await sites_service.deploy_bundle(site, _build(tmp_path, []), cloudflare=cf)
    assert cf.puts[0]["bindings"] == []


def test_an_assets_only_worker_gets_neither_the_token_nor_its_vars():
    from pocketpaw_ee.sites import bundle_deploy
    from pocketpaw_ee.sites.bundle_deploy import PawBundle, ProvisionedResources

    bundle = PawBundle(
        main_module=None,
        modules=[],
        assets={"/index.html": b"<p>hi</p>"},
        assets_config={},
        compatibility_date="2026-09-01",
        compatibility_flags=[],
        bindings=[],
    )
    provisioned = ProvisionedResources(
        secrets={"PAW_GATEWAY_TOKEN": "t" * 43},
        plain_text={"PAW_API_BASE": "https://x/api/v1", "PAW_SITE_ID": "s1", "OTHER": "kept"},
    )
    bundle_deploy._map_into(bundle, {}, provisioned)
    assert [b["name"] for b in bundle.bindings] == ["OTHER"]
