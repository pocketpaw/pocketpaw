# tests/ee/sites/test_integrations_social_login.py: the built-in ``social_login``
# integration and its platform OAuth broker (integrations/social_broker.py).
#
# Covers the provider view (builtin / auth / site, required multi select,
# unconfigured without platform clients or the pairwise secret), ``authorize`` (binding
# + provider enabled, return_to exact own-host origin incl. userinfo / port / suffix /
# backslash tricks, live custom domains only, same-origin ``next``), the consent page
# (names the site, CSP + no framing, Strict-cookie-bound single-use token, Cancel),
# PKCE on both providers, ``callback`` (single-use state, provider pinned, ticket in
# the URL and no profile, denial, pairwise account ids), the Google / GitHub profile
# mapping (GitHub: only a VERIFIED /user/emails address), ``redeem`` (gateway token,
# single use, nonce / site mismatch, 60 s TTL) and the shared per-minute counters.
# Fixtures come from test_integrations.py; no network anywhere.
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    WS,
    _client,
    _env,
    _link,
    _pocket,
)

HOST = "shop.example.com"
RETURN_TO = f"https://{HOST}/api/paw-auth/callback"
NONCE = "n" * 32
PROFILE = {
    "provider": "google",
    "provider_account_id": "g-123",
    "email": "ada@example.com",
    "email_verified": True,
    "name": "Ada",
    "avatar_url": "https://img.example.com/a.png",
}


@pytest.fixture(autouse=True)
def _social_env(monkeypatch):
    for p in ("GOOGLE", "GITHUB"):
        monkeypatch.setenv(f"POCKETPAW_SOCIAL_{p}_CLIENT_ID", f"{p.lower()}-client")
        monkeypatch.setenv(f"POCKETPAW_SOCIAL_{p}_CLIENT_SECRET", f"{p.lower()}-secret")
    monkeypatch.setenv("POCKETPAW_SOCIAL_PAIRWISE_SECRET", "p" * 40)


async def _site(pocket_id: str, *, script: str = "shop-abc", url: str = f"https://{HOST}"):
    from pocketpaw_ee.cloud.models.site import Site, SiteDomain

    site = Site(
        workspace=WS,
        pocket_id=pocket_id,
        owner="user-owner",
        script_name=script,
        url=url,
        domains=[
            SiteDomain(hostname="www.acme.test", status="live"),
            SiteDomain(hostname="pending.acme.test", status="pending"),
        ],
    )
    await site.insert()
    return site


async def _bound(providers: str = "google,github"):
    pid = await _pocket()
    site = await _site(pid)
    await _link(pid, "social_login", settings={"providers": providers})
    return pid, site


async def _token(site) -> str:
    from pocketpaw_ee.sites import site_secrets

    return (await site_secrets.secrets_for_deploy(site))["PAW_GATEWAY_TOKEN"]


async def _authorize(c, site_id: str, provider: str = "google", **over):
    params = {"site_id": site_id, "return_to": RETURN_TO, "nonce": NONCE, "next": "/account"}
    params.update(over)
    return await c.get(f"/api/v1/integrations/social/{provider}/authorize", params=params)


def _consent_token(page) -> str:
    return re.search(r'name="t" value="([^"]+)"', page.text).group(1)


async def _start(c, site_id: str, provider: str = "google", action: str = "continue", **over):
    """consent page -> Continue (or Cancel) in the same browser."""
    page = await _authorize(c, site_id, provider, **over)
    assert page.status_code == 200, page.text
    return await c.post(
        f"/api/v1/integrations/social/{provider}/authorize",
        data={"t": _consent_token(page), "action": action},
    )


def _q(resp) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}


def _err(resp) -> str:
    body = resp.json()
    assert body["ok"] is False, body
    return body["error"]["code"]


async def _ticket(c, site_id: str, monkeypatch, provider: str = "google", profile=None) -> str:
    from pocketpaw_ee.integrations import social_broker

    async def _fake(prov, code, payload):
        assert code == "the-code"
        return dict(profile or {**PROFILE, "provider": prov})

    monkeypatch.setattr(social_broker, "fetch_profile", _fake)
    start = await _start(c, site_id, provider)
    assert start.status_code == 303, start.text
    cb = await c.get(
        f"/api/v1/integrations/social/{provider}/callback",
        params={"code": "the-code", "state": _q(start)["state"]},
    )
    assert cb.status_code == 303, cb.text
    return _q(cb)["ticket"]


