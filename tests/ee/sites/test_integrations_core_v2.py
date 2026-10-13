# tests/ee/sites/test_integrations_core_v2.py: Sites integrations, the Wave 1 shared seams.
#
# Covers what the Wave 1 contract addendum adds on top of wave 0: ``settings_fields``
# and ``requires_connection`` on ProviderView, server-side validation of PUT
# settings, the ``site_setup`` / ``site_status`` / ``connection_attention`` /
# ``resume_url`` provider hooks, ``setup_required``, the resume route (same state +
# browser cookie binding as connect), the /start session check, the embed kind
# (``calendly`` / ``cal_com``) end to end, and the agent tool's embed answer.
# Fixtures and helpers come from test_integrations.py (the ``demo`` provider).
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    EDITOR,
    OWNER,
    WS,
    _agent,
    _client,
    _code,
    _connect_demo,
    _env,
    _gw,
    _link,
    _message,
    _payload,
    _pocket,
    _site,
)

CALENDLY = "https://calendly.com/acme/30min"


def _sites_url(pid: str, provider: str = "") -> str:
    base = f"/api/v1/sites/by-pocket/{pid}/integrations"
    return f"{base}/{provider}" if provider else base


async def _demo_bound(c, pid: str) -> str:
    page, _ = await _connect_demo(c, pid)
    return _message(page)["connection_id"]


async def _set_status(conn_id: str, status: str) -> None:
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    conn = await IntegrationConnection.get(PydanticObjectId(conn_id))
    conn.status = status
    await conn.save()


async def _follow(c, authorize_url: str, provider: str = "demo"):
    """/start -> provider (the demo redirects straight back) -> callback."""
    split = urlsplit(authorize_url)
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    query = parse_qs(urlsplit(start.headers["location"]).query)
    return await c.get(
        f"/api/v1/integrations/{provider}/callback",
        params={"code": query["code"][0], "state": query["state"][0]},
    )


# ---------------------------------------------------------------- catalog


@pytest.mark.asyncio
async def test_provider_views_carry_settings_fields_and_requires_connection(beanie_test_db):
    async with _client() as c:
        providers = {p["id"]: p for p in (await c.get("/api/v1/integrations/providers")).json()}
    assert {k: p["requires_connection"] for k, p in providers.items()} == {
        "newsletter": False,
        "calendly": False,
        "cal_com": False,
        "demo": True,
    }
    calendly = providers["calendly"]
    assert (calendly["kind"], calendly["category"], calendly["plan"]) == (
        "embed",
        "scheduling",
        "free",
    )
    assert calendly["actions"] == []
    [field] = calendly["settings_fields"]
    assert set(field) == {
        "key",
        "label",
        "type",
        "help",
        "required",
        "options",
        "readonly",
        "multi",
        "allowed_hosts",
        "max_length",
    }
    assert (field["key"], field["type"], field["required"], field["readonly"]) == (
        "url",
        "url",
        True,
        False,
    )
    assert field["multi"] is False
    assert field["allowed_hosts"] == ["calendly.com"]
    assert isinstance(field["max_length"], int)
    assert providers["cal_com"]["settings_fields"][0]["key"] == "url"
    assert providers["newsletter"]["settings_fields"] == []
    board = next(f for f in providers["demo"]["settings_fields"] if f["key"] == "board_url")
    assert (board["type"], board["readonly"], board["options"]) == ("link", True, None)


def test_registry_is_a_tuple_with_unique_ids():
    from pocketpaw_ee.integrations import providers

    assert isinstance(providers._PROVIDERS, tuple)
    ids = [p.id for p in providers.all_providers()]
    assert len(ids) == len(set(ids))
    assert ids[:3] == ["newsletter", "calendly", "cal_com"]


# ---------------------------------------------------------------- settings


