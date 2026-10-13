# tests/ee/sites/test_integrations_turnstile.py: the built-in ``turnstile`` integration.
#
# Covers the per-site widget on our Cloudflare account (create on link with the
# site's deployed + live hosts, re-domain only on change, the 10-host cap, delete on
# unlink, unconfigured / unpublished / CF-error postures), the sitekey being
# server-held (kept across an owner PUT, never in a view) and the secret encrypted,
# the gateway verifying writes against the site's own secret and hostnames,
# and ``siteConfig.turnstileSiteKey`` for the generator. The Cloudflare API is an
# ``httpx.MockTransport``; no network anywhere.
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    SITE_HOST,
    WS,
    _client,
    _code,
    _env,
    _gw,
    _pocket,
    _site,
)

SITE_URL = "https://shop-abc.pawsites.app"


class _FakeCF:
    """Records Turnstile widget calls and answers like the Cloudflare API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer cf-token"
        if self.fail:
            return httpx.Response(403, json={"success": False, "errors": [{"code": 10000}]})
        if request.method == "POST":
            n = len([c for c in self.calls if c[0] == "POST"])
            result = {"sitekey": f"0x4AAA-site-{n}", "secret": f"0x4AAA-secret-{n}", **body}
            return httpx.Response(200, json={"success": True, "result": result})
        return httpx.Response(200, json={"success": True, "result": body or {}})


@pytest.fixture
def cf(monkeypatch):
    from pocketpaw_ee.integrations.providers import turnstile

    fake = _FakeCF()
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("PAW_CF_API_TOKEN", "cf-token")
    monkeypatch.setattr(turnstile, "_transport", httpx.MockTransport(fake.handler))
    return fake


async def _deployed_site(pid: str, *, domains=()):
    from pocketpaw_ee.cloud.models.site import SiteDomain

    site = await _site(pid)
    site.url = SITE_URL
    site.domains = [SiteDomain(hostname=h, status=s) for h, s in domains]
    await site.save()
    return site


async def _put(pid: str, **body):
    async with _client() as c:
        resp = await c.put(f"/api/v1/sites/by-pocket/{pid}/integrations/turnstile", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _row(pid: str):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    return await SiteIntegration.find_one(
        SiteIntegration.pocket_id == pid, SiteIntegration.provider == "turnstile"
    )


# ------------------------------------------------------------------ provider


@pytest.mark.asyncio
async def test_unconfigured_without_cloudflare_credentials(beanie_test_db, monkeypatch):
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_CF_API_TOKEN", raising=False)
    async with _client() as c:
        providers = (await c.get("/api/v1/integrations/providers")).json()
    view = next(p for p in providers if p["id"] == "turnstile")
    assert {k: view[k] for k in ("kind", "category", "plan", "availability", "actions")} == {
        "kind": "builtin",
        "category": "security",
        "plan": "free",
        "availability": "unconfigured",
        "actions": [],
    }
    # The sitekey fields are hidden: nothing for the owner to fill in.
    assert view["settings_fields"] == [] and view["requires_connection"] is False

    pid = await _pocket()
    await _deployed_site(pid)
    linked = await _put(pid)  # linking still works; no widget, no call
    assert linked["status"] == "connected" and linked["settings"] == {}
    assert not (await _row(pid)).secret_ciphertext


@pytest.mark.asyncio
async def test_link_creates_a_widget_for_the_sites_hosts_and_hides_the_sitekey(beanie_test_db, cf):
    from pocketpaw_ee.cloud._core import crypto

    async with _client() as c:
        providers = (await c.get("/api/v1/integrations/providers")).json()
    assert next(p for p in providers if p["id"] == "turnstile")["availability"] == "available"

    pid = await _pocket()
    site = await _deployed_site(
        pid, domains=[("www.shop.com", "live"), ("pending.shop.com", "verifying")]
    )
    view = await _put(pid)
    assert view["status"] == "connected"
    assert view["settings"] == {} and view["enabled_actions"] == []
    assert cf.calls == [
        (
            "POST",
            "/client/v4/accounts/acct123/challenges/widgets",
            {
                "name": f"paw-site-{site.id}",
                "domains": ["shop-abc.pawsites.app", "www.shop.com"],
                "mode": "managed",
            },
        )
    ]
    row = await _row(pid)
    assert row.settings["sitekey"] == "0x4AAA-site-1"
    assert "0x4AAA-secret-1" not in json.dumps(row.settings)
    assert crypto.decrypt(row.secret_ciphertext) == "0x4AAA-secret-1"

    assert row.settings["widget_domains"] == "shop-abc.pawsites.app,www.shop.com"

    # An owner PUT can neither see, replace nor wipe the server-held keys.
    again = await _put(pid, settings={"sitekey": "attacker", "widget_domains": ""})
    assert again["settings"] == {}
    row = await _row(pid)
    assert row.settings["sitekey"] == "0x4AAA-site-1"
    assert row.settings["widget_domains"] == "shop-abc.pawsites.app,www.shop.com"
    async with _client() as c:
        listed = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert "0x4AAA" not in json.dumps(listed)
    assert len(cf.calls) == 1  # same hosts: no second Cloudflare call


@pytest.mark.asyncio
async def test_new_hosts_redomain_the_widget_capped_at_ten(beanie_test_db, cf, caplog):
    from pocketpaw_ee.cloud.models.site import Site, SiteDomain
    from pocketpaw_ee.integrations.providers.turnstile import site_key_for_build

    pid = await _pocket()
    site = await _deployed_site(pid)
    await _put(pid)
    site = await Site.get(site.id)
    site.domains = [SiteDomain(hostname=f"d{i}.shop.com", status="live") for i in range(12)]
    await site.save()
    await _put(pid)  # nothing changed: setup does not re-run on a no-op PUT
    assert len(cf.calls) == 1
    assert await site_key_for_build(WS, pid) == "0x4AAA-site-1"  # a publish re-syncs
    method, path, body = cf.calls[-1]
    assert (method, path) == ("PUT", "/client/v4/accounts/acct123/challenges/widgets/0x4AAA-site-1")
    assert body["domains"] == ["shop-abc.pawsites.app"] + [f"d{i}.shop.com" for i in range(9)]
    assert body["mode"] == "managed"
    assert "keeps the first 10" in caplog.text
    assert (await _row(pid)).settings["widget_domains"] == ",".join(body["domains"])


@pytest.mark.asyncio
async def test_unpublished_site_gets_its_widget_on_the_next_publish(beanie_test_db, cf):
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites.service import _turnstile_build_kwargs

    pid = await _pocket()
    site = await _site(pid)  # no deployed url yet; allowed_origins is not a host source
    await _put(pid)
    assert cf.calls == []
    assert await _turnstile_build_kwargs(WS, pid) == {}

    site = await Site.get(site.id)
    site.url = SITE_URL
    await site.save()
    assert await _turnstile_build_kwargs(WS, pid) == {"turnstile_site_key": "0x4AAA-site-1"}
    assert cf.calls[0][2]["domains"] == ["shop-abc.pawsites.app"]
    other = await _pocket()
    assert await _turnstile_build_kwargs(WS, other) == {}  # not linked


@pytest.mark.asyncio
async def test_cloudflare_error_leaves_the_site_setup_required(beanie_test_db, cf):
    from pocketpaw_ee.sites.service import _turnstile_build_kwargs

    cf.fail = True
    pid = await _pocket()
    await _deployed_site(pid)
    view = await _put(pid)  # the link itself still succeeds
    assert view["status"] == "setup_required"
    row = await _row(pid)
    assert not row.secret_ciphertext and "sitekey" not in row.settings
    assert row.setup_error == "setup_failed"
    assert await _turnstile_build_kwargs(WS, pid) == {}  # a publish is not blocked

    cf.fail = False  # the next publish finishes the setup (POSTs 1-2 were refused)
    assert await _turnstile_build_kwargs(WS, pid) == {"turnstile_site_key": "0x4AAA-site-3"}
    async with _client() as c:
        listed = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert listed[0]["status"] == "connected"


@pytest.mark.asyncio
async def test_unlink_and_site_purge_delete_the_widget(beanie_test_db, cf):
    from pocketpaw_ee.integrations import bindings

    pid = await _pocket()
    await _deployed_site(pid)
    await _put(pid)
    async with _client() as c:
        resp = await c.delete(f"/api/v1/sites/by-pocket/{pid}/integrations/turnstile")
    assert resp.status_code == 204
    assert cf.calls[-1][:2] == (
        "DELETE",
        "/client/v4/accounts/acct123/challenges/widgets/0x4AAA-site-1",
    )

    await _put(pid)
    await bindings.purge_for_pocket(workspace_id=WS, pocket_id=pid)
    assert cf.calls[-1][:2] == (
        "DELETE",
        "/client/v4/accounts/acct123/challenges/widgets/0x4AAA-site-2",
    )
    assert await _row(pid) is None


# ------------------------------------------------------------------- gateway


@pytest.mark.asyncio
async def test_gateway_verifies_writes_against_the_sites_widget(beanie_test_db, cf, monkeypatch):
    from pocketpaw_ee.cloud._core import turnstile
    from pocketpaw_ee.cloud._core.errors import BadRequest
    from pocketpaw_ee.cloud.models.site import SiteDomain

    seen: list[dict] = []

    async def _verify(token, remote_ip, *, code, transport=None, **kw):
        seen.append({"token": token, **kw})
        if token != "good":
            raise BadRequest(code, "no")

    monkeypatch.setattr(turnstile, "verify_turnstile", _verify)
    pid = await _pocket()
    site = await _deployed_site(pid, domains=[("www.shop.com", "live")])
    site.domains.append(SiteDomain(hostname=SITE_HOST, status="live"))
    await site.save()
    async with _client() as c:
        await c.put(f"/api/v1/sites/by-pocket/{pid}/integrations/newsletter", json={})
    await _put(pid)
    sid = str(site.id)
    async with _client() as c:
        # The platform secret is NOT configured (_env), yet the site's widget applies.
        missing = await _gw(c, sid, "newsletter", "subscribe", args={"email": "a@b.co"})
        bad = await _gw(
            c, sid, "newsletter", "subscribe", args={"email": "a@b.co"}, turnstile_token="bad"
        )
        ok = await _gw(
            c, sid, "newsletter", "subscribe", args={"email": "a@b.co"}, turnstile_token="good"
        )
    assert (missing.status_code, _code(missing)) == (403, "turnstile_required")
    assert (bad.status_code, _code(bad)) == (403, "turnstile_required")
    assert ok.status_code == 200 and ok.json()["result"] == {"subscribed": True}
    assert [s["secret"] for s in seen] == ["0x4AAA-secret-1", "0x4AAA-secret-1"]
    assert set(seen[1]["expected_hostnames"]) == {
        "shop-abc.pawsites.app",
        "www.shop.com",
        SITE_HOST,
    }


@pytest.mark.asyncio
async def test_without_the_binding_the_gateway_keeps_the_platform_rule(beanie_test_db, cf):
    pid = await _pocket()
    site = await _deployed_site(pid)
    async with _client() as c:
        await c.put(f"/api/v1/sites/by-pocket/{pid}/integrations/newsletter", json={})
        ok = await _gw(c, str(site.id), "newsletter", "subscribe", args={"email": "a@b.co"})
    assert ok.status_code == 200  # platform secret unset in tests -> no check


def _siteverify(answer: dict, sent: list):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json=answer)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_verify_turnstile_uses_the_given_secret_and_checks_the_hostname():
    from pocketpaw_ee.cloud._core.errors import BadRequest
    from pocketpaw_ee.cloud._core.turnstile import verify_turnstile

    sent: list = []
    good = _siteverify({"success": True, "hostname": "WWW.shop.com"}, sent)
    await verify_turnstile(
        "tok", "1.2.3.4", code="x", transport=good, secret="s1", expected_hostnames=["www.shop.com"]
    )
    assert sent[0]["secret"] == ["s1"] and sent[0]["response"] == ["tok"]

    foreign = _siteverify({"success": True, "hostname": "evil.test"}, sent)
    with pytest.raises(BadRequest):
        await verify_turnstile(
            "tok", None, code="x", transport=foreign, secret="s1", expected_hostnames=["a.com"]
        )
    with pytest.raises(BadRequest):  # an explicit empty secret never falls back
        await verify_turnstile("tok", None, code="x", transport=good, secret="  ")


# ------------------------------------------------------------- siteConfig


def test_generator_input_carries_the_turnstile_sitekey_only_when_set():
    from pocketpaw_ee.sites.generator_client import build_generator_input

    base = {
        "engine": "html",
        "theme": {},
        "site_id": "s1",
        "title": "Shop",
        "capture_api_base": "https://api.example.com",
        "capture_signed_key": "site_key_x",
        "source": {"index.html": "<h1>hi</h1>"},
    }
    assert "turnstileSiteKey" not in build_generator_input(**base)["siteConfig"]
    with_key = build_generator_input(**base, turnstile_site_key="0x4AAA-site-1")
    assert with_key["siteConfig"]["turnstileSiteKey"] == "0x4AAA-site-1"


@pytest.mark.asyncio
async def test_build_passes_the_sitekey_to_the_generator():
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    class _Runner:
        input_json: dict | None = None

        async def generate(self, input_json, out_dir):
            self.input_json = input_json
            return {"projectDir": "/tmp/site", "rippleVersion": "0.2.0"}

        async def install(self, project_dir):
            return True, "ok"

        async def smoke(self, project_dir):
            return True, "ok"

    runner = _Runner()
    await GeneratorClient(_runner=runner).build(
        theme={},
        site_id="s1",
        title="Shop",
        capture_api_base="https://api.example.com",
        capture_signed_key="site_key_x",
        turnstile_site_key="0x4AAA-site-1",
        smoke=False,
        static_build=False,
    )
    assert runner.input_json["siteConfig"]["turnstileSiteKey"] == "0x4AAA-site-1"