async def _redeem(c, token: str | None, **body):
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await c.post("/api/v1/integrations/social/redeem", json=body, headers=headers)


# ------------------------------------------------------------------ provider


@pytest.mark.asyncio
async def test_provider_view_and_availability(beanie_test_db, monkeypatch):
    async with _client() as c:
        views = {p["id"]: p for p in (await c.get("/api/v1/integrations/providers")).json()}
    view = views["social_login"]
    assert (view["kind"], view["category"], view["plan"], view["availability"]) == (
        "builtin",
        "auth",
        "site",
        "available",
    )
    assert view["actions"] == [] and view["requires_connection"] is False
    [field] = view["settings_fields"]
    assert (field["key"], field["type"], field["required"], field["multi"]) == (
        "providers",
        "select",
        True,
        True,
    )
    assert [o["value"] for o in field["options"]] == ["google", "github"]

    from pocketpaw_ee.integrations.providers.social_login import SocialLoginProvider

    for p in ("GOOGLE", "GITHUB"):
        monkeypatch.delenv(f"POCKETPAW_SOCIAL_{p}_CLIENT_ID")
    assert SocialLoginProvider().availability() == "unconfigured"
    monkeypatch.setenv("POCKETPAW_SOCIAL_GITHUB_CLIENT_ID", "x")
    assert SocialLoginProvider().availability() == "available"
    monkeypatch.setenv("POCKETPAW_SOCIAL_PAIRWISE_SECRET", "too-short")
    assert SocialLoginProvider().availability() == "unconfigured"


@pytest.mark.asyncio
async def test_linking_mints_the_gateway_token_and_needs_a_provider(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid)
    unset = await _link(pid, "social_login")
    assert unset["status"] == "setup_required"
    view = await _link(pid, "social_login", settings={"providers": "github,google,github"})
    assert view["status"] == "connected" and view["settings"] == {"providers": "github,google"}
    assert await _token(site)
    async with _client() as c:
        bad = await c.put(
            f"/api/v1/sites/by-pocket/{pid}/integrations/social_login",
            json={"settings": {"providers": "apple"}},
        )
    assert bad.status_code == 422


# ----------------------------------------------------------------- authorize


@pytest.mark.asyncio
async def test_continue_redirects_to_google_with_pkce_and_platform_callback(beanie_test_db):
    from pocketpaw_ee.cloud._core import redis_client

    _, site = await _bound()
    async with _client() as c:
        resp = await _start(c, str(site.id))
        by_script = await _start(c, "shop-abc", "github")
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    q = _q(resp)
    assert q["client_id"] == "google-client"
    assert q["redirect_uri"] == (
        "https://connect.example.com/api/v1/integrations/social/google/callback"
    )
    assert q["scope"] == "openid email profile"
    assert q["code_challenge_method"] == "S256" and q["code_challenge"] and q["nonce"]
    raw = await redis_client.get_redis().get(f"social_broker_state:{q['state']}")
    assert NONCE in raw and "/account" in raw and RETURN_TO in raw
    assert NONCE not in loc
    assert by_script.status_code == 303
    gh = _q(by_script)
    assert by_script.headers["location"].startswith("https://github.com/login/oauth/authorize?")
    assert gh["scope"] == "read:user user:email" and gh["client_id"] == "github-client"
    assert gh["code_challenge_method"] == "S256" and gh["code_challenge"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "return_to",
    [
        f"http://{HOST}/api/paw-auth/callback",
        "https://evil.test/api/paw-auth/callback",
        f"https://{HOST}.evil.test/api/paw-auth/callback",
        f"https://evil.test@{HOST}/api/paw-auth/callback",
        f"https://{HOST}@evil.test/api/paw-auth/callback",
        f"https://{HOST}:8443/api/paw-auth/callback",
        f"https://evil.test\\@{HOST}/api/paw-auth/callback",
        f"https://{HOST}/api/paw-auth/callback?x=https://evil.test",
        f"https://{HOST}/api/paw-auth/callback#frag",
        f"https://{HOST}/somewhere-else",
        "https://pending.acme.test/api/paw-auth/callback",
        f"//{HOST}/api/paw-auth/callback",
        "javascript:alert(1)",
        "",
    ],
)
async def test_authorize_refuses_foreign_return_to(beanie_test_db, return_to):
    _, site = await _bound()
    async with _client() as c:
        resp = await _authorize(c, str(site.id), return_to=return_to)
    assert (resp.status_code, _err(resp)) == (403, "return_to_denied")