def test_settings_value_rules():
    from pocketpaw_ee.integrations.providers.base import SettingOption, SettingsField
    from pocketpaw_ee.integrations.settings import check_value

    url = SettingsField("url", "Link", "url", allowed_hosts=("cal.com", "app.cal.com"))
    assert check_value(url, " https://app.cal.com/x ") == "https://app.cal.com/x"
    for bad in (
        "javascript:alert(1)",
        "https://cal.com.evil.test/x",
        "ftp://cal.com/x",
        "cal.com",
        "https://evil.com\\@cal.com/x",  # urlsplit sees cal.com, a browser evil.com
        "https://evil.com@cal.com/x",
        "https://user:pw@cal.com/x",
        "https://cal.com:8443/x",
        "https://cal.com/x y",
        "https://cal.com/x\ty",
        "https://cal.com/x\x00",
        'https://cal.com/x"onload=1',
        "https://cal.com/x'y",
        "https://cal.com/<script>",
        "https://cal.com/x>",
        "https://c\u0430l.com/x",  # Cyrillic a: not ASCII
        "https://cal.com:bad/x",
    ):
        with pytest.raises(ValueError):
            check_value(url, bad)
    with pytest.raises(ValueError):
        check_value(url, 42)

    opts = (SettingOption("google", "Google"), SettingOption("github", "GitHub"))
    single = SettingsField("p", "Provider", "select", options=opts)
    multi = SettingsField("p", "Providers", "select", options=opts, multi=True)
    assert check_value(single, "github") == "github"
    with pytest.raises(ValueError):
        check_value(single, "google,github")
    assert check_value(multi, "github, google,github") == "github,google"
    with pytest.raises(ValueError):
        check_value(multi, "google,apple")

    text = SettingsField("t", "Text", max_length=5)
    assert check_value(text, "abcde") == "abcde"
    with pytest.raises(ValueError):
        check_value(text, "abcdef")


@pytest.mark.asyncio
async def test_put_settings_are_validated_readonly_ignored_and_merge(beanie_test_db):
    pid = await _pocket()
    async with _client() as c:
        unknown = await c.put(_sites_url(pid, "demo"), json={"settings": {"sheet": "A"}})
        too_long = await c.put(_sites_url(pid, "demo"), json={"settings": {"note": "x" * 121}})
        ok = await c.put(
            _sites_url(pid, "demo"),
            json={"settings": {"note": "hi", "board_url": "https://evil.test"}},
        )
        kept = await c.put(_sites_url(pid, "demo"), json={"settings": {}})
        cleared = await c.put(_sites_url(pid, "demo"), json={"settings": {"note": ""}})
        builtin = await c.put(_sites_url(pid, "newsletter"), json={"settings": {"x": "1"}})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (422, "invalid_settings")
    assert (too_long.status_code, too_long.json()["error"]["code"]) == (422, "invalid_settings")
    assert builtin.status_code == 422
    assert ok.status_code == 200 and ok.json()["settings"] == {"note": "hi"}
    assert kept.json()["settings"] == {"note": "hi"}
    assert cleared.json()["settings"] == {}


# ------------------------------------------------------------------- embed


@pytest.mark.asyncio
async def test_embed_is_connected_once_its_link_is_valid_and_the_gateway_refuses_it(
    beanie_test_db,
):
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        connect = await c.post("/api/v1/integrations/calendly/connect", json={})
        linked = await c.put(_sites_url(pid, "calendly"), json={})
        wrong_host = await c.put(
            _sites_url(pid, "calendly"), json={"settings": {"url": "https://cal.com/acme"}}
        )
        good = await c.put(_sites_url(pid, "calendly"), json={"settings": {"url": CALENDLY}})
        cal = await c.put(
            _sites_url(pid, "cal_com"), json={"settings": {"url": "https://app.cal.com/acme"}}
        )
        listed = (await c.get(_sites_url(pid))).json()
        call = await _gw(c, str(site.id), "calendly", "book")
    assert connect.status_code == 422
    assert linked.status_code == 200
    assert linked.json() == {
        "provider": "calendly",
        "connection_id": None,
        "status": "setup_required",
        "settings": {},
        "enabled_actions": [],
        "updated_at": linked.json()["updated_at"],
    }
    assert wrong_host.status_code == 422
    assert good.json()["status"] == "connected" and good.json()["settings"] == {"url": CALENDLY}
    assert cal.json()["status"] == "connected"
    assert {v["provider"]: v["status"] for v in listed} == {
        "cal_com": "connected",
        "calendly": "connected",
    }
    assert (call.status_code, _code(call)) == (403, "action_disabled")


