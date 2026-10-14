# tests/ee/sites/test_integrations_turnstile.py: the built-in ``turnstile`` integration.
#
# Covers the per-site widget on our Cloudflare account (create on link with the
# site's deployed + live hosts, re-domain only on change, the 10-host cap, delete on
# unlink / purge even when Cloudflare refuses, unconfigured / unpublished / CF-error
# postures, the concurrent-setup race), the sitekey being server-held (never in a
# view, never wiped by a stale write) and the secret encrypted, ``deployed_sitekey``
# gating enforcement (a link after deploy is not enforced until a publish carries
# the widget), the gateway verifying writes against the site's own secret and
# hostnames (fails closed on an unreadable secret), and
# ``siteConfig.turnstileSiteKey`` for the generator. The Cloudflare API is an
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
WIDGETS = "/client/v4/accounts/acct123/challenges/widgets"


class _FakeCF:
    """Records Turnstile widget calls and answers like the Cloudflare API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = False
        self.fail_delete = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer cf-token"
        if self.fail or (self.fail_delete and request.method == "DELETE"):
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


async def _status(pid: str) -> str:
    async with _client() as c:
        listed = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    return next(v["status"] for v in listed if v["provider"] == "turnstile")


async def _published(pid: str, sitekey: str) -> None:
    """What a successful publish whose build carried ``sitekey`` does."""
    from pocketpaw_ee.integrations.providers.turnstile import after_publish

    await after_publish(WS, pid, sitekey)


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
async def test_link_creates_a_widget_and_is_setup_required_until_a_publish_carries_it(
    beanie_test_db, cf
):
    from pocketpaw_ee.cloud._core import crypto

    async with _client() as c:
        providers = (await c.get("/api/v1/integrations/providers")).json()
    assert next(p for p in providers if p["id"] == "turnstile")["availability"] == "available"

    pid = await _pocket()
    site = await _deployed_site(
        pid, domains=[("www.shop.com", "live"), ("pending.shop.com", "verifying")]
    )
    view = await _put(pid)
    assert view["status"] == "setup_required"  # the live page has no widget yet
    assert view["settings"] == {} and view["enabled_actions"] == []
    assert cf.calls == [
        (
            "POST",
            WIDGETS,
            {
                "name": f"paw-site-{site.id}",
                "domains": ["shop-abc.pawsites.app", "www.shop.com"],
                "mode": "managed",
            },
        )
    ]
    row = await _row(pid)
    assert row.settings["sitekey"] == "0x4AAA-site-1"
    assert row.settings["widget_domains"] == "shop-abc.pawsites.app,www.shop.com"
    assert "0x4AAA-secret-1" not in json.dumps(row.settings)
    assert crypto.decrypt(row.secret_ciphertext) == "0x4AAA-secret-1"

    await _published(pid, "0x4AAA-site-1")
    assert (await _row(pid)).deployed_sitekey == "0x4AAA-site-1"
    assert await _status(pid) == "connected"

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
async def test_a_stale_write_cannot_wipe_the_secret_or_the_sitekey(beanie_test_db, cf):
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.integrations import bindings
    from pocketpaw_ee.integrations.providers.turnstile import site_key_for_build

    pid = await _pocket()
    await _site(pid)  # not deployed: link creates no widget
    await _put(pid)
    stale = await _row(pid)  # loaded before setup wrote anything
    before = dict(stale.settings or {})

    doc = await Site.find_one(Site.pocket_id == pid)
    doc.url = SITE_URL
    await doc.save()
    assert await site_key_for_build(WS, pid) == "0x4AAA-site-1"

    stale.purpose = "stale"
    await bindings._persist(stale, before, "purpose", "settings", "enabled_actions")
    row = await _row(pid)
    assert row.purpose == "stale"
    assert row.secret_ciphertext and row.settings["sitekey"] == "0x4AAA-site-1"


@pytest.mark.asyncio
async def test_concurrent_setups_keep_one_widget_and_a_matching_secret(beanie_test_db, cf):
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.integrations.providers.base import SetupContext
    from pocketpaw_ee.integrations.providers.turnstile import TurnstileProvider

    pid = await _pocket()
    await _site(pid)
    await _put(pid)  # no url: no widget yet
    first, second = await _row(pid), await _row(pid)  # both see "no secret"
    doc = await Site.find_one(Site.pocket_id == pid)
    doc.url = SITE_URL
    await doc.save()

    ctx = SetupContext(workspace_id=WS, pocket_id=pid, user_id="", provider="turnstile")
    provider = TurnstileProvider()
    won = await provider.site_setup(first, ctx)
    lost = await provider.site_setup(second, ctx)
    assert won["sitekey"] == lost["sitekey"] == "0x4AAA-site-1"
    assert ("DELETE", f"{WIDGETS}/0x4AAA-site-2", None) in cf.calls  # loser cleaned up
    row = await _row(pid)
    assert row.settings["sitekey"] == "0x4AAA-site-1"
    assert crypto.decrypt(row.secret_ciphertext) == "0x4AAA-secret-1"
    assert second.secret_ciphertext == row.secret_ciphertext


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
    assert (method, path) == ("PUT", f"{WIDGETS}/0x4AAA-site-1")
    assert body["domains"] == ["shop-abc.pawsites.app"] + [f"d{i}.shop.com" for i in range(9)]
    assert body["mode"] == "managed"
    assert "keeps the first 10" in caplog.text
    assert (await _row(pid)).settings["widget_domains"] == ",".join(body["domains"])


@pytest.mark.asyncio
async def test_a_custom_domain_going_live_redomains_the_widget(beanie_test_db, cf):
    from pocketpaw_ee.cloud.models.site import SiteDomain
    from pocketpaw_ee.sites import service
    from pocketpaw_ee.sites.domain import HostnameStatus

    pid = await _pocket()
    site = await _deployed_site(pid)
    site.domains = [SiteDomain(hostname="www.shop.com", status="verifying", cf_hostname_id="h1")]
    await site.save()
    await _put(pid)

    class _CF:
        async def get_hostname_status(self, _id):
            return HostnameStatus.LIVE

    await service.domain_status(
        workspace_id=WS, site_id=str(site.id), hostname="www.shop.com", _cloudflare=_CF()
    )
    assert cf.calls[-1][:2] == ("PUT", f"{WIDGETS}/0x4AAA-site-1")
    assert cf.calls[-1][2]["domains"] == ["shop-abc.pawsites.app", "www.shop.com"]


@pytest.mark.asyncio
async def test_first_publish_gets_its_widget_after_deploy_and_the_next_carries_it(
    beanie_test_db, cf
):
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites.service import _turnstile_build_kwargs

    pid = await _pocket()
    site = await _site(pid)  # no deployed url yet; allowed_origins is not a host source
    await _put(pid)
    assert cf.calls == []
    assert await _turnstile_build_kwargs(WS, pid) == {}  # first build: no key

    site = await Site.get(site.id)
    site.url = SITE_URL  # the deploy made the URL known
    await site.save()
    await _published(pid, "")  # ...and the post-publish step creates the widget
    assert cf.calls[0][2]["domains"] == ["shop-abc.pawsites.app"]
    assert await _status(pid) == "setup_required"  # the live page still has none

    assert await _turnstile_build_kwargs(WS, pid) == {"turnstile_site_key": "0x4AAA-site-1"}
    await _published(pid, "0x4AAA-site-1")
    assert await _status(pid) == "connected"
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
    await _published(pid, "0x4AAA-site-3")
    assert await _status(pid) == "connected"


@pytest.mark.asyncio
async def test_unlink_and_site_purge_delete_the_widget(beanie_test_db, cf):
    from pocketpaw_ee.integrations import bindings

    pid = await _pocket()
    await _deployed_site(pid)
    await _put(pid)
    async with _client() as c:
        resp = await c.delete(f"/api/v1/sites/by-pocket/{pid}/integrations/turnstile")
    assert resp.status_code == 204
    assert cf.calls[-1][:2] == ("DELETE", f"{WIDGETS}/0x4AAA-site-1")

    await _put(pid)
    await bindings.purge_for_pocket(workspace_id=WS, pocket_id=pid)
    assert cf.calls[-1][:2] == ("DELETE", f"{WIDGETS}/0x4AAA-site-2")
    assert await _row(pid) is None


@pytest.mark.asyncio
async def test_a_cloudflare_teardown_error_never_blocks_unlink_or_purge(beanie_test_db, cf):
    from pocketpaw_ee.integrations import bindings

    pid = await _pocket()
    await _deployed_site(pid)
    await _put(pid)
    cf.fail_delete = True
    async with _client() as c:
        resp = await c.delete(f"/api/v1/sites/by-pocket/{pid}/integrations/turnstile")
    assert resp.status_code == 204 and await _row(pid) is None

    await _put(pid)
    assert await bindings.purge_for_pocket(workspace_id=WS, pocket_id=pid) >= 1
    assert await _row(pid) is None


# ------------------------------------------------------------------- gateway


@pytest.fixture
def verify(monkeypatch):
    from pocketpaw_ee.cloud._core import turnstile
    from pocketpaw_ee.cloud._core.errors import BadRequest

    seen: list[dict] = []

    async def _verify(token, remote_ip, *, code, transport=None, **kw):
        seen.append({"token": token, **kw})
        if token != "good":
            raise BadRequest(code, "no")

    monkeypatch.setattr(turnstile, "verify_turnstile", _verify)
    return seen


async def _site_with_newsletter(pid: str):
    from pocketpaw_ee.cloud.models.site import SiteDomain

    site = await _deployed_site(pid, domains=[("www.shop.com", "live")])
    site.domains.append(SiteDomain(hostname=SITE_HOST, status="live"))
    await site.save()
    async with _client() as c:
        await c.put(f"/api/v1/sites/by-pocket/{pid}/integrations/newsletter", json={})
    return str(site.id)


async def _subscribe(sid: str, **kw):
    async with _client() as c:
        return await _gw(c, sid, "newsletter", "subscribe", args={"email": "a@b.co"}, **kw)


@pytest.mark.asyncio
async def test_gateway_enforces_the_widget_only_once_the_live_build_carries_it(
    beanie_test_db, cf, verify
):
    pid = await _pocket()
    sid = await _site_with_newsletter(pid)
    await _put(pid)

    # Linked after the deploy, no republish: the page can't render the widget, so the
    # platform rule (unset in tests -> no check) still applies.
    assert (await _subscribe(sid)).status_code == 200
    assert verify == []

    await _published(pid, "0x4AAA-site-1")
    missing = await _subscribe(sid)
    bad = await _subscribe(sid, turnstile_token="bad")
    ok = await _subscribe(sid, turnstile_token="good")
    assert (missing.status_code, _code(missing)) == (403, "turnstile_required")
    assert (bad.status_code, _code(bad)) == (403, "turnstile_required")
    assert ok.status_code == 200 and ok.json()["result"] == {"subscribed": True}
    assert [s["secret"] for s in verify] == ["0x4AAA-secret-1", "0x4AAA-secret-1"]
    assert set(verify[1]["expected_hostnames"]) == {
        "shop-abc.pawsites.app",
        "www.shop.com",
        SITE_HOST,
    }


@pytest.mark.asyncio
async def test_relink_is_not_enforced_until_the_new_sitekey_is_published(
    beanie_test_db, cf, verify
):
    pid = await _pocket()
    sid = await _site_with_newsletter(pid)
    await _put(pid)
    await _published(pid, "0x4AAA-site-1")
    async with _client() as c:
        await c.delete(f"/api/v1/sites/by-pocket/{pid}/integrations/turnstile")
    await _put(pid)  # new widget 0x4AAA-site-2; the live page still has site-1 / none
    assert (await _row(pid)).settings["sitekey"] == "0x4AAA-site-2"
    assert (await _subscribe(sid)).status_code == 200
    assert await _status(pid) == "setup_required"


@pytest.mark.asyncio
async def test_an_unreadable_secret_fails_closed(beanie_test_db, cf, verify):
    pid = await _pocket()
    sid = await _site_with_newsletter(pid)
    await _put(pid)
    await _published(pid, "0x4AAA-site-1")
    row = await _row(pid)
    await row.get_pymongo_collection().update_one(
        {"_id": row.id}, {"$set": {"secret_ciphertext": "not-a-fernet-token"}}
    )
    resp = await _subscribe(sid, turnstile_token="good")
    assert (resp.status_code, _code(resp)) == (403, "turnstile_required")
    assert verify == []


@pytest.mark.asyncio
async def test_without_the_binding_the_gateway_keeps_the_platform_rule(beanie_test_db, cf):
    pid = await _pocket()
    sid = await _site_with_newsletter(pid)
    assert (await _subscribe(sid)).status_code == 200  # platform secret unset in tests


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

    # Case and a port on either side don't matter.
    ported = _siteverify({"success": True, "hostname": "Www.Shop.com:443"}, sent)
    await verify_turnstile(
        "tok", None, code="x", transport=ported, secret="s1", expected_hostnames=["WWW.shop.com."]
    )

    foreign = _siteverify({"success": True, "hostname": "evil.test"}, sent)
    with pytest.raises(BadRequest):
        await verify_turnstile(
            "tok", None, code="x", transport=foreign, secret="s1", expected_hostnames=["a.com"]
        )
    with pytest.raises(BadRequest):  # a site with no known hosts accepts nothing
        await verify_turnstile(
            "tok", None, code="x", transport=good, secret="s1", expected_hostnames=[]
        )
    blank = _siteverify({"success": True, "hostname": ""}, sent)
    with pytest.raises(BadRequest):
        await verify_turnstile(
            "tok", None, code="x", transport=blank, secret="s1", expected_hostnames=["a.com"]
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