@pytest.mark.asyncio
async def test_authorize_accepts_a_live_custom_domain(beanie_test_db):
    _, site = await _bound()
    async with _client() as c:
        resp = await _authorize(
            c, str(site.id), return_to="https://WWW.acme.test/api/paw-auth/callback"
        )
    assert resp.status_code == 200, resp.text
    assert "Sign in to www.acme.test with Google?" in resp.text


@pytest.mark.asyncio
async def test_authorize_needs_the_binding_and_the_provider_on(beanie_test_db, monkeypatch):
    _, site = await _bound(providers="github")
    unbound_site = await _site(await _pocket(), script="other")
    async with _client() as c:
        off = await _authorize(c, str(site.id), "google")
        unbound = await _authorize(c, str(unbound_site.id), "github")
        missing = await _authorize(c, "no-such-site", "github")
        unknown = await _authorize(c, str(site.id), "apple")
        monkeypatch.delenv("POCKETPAW_SOCIAL_GITHUB_CLIENT_SECRET")
        unconfigured = await _authorize(c, str(site.id), "github")
    assert (off.status_code, _err(off)) == (403, "not_enabled")
    assert (unbound.status_code, _err(unbound)) == (403, "not_enabled")
    assert (missing.status_code, _err(missing)) == (403, "not_enabled")
    assert (unknown.status_code, _err(unknown)) == (404, "not_found")
    assert (unconfigured.status_code, _err(unconfigured)) == (409, "provider_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("next_path", "ok"),
    [
        ("/account?tab=1", True),
        ("", True),
        ("//evil.test/x", False),
        ("https://evil.test/", False),
        ("/\\evil.test", False),
        ("javascript:alert(1)", False),
        ("/a\nb", False),
        ("/" + "a" * 600, False),
    ],
)
async def test_authorize_next_must_be_a_same_origin_path(beanie_test_db, next_path, ok):
    _, site = await _bound()
    async with _client() as c:
        resp = await _authorize(c, str(site.id), next=next_path)
    if ok:
        assert resp.status_code == 200
    else:
        assert (resp.status_code, _err(resp)) == (400, "invalid_request")


@pytest.mark.asyncio
async def test_authorize_refuses_a_malformed_nonce(beanie_test_db):
    _, site = await _bound()
    async with _client() as c:
        short = await _authorize(c, str(site.id), nonce="abc")
        odd = await _authorize(c, str(site.id), nonce="x" * 20 + "<script>")
    assert _err(short) == _err(odd) == "invalid_request"


# ------------------------------------------------------------------ callback