# ------------------------------------------------------------- setup hooks


@pytest.mark.asyncio
async def test_site_setup_writes_readonly_settings_and_is_rerun_idempotently(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    calls: list[tuple[str, bool]] = []

    async def _setup(self, binding, ctx):
        calls.append((ctx.pocket_id, ctx.get_token is not None))
        if binding.settings.get("board_url"):
            return {}
        return {"board_url": "https://board.example.com/1", "note": "sneaky", "evil": "x"}

    monkeypatch.setattr(DemoProvider, "site_setup", _setup)
    pid = await _pocket()
    async with _client() as c:
        no_grant = await c.put(_sites_url(pid, "demo"), json={})
        assert calls == []  # connect kind with no grant: nothing to set up yet
        assert no_grant.json()["status"] == "needs_connection"
        await _demo_bound(c, pid)
        [view] = (await c.get(_sites_url(pid))).json()
        linked = len(calls)
        again = await c.put(_sites_url(pid, "demo"), json={"enabled_actions": ["echo"]})
        after_toggle = len(calls)
        await c.put(_sites_url(pid, "demo"), json={"enabled_actions": []})
        await c.put(_sites_url(pid, "demo"), json={"enabled_actions": ["echo"]})
        after_enable = len(calls)
        await c.put(_sites_url(pid, "demo"), json={"settings": {"note": "x"}})
        after_settings = len(calls)
    assert view["status"] == "connected"
    assert view["settings"] == {"board_url": "https://board.example.com/1"}
    assert again.json()["settings"] == {"board_url": "https://board.example.com/1"}
    assert linked >= 1 and all(has_token for _, has_token in calls)
    assert after_toggle == linked  # a pure enabled_actions toggle runs nothing
    assert after_enable == linked + 1  # disabled -> enabled does
    assert after_settings == after_enable + 1  # so does a settings change


@pytest.mark.asyncio
async def test_site_setup_values_are_checked_and_stored_with_an_atomic_set(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    async def _setup(self, binding, ctx):
        # A concurrent writer changes another field while setup runs.
        await SiteIntegration.get_pymongo_collection().update_one(
            {"_id": binding.id}, {"$set": {"purpose": "written meanwhile"}}
        )
        return {"board_url": "javascript:alert(1)"}

    monkeypatch.setattr(DemoProvider, "site_setup", _setup)
    pid = await _pocket()
    async with _client() as c:
        await _demo_bound(c, pid)
        [view] = (await c.get(_sites_url(pid))).json()
    row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
    assert "board_url" not in row.settings and view["settings"] == {}
    assert row.purpose == "written meanwhile"


@pytest.mark.asyncio
async def test_a_failing_site_setup_reads_setup_required_and_recovers(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import ProviderError
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    state = {"fail": True}

    async def _setup(self, binding, ctx):
        if state["fail"]:
            raise ProviderError("sheet create failed")
        return {"board_url": "https://board.example.com/2"}

    monkeypatch.setattr(DemoProvider, "site_setup", _setup)
    pid = await _pocket()
    async with _client() as c:
        await _demo_bound(c, pid)
        [failed] = (await c.get(_sites_url(pid))).json()
        state["fail"] = False
        fixed = await c.put(_sites_url(pid, "demo"), json={"settings": {"note": "retry"}})
    assert failed["status"] == "setup_required"
    assert fixed.status_code == 200 and fixed.json()["status"] == "connected"


@pytest.mark.asyncio
async def test_site_status_and_attention_hooks(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    monkeypatch.setattr(
        DemoProvider,
        "site_status",
        lambda self, binding, conn: "setup_required" if conn.meta.get("pending") else None,
    )
    monkeypatch.setattr(
        DemoProvider,
        "connection_attention",
        lambda self, conn: "Finish setting up" if conn.meta.get("pending") else None,
    )
    pid = await _pocket()
    async with _client() as c:
        conn_id = await _demo_bound(c, pid)
        [before] = (await c.get("/api/v1/integrations/connections")).json()

        from beanie import PydanticObjectId
        from pocketpaw_ee.cloud.models.integration import IntegrationConnection

        conn = await IntegrationConnection.get(PydanticObjectId(conn_id))
        conn.meta = {"pending": True}
        await conn.save()
        [after] = (await c.get("/api/v1/integrations/connections")).json()
        [site] = (await c.get(_sites_url(pid))).json()

        monkeypatch.setattr(DemoProvider, "connection_attention", lambda self, conn: 1 / 0)
        [broken] = (await c.get("/api/v1/integrations/connections")).json()
    assert before["attention"] is None
    assert after["attention"] == "Finish setting up"
    assert site["status"] == "setup_required"
    assert broken["attention"] is None


@pytest.mark.asyncio
async def test_hidden_settings_never_reach_a_view(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import SettingsField
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    monkeypatch.setattr(
        DemoProvider,
        "settings_fields",
        (*DemoProvider.settings_fields, SettingsField("sitekey", "Key", hidden=True)),
    )

    async def _setup(self, binding, ctx):
        return {"sitekey": "0x4AAA"}

    monkeypatch.setattr(DemoProvider, "site_setup", _setup)
    pid = await _pocket()
    async with _client() as c:
        await _demo_bound(c, pid)
        [view] = (await c.get(_sites_url(pid))).json()
        sent = await c.put(_sites_url(pid, "demo"), json={"settings": {"sitekey": "mine"}})
        providers = (await c.get("/api/v1/integrations/providers")).json()
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
    assert row.settings["sitekey"] == "0x4AAA"
    assert "sitekey" not in view["settings"] and "sitekey" not in sent.json()["settings"]
    demo = next(p for p in providers if p["id"] == "demo")
    assert "sitekey" not in {f["key"] for f in demo["settings_fields"]}


# ------------------------------------------------------------------ resume


@pytest.mark.asyncio
async def test_resume_reconnects_a_dead_grant_in_place(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    pid = await _pocket()
    async with _client() as c:
        conn_id = await _demo_bound(c, pid)
        nothing = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
        await _set_status(conn_id, "needs_reconnect")
        [dead] = (await c.get(_sites_url(pid))).json()
        resumed = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
        assert resumed.status_code == 200, resumed.text
        url = resumed.json()["authorize_url"]
        assert url.startswith("https://connect.example.com/api/v1/integrations/demo/start?t=")
        cb = await _follow(c, url)
        [alive] = (await c.get(_sites_url(pid))).json()
    assert (nothing.status_code, nothing.json()["error"]["code"]) == (
        409,
        "integrations.nothing_to_resume",
    )
    assert dead["status"] == "needs_reconnect"
    msg = _message(cb.text)
    assert msg["status"] == "active" and msg["connection_id"] == conn_id
    assert alive["status"] == "connected" and alive["connection_id"] == conn_id
    assert await IntegrationConnection.find_all().count() == 1


@pytest.mark.asyncio
async def test_resume_uses_the_provider_resume_url_and_keeps_the_cookie_binding(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    seen: dict = {}

    async def _resume(self, conn, state, redirect_uri):
        seen.update(conn=str(conn.id), state=state, redirect_uri=redirect_uri)
        return f"https://provider.example.com/onboard?return={redirect_uri}"

    monkeypatch.setattr(DemoProvider, "resume_url", _resume)
    pid = await _pocket()
    async with _client() as c:
        conn_id = await _demo_bound(c, pid)
        resumed = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
        split = urlsplit(resumed.json()["authorize_url"])
        start = await c.get(split.path, params=parse_qs(split.query))
    assert seen["conn"] == conn_id
    assert seen["redirect_uri"] == "https://connect.example.com/api/v1/integrations/demo/callback"
    assert start.status_code == 302
    assert start.headers["location"].startswith("https://provider.example.com/onboard")
    assert "paw_int_demo=" in start.headers["set-cookie"]


@pytest.mark.asyncio
async def test_resume_authz_revoked_and_account_mismatch(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import TokenGrant
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    pid = await _pocket()
    async with _client() as c:
        conn_id = await _demo_bound(c, pid)
        await _set_status(conn_id, "needs_reconnect")
    async with _client(WS, EDITOR) as c:
        denied = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
    assert (denied.status_code, denied.json()["error"]["code"]) == (
        403,
        "integrations.not_connection_owner",
    )

    async def _other(self, code, redirect_uri, code_verifier=None):
        return TokenGrant(access_token="a", account_ref="someone-else", account_label="Other")

    monkeypatch.setattr(DemoProvider, "complete", _other)
    async with _client() as c:
        resumed = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
        cb = await _follow(c, resumed.json()["authorize_url"])
        assert _message(cb.text)["error"] == "account_mismatch"
        await _set_status(conn_id, "revoked")
        gone = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
    assert gone.status_code == 409


@pytest.mark.asyncio
async def test_grant_meta_lands_on_the_connection(beanie_test_db):
    from pocketpaw_ee.integrations import vault
    from pocketpaw_ee.integrations.providers.base import TokenGrant

    conn = await vault.store_grant(
        workspace_id=WS,
        provider="demo",
        grant=TokenGrant(access_token="a", account_ref="acct", meta={"charges_enabled": False}),
        user_id=OWNER,
    )
    again = await vault.store_grant(
        workspace_id=WS,
        provider="demo",
        grant=TokenGrant(access_token="b", account_ref="acct", meta={"charges_enabled": True}),
        user_id=OWNER,
        connection_id=str(conn.id),
    )
    assert again.id == conn.id and again.meta == {"charges_enabled": True}

    from pocketpaw_ee.cloud._core.errors import ConflictError

    for ref in ("other", ""):
        with pytest.raises(ConflictError) as exc:
            await vault.store_grant(
                workspace_id=WS,
                provider="demo",
                grant=TokenGrant(access_token="c", account_ref=ref),
                user_id=OWNER,
                connection_id=str(conn.id),
            )
        assert exc.value.code == "integrations.resume_mismatch"


# ------------------------------------------------------------ /start session


@pytest.mark.asyncio
async def test_start_refuses_a_browser_signed_in_as_someone_else(beanie_test_db):
    from pocketpaw_ee.cloud.auth.core import get_jwt_strategy

    strategy = get_jwt_strategy()
    other = await strategy.write_token(SimpleNamespace(id="user-someone-else"))
    same = await strategy.write_token(SimpleNamespace(id=OWNER))
    async with _client() as c:
        for token, expected in ((other, "session_mismatch"), (same, None), ("garbage", None)):
            started = await c.post("/api/v1/integrations/demo/connect", json={})
            split = urlsplit(started.json()["authorize_url"])
            resp = await c.get(
                split.path,
                params=parse_qs(split.query),
                headers={"cookie": f"paw_auth={token}"},
            )
            if expected:
                assert _message(resp.text)["error"] == expected
            else:
                assert resp.status_code == 302


# ------------------------------------------------------------------- agent


@pytest.mark.asyncio
async def test_connect_tool_for_an_embed_asks_for_the_booking_link(beanie_test_db, _agent):  # noqa: F811
    sites_create, pushed = _agent
    pid = await _pocket()
    out = _payload(
        await sites_create._connect_site_integration_handler(
            {"pocket_id": pid, "provider": "calendly", "purpose": "Let clients book a call"}
        )
    )
    assert out["site_integration"]["status"] == "setup_required"
    assert "booking link" in out["message"] and "inline embed" in out["message"]
    [(kind, data)] = pushed
    assert kind == "site_integration_requested"
    assert data["provider"]["requires_connection"] is False
    assert data["provider"]["settings_fields"][0]["key"] == "url"