@pytest.mark.asyncio
async def test_callback_issues_a_ticket_and_state_is_single_use(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import social_broker

    async def _fake(provider, code, payload):
        assert payload["code_verifier"] and payload["oidc_nonce"]
        return dict(PROFILE)

    monkeypatch.setattr(social_broker, "fetch_profile", _fake)
    _, site = await _bound()
    async with _client() as c:
        start = await _start(c, str(site.id))
        state = _q(start)["state"]
        wrong = await c.get(
            "/api/v1/integrations/social/github/callback", params={"code": "c", "state": state}
        )
        start = await _start(c, str(site.id))
        state = _q(start)["state"]
        cb = await c.get(
            "/api/v1/integrations/social/google/callback", params={"code": "c", "state": state}
        )
        replay = await c.get(
            "/api/v1/integrations/social/google/callback", params={"code": "c", "state": state}
        )
    assert (wrong.status_code, _err(wrong)) == (400, "invalid_ticket")
    assert cb.status_code == 303
    loc = cb.headers["location"]
    assert loc.startswith(RETURN_TO + "?")
    q = _q(cb)
    assert set(q) == {"ticket", "next"} and q["next"] == "/account"
    assert "ada" not in loc.lower() and "g-123" not in loc
    assert (replay.status_code, _err(replay)) == (400, "invalid_ticket")


@pytest.mark.asyncio
async def test_callback_denied_or_failed_goes_back_with_an_error(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import social_broker

    async def _boom(*_a):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(social_broker, "fetch_profile", _boom)
    _, site = await _bound()
    async with _client() as c:
        denied = await c.get(
            "/api/v1/integrations/social/google/callback",
            params={"state": _q(await _start(c, str(site.id)))["state"]},
        )
        failed = await c.get(
            "/api/v1/integrations/social/google/callback",
            params={"code": "c", "state": _q(await _start(c, str(site.id)))["state"]},
        )
    assert _q(denied) == {"error": "access_denied", "next": "/account"}
    assert _q(failed) == {"error": "provider_error", "next": "/account"}


def test_google_profile_mapping():
    from pocketpaw_ee.integrations.social_broker import google_profile

    assert google_profile(
        {
            "sub": "1",
            "email": "Ada@Example.com",
            "email_verified": True,
            "name": "Ada",
            "picture": "https://p/x.png",
        }
    ) == {
        "provider": "google",
        "provider_account_id": "1",
        "email": "ada@example.com",
        "email_verified": True,
        "name": "Ada",
        "avatar_url": "https://p/x.png",
    }
    # Only a real boolean true counts as verified.
    assert (
        google_profile({"sub": "1", "email": "a@b.co", "email_verified": "true"})["email_verified"]
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "email"),
    [
        (
            [
                {"email": "spoof@victim.test", "primary": True, "verified": False},
                {"email": "Real@Example.com", "primary": False, "verified": True},
            ],
            "real@example.com",
        ),
        ([{"email": "spoof@victim.test", "primary": True, "verified": False}], None),
        (None, None),  # the visitor declined user:email -> 403
    ],
)
async def test_github_profile_uses_only_a_verified_email(monkeypatch, rows, email):
    from pocketpaw_ee.integrations import social_broker

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/login/oauth/access_token":
            assert b"client_secret=github-secret" in request.content
            assert b"code_verifier=the-verifier" in request.content
            return httpx.Response(200, json={"access_token": "gho_x"})
        assert request.headers["authorization"] == "Bearer gho_x"
        if request.url.path == "/user":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "login": "ada",
                    "name": None,
                    "email": "spoof@victim.test",
                    "avatar_url": "https://avatars.example/42",
                },
            )
        if rows is None:
            return httpx.Response(403, json={"message": "nope"})
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(social_broker, "_transport", httpx.MockTransport(handler))
    profile = await social_broker.fetch_profile("github", "code", {"code_verifier": "the-verifier"})
    assert profile == {
        "provider": "github",
        "provider_account_id": "42",
        "email": email,
        "email_verified": email is not None,
        "name": "ada",
        "avatar_url": "https://avatars.example/42",
    }
    assert seen == ["/login/oauth/access_token", "/user", "/user/emails"]


# -------------------------------------------------------------------- redeem


@pytest.mark.asyncio
async def test_redeem_returns_the_profile_once(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core import redis_client

    _, site = await _bound()
    token = await _token(site)
    async with _client() as c:
        ticket = await _ticket(c, str(site.id), monkeypatch)
        ttl = await redis_client.get_redis().ttl(f"social_ticket_state:{ticket}")
        ok = await _redeem(c, token, site_id=str(site.id), ticket=ticket, nonce=NONCE)
        again = await _redeem(c, token, site_id=str(site.id), ticket=ticket, nonce=NONCE)
    assert 0 < ttl <= 60
    from pocketpaw_ee.integrations.social_broker import pairwise_id

    pairwise = pairwise_id(str(site.id), "google", "g-123")
    assert len(pairwise) == 64 and pairwise != "g-123"
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "result": {**PROFILE, "provider_account_id": pairwise}}
    assert (again.status_code, _err(again)) == (400, "invalid_ticket")


@pytest.mark.asyncio
async def test_redeem_refuses_wrong_nonce_and_burns_the_ticket(beanie_test_db, monkeypatch):
    _, site = await _bound()
    token = await _token(site)
    async with _client() as c:
        ticket = await _ticket(c, str(site.id), monkeypatch)
        wrong = await _redeem(c, token, site_id=str(site.id), ticket=ticket, nonce="m" * 32)
        right = await _redeem(c, token, site_id=str(site.id), ticket=ticket, nonce=NONCE)
    assert (wrong.status_code, _err(wrong)) == (400, "invalid_ticket")
    assert _err(right) == "invalid_ticket"


@pytest.mark.asyncio
async def test_redeem_refuses_another_sites_ticket(beanie_test_db, monkeypatch):
    _, site = await _bound()
    pid_b = await _pocket()
    site_b = await _site(pid_b, script="shop-b")
    await _link(pid_b, "social_login", settings={"providers": "google"})
    async with _client() as c:
        ticket = await _ticket(c, str(site.id), monkeypatch)
        cross = await _redeem(
            c, await _token(site_b), site_id=str(site_b.id), ticket=ticket, nonce=NONCE
        )
    assert (cross.status_code, _err(cross)) == (400, "invalid_ticket")


@pytest.mark.asyncio
async def test_redeem_needs_the_sites_gateway_token(beanie_test_db, monkeypatch):
    _, site = await _bound()
    pid_b = await _pocket()
    site_b = await _site(pid_b, script="shop-b")
    await _link(pid_b, "social_login", settings={"providers": "google"})
    sid = str(site.id)
    async with _client() as c:
        ticket = await _ticket(c, sid, monkeypatch)
        missing = await _redeem(c, None, site_id=sid, ticket=ticket, nonce=NONCE)
        wrong = await _redeem(c, "not-the-token", site_id=sid, ticket=ticket, nonce=NONCE)
        other = await _redeem(c, await _token(site_b), site_id=sid, ticket=ticket, nonce=NONCE)
        bad_body = await _redeem(c, await _token(site), ticket=ticket)
        ok = await _redeem(c, await _token(site), site_id=sid, ticket=ticket, nonce=NONCE)
    for resp in (missing, wrong, other):
        assert (resp.status_code, _err(resp)) == (401, "unauthorized")
    assert (bad_body.status_code, _err(bad_body)) == (400, "invalid_request")
    # Refused attempts never reached the ticket.
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_redeem_refuses_once_the_provider_is_turned_off(beanie_test_db, monkeypatch):
    pid, site = await _bound()
    async with _client() as c:
        ticket = await _ticket(c, str(site.id), monkeypatch)
        await _link(pid, "social_login", settings={"providers": "github"})
        resp = await _redeem(
            c, await _token(site), site_id=str(site.id), ticket=ticket, nonce=NONCE
        )
    assert (resp.status_code, _err(resp)) == (403, "not_enabled")


@pytest.mark.asyncio
async def test_an_expired_ticket_is_refused(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core import redis_client

    _, site = await _bound()
    async with _client() as c:
        ticket = await _ticket(c, str(site.id), monkeypatch)
        await redis_client.get_redis().delete(f"social_ticket_state:{ticket}")  # TTL elapsed
        resp = await _redeem(
            c, await _token(site), site_id=str(site.id), ticket=ticket, nonce=NONCE
        )
    assert (resp.status_code, _err(resp)) == (400, "invalid_ticket")


# ------------------------------------------------------------------- consent


@pytest.mark.asyncio
async def test_consent_page_names_the_site_and_cannot_be_framed(beanie_test_db):
    _, site = await _bound()
    async with _client() as c:
        page = await _authorize(c, str(site.id), "github")
    assert page.status_code == 200
    assert "Sign in to shop.example.com with GitHub?" in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
    nonce = re.search(r"style-src 'nonce-([^']+)'", csp).group(1)
    assert f'<style nonce="{nonce}">' in page.text and "<script" not in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"
    cookie = page.headers["set-cookie"].lower()
    assert "samesite=strict" in cookie and "httponly" in cookie and "secure" in cookie
    assert "path=/api/v1/integrations/social/" in cookie
    # Nothing has gone to the provider yet.
    assert "github.com" not in page.text and "location" not in page.headers


@pytest.mark.asyncio
async def test_continue_needs_the_strict_cookie_and_the_token_is_single_use(beanie_test_db):
    _, site = await _bound()
    url = "/api/v1/integrations/social/google/authorize"
    async with _client() as c:
        token = _consent_token(await _authorize(c, str(site.id)))
    # Another browser: a cross-site auto-submit carries no Strict cookie.
    async with _client() as other:
        foreign = await other.post(url, data={"t": token, "action": "continue"})
    async with _client() as c2:
        page2 = await _authorize(c2, str(site.id))
        swapped = await c2.post(url, data={"t": token, "action": "continue"})
        wrong_provider = await c2.post(
            "/api/v1/integrations/social/github/authorize",
            data={"t": _consent_token(page2), "action": "continue"},
        )
    async with _client() as c3:
        t3 = _consent_token(await _authorize(c3, str(site.id)))
        first = await c3.post(url, data={"t": t3, "action": "continue"})
        c3.cookies.set(
            "paw_social_consent",
            hashlib.sha256(t3.encode()).hexdigest(),
            path="/api/v1/integrations/social/",
        )
        replay = await c3.post(url, data={"t": t3, "action": "continue"})
    assert (foreign.status_code, _err(foreign)) == (400, "invalid_request")
    assert (swapped.status_code, _err(swapped)) == (400, "invalid_request")
    assert (wrong_provider.status_code, _err(wrong_provider)) == (400, "invalid_request")
    assert first.status_code == 303
    assert (replay.status_code, _err(replay)) == (400, "invalid_ticket")


@pytest.mark.asyncio
async def test_cancel_goes_back_to_the_site_without_the_provider(beanie_test_db):
    _, site = await _bound()
    async with _client() as c:
        resp = await _start(c, str(site.id), action="cancel")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(RETURN_TO + "?")
    assert _q(resp) == {"error": "cancelled", "next": "/account"}


# ------------------------------------------------------------------ pairwise


@pytest.mark.asyncio
async def test_one_visitor_gets_unlinkable_ids_on_two_sites(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core import redis_client

    _, site_a = await _bound()
    pid_b = await _pocket()
    site_b = await _site(pid_b, script="shop-b")
    await _link(pid_b, "social_login", settings={"providers": "google"})
    async with _client() as c:
        ta = await _ticket(c, str(site_a.id), monkeypatch)
        tb = await _ticket(c, str(site_b.id), monkeypatch)
        raw = await redis_client.get_redis().get(f"social_ticket_state:{ta}")
        a = await _redeem(c, await _token(site_a), site_id=str(site_a.id), ticket=ta, nonce=NONCE)
        b = await _redeem(c, await _token(site_b), site_id=str(site_b.id), ticket=tb, nonce=NONCE)
        again = await _ticket(c, str(site_a.id), monkeypatch)
        a2 = await _redeem(
            c, await _token(site_a), site_id=str(site_a.id), ticket=again, nonce=NONCE
        )
    ida = a.json()["result"]["provider_account_id"]
    idb = b.json()["result"]["provider_account_id"]
    assert ida != idb and "g-123" not in (ida + idb)
    assert a2.json()["result"]["provider_account_id"] == ida  # stable per site
    assert "g-123" not in raw  # the raw id is never stored


# --------------------------------------------------------------- rate limits


@pytest.mark.asyncio
async def test_per_ip_counter_covers_consent_decide_and_callback(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import social_broker

    monkeypatch.setattr(social_broker, "IP_PER_MIN", 3)
    _, site = await _bound()
    async with _client() as c:
        start = await _start(c, str(site.id))  # consent + decide: 2
        cb = await c.get(  # 3
            "/api/v1/integrations/social/google/callback",
            params={"state": _q(start)["state"]},
        )
        over = await _authorize(c, str(site.id))  # 4
    assert start.status_code == 303 and cb.status_code == 303
    assert (over.status_code, _err(over)) == (429, "rate_limited")


@pytest.mark.asyncio
async def test_redeem_is_capped_per_site_before_the_token_is_checked(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import social_broker
    from pocketpaw_ee.sites import site_secrets

    _, site = await _bound()
    token = await _token(site)
    monkeypatch.setattr(social_broker, "REDEEM_SITE_PER_MIN", 1)
    calls = []
    real = site_secrets.secret_matches

    async def _spy(**kw):
        calls.append(kw)
        return await real(**kw)

    monkeypatch.setattr(site_secrets, "secret_matches", _spy)
    async with _client() as c:
        first = await _redeem(c, token, site_id=str(site.id), ticket="t" * 40, nonce=NONCE)
        second = await _redeem(c, token, site_id=str(site.id), ticket="t" * 40, nonce=NONCE)
    assert _err(first) == "invalid_ticket"
    assert (second.status_code, _err(second)) == (429, "rate_limited")
    assert len(calls) == 1
