# tests/ee/sites/test_owner_sign_in.py: "Open owner page" (integrations/owner_sign_in.py).
#
# A VERIFIED, active Paw user, still a member of the site's workspace, who owns a published
# site or is on its team (pocket.team; a shared_with share stores no access level, so it
# gives no site role yet) gets a one-time link that signs them in to the site's own
# better-auth as "owner" or "staff". Plain workspace members get nothing. The ticket rides
# in the URL fragment, so no server or request log ever sees it. The site's Worker
# trades it for the identity with its PAW_GATEWAY_TOKEN, and re-checks the role with
# /check. Negative paths: unverified email, not a collaborator, replay, another site's
# ticket, a wrong or missing token (answered exactly like a missing site), expiry,
# tampering, open redirects, unpublished, no token deployed yet. Plus the deploy side.
from __future__ import annotations

import asyncio
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
TEAMMATE = "user-teammate"


async def _site(
    pocket_id: str,
    *,
    url: str = f"https://{HOST}",
    script: str = "pickles-1",
    token: bool = True,
):
    """A site doc; ``token`` = it was published with its gateway token."""
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.integrations import bindings

    site = Site(workspace=WS, pocket_id=pocket_id, owner=OWNER, script_name=script, url=url)
    await site.insert()
    if token:
        await bindings.ensure_gateway_token(WS, pocket_id)
    return site


async def _with_team(pocket_id: str, *, team=(), shared_with=()) -> None:
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = await Pocket.get(pocket_id)
    doc.team = list(team)
    doc.shared_with = list(shared_with)
    await doc.save()


def _app(
    user_id: str,
    *,
    workspace_id: str = WS,
    email: str = "asha@example.com",
    verified: bool = True,
    member: bool = True,
) -> FastAPI:
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
        is_verified=verified,
        is_active=True,
        active_workspace=workspace_id,
        workspaces=[_Membership(workspace_id)] if member else [],
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


def _fragment(resp) -> dict[str, str]:
    assert resp.status_code == 200, resp.text
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.json()["url"]).fragment).items()}


def _ticket(resp) -> str:
    return _fragment(resp)["ticket"]


async def _token(site) -> str:
    from pocketpaw_ee.sites import site_secrets

    return (await site_secrets.secrets_for_deploy(site))["PAW_GATEWAY_TOKEN"]


async def _redeem(c: AsyncClient, token: str | None, **body):
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await c.post("/api/v1/integrations/owner-sign-in/redeem", json=body, headers=headers)


async def _check(c: AsyncClient, token: str | None, **body):
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await c.post("/api/v1/integrations/owner-sign-in/check", json=body, headers=headers)


def _err(resp) -> str:
    body = resp.json()
    assert body["ok"] is False, body
    return body["error"]["code"]


async def _site_doc(pid: str):
    from pocketpaw_ee.cloud.models.site import Site

    return await Site.find_one(Site.pocket_id == pid)


# --------------------------------------------------------------------- issue


@pytest.mark.asyncio
async def test_the_owner_gets_a_one_time_link_with_the_ticket_in_the_fragment(beanie_test_db):
    pid = await _pocket()
    await _site(pid)
    async with _client() as c:
        resp = await _link(c, pid, next="/admin")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    url = urlsplit(body["url"])
    assert (url.scheme, url.netloc, url.path, url.query) == ("https", HOST, CALLBACK, "")
    frag = _fragment(resp)
    assert frag["next"] == "/admin" and len(frag["ticket"]) >= 32
    assert body["role"] == "owner" and body["expires_in"] == 60
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_an_unverified_paw_email_gets_no_link(beanie_test_db):
    pid = await _pocket()
    await _site(pid)
    async with _client(verified=False) as c:
        resp = await _link(c, pid)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "sites.owner_page_unverified"
    assert "Verify your email" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_the_first_link_on_a_site_without_its_token_asks_for_a_publish(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid, token=False)
    async with _client() as c:
        first = await _link(c, pid)
        second = await _link(c, pid)
    assert first.status_code == 409
    assert first.json()["error"]["code"] == "sites.owner_page_publish_needed"
    assert "Publish" in first.json()["error"]["message"]
    # The token now exists for the next publish to bind; once it does, links work.
    assert await _token(site)
    assert second.status_code == 200


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
    assert _fragment(resp)["next"] == "/"


@pytest.mark.asyncio
async def test_a_view_only_share_gets_no_site_role(beanie_test_db):
    """shared_with stores no access level, so a view share must not become staff."""
    pid = await _pocket()
    await _site(pid)
    await _with_team(pid, shared_with=[TEAMMATE])
    async with _client(TEAMMATE) as c:
        resp = await _link(c, pid)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sites.owner_page_not_collaborator"


@pytest.mark.asyncio
async def test_someone_no_longer_in_the_workspace_gets_no_link(beanie_test_db):
    pid = await _pocket()
    await _site(pid)
    await _with_team(pid, team=[TEAMMATE])
    async with _client(TEAMMATE, member=False) as c:
        resp = await _link(c, pid)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_team_member_gets_a_staff_link(beanie_test_db):
    pid = await _pocket()
    await _site(pid)
    await _with_team(pid, team=[TEAMMATE])
    async with _client(TEAMMATE, email="ravi@example.com") as c:
        resp = await _link(c, pid)
        ticket = _ticket(resp)
    assert resp.json()["role"] == "staff"
    site = await _site_doc(pid)
    async with _client() as c:
        redeemed = await _redeem(c, await _token(site), site_id=str(site.id), ticket=ticket)
    assert redeemed.json()["result"]["role"] == "staff"
    assert redeemed.json()["result"]["email"] == "ravi@example.com"


@pytest.mark.asyncio
async def test_a_plain_workspace_member_gets_nothing(beanie_test_db):
    pid = await _pocket()  # workspace-visible: EDITOR can edit it, but is no collaborator
    await _site(pid)
    async with _client(EDITOR) as c:
        resp = await _link(c, pid)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sites.owner_page_not_collaborator"


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
    assert denied.json()["error"]["code"] == "pocket.access_denied"
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


@pytest.mark.asyncio
async def test_each_link_is_audited_with_its_role(beanie_test_db, monkeypatch):
    from pocketpaw_ee.guards import audit

    seen: list[dict] = []
    monkeypatch.setattr(audit, "log_privileged_action", lambda **kw: seen.append(kw))
    pid = await _pocket()
    await _site(pid)
    async with _client() as c:
        ticket = _ticket(await _link(c, pid))
    assert seen and seen[-1]["action"] == "sites.owner_sign_in.issue"
    assert seen[-1]["actor"] == OWNER and seen[-1]["role"] == "owner"
    assert ticket not in json.dumps(seen, default=str)


# -------------------------------------------------------------------- redeem


@pytest.mark.asyncio
async def test_redeem_returns_the_verified_owner_once(beanie_test_db, caplog):
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
    assert result["role"] == "owner" and result["email_verified"] is True
    assert result["email"] == "asha@example.com" and result["name"] == "Asha Rao"
    assert len(result["account_id"]) == 64 and OWNER not in result["account_id"]
    assert first.headers["cache-control"] == "no-store"
    assert (replay.status_code, _err(replay)) == (400, "invalid_ticket")
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
    await _with_team(pid_a, team=[TEAMMATE])
    a1 = await _account(OWNER, pid_a, site_a)
    assert a1 == await _account(OWNER, pid_a, site_a)
    assert a1 != await _account(OWNER, pid_b, site_b)
    assert a1 != await _account(TEAMMATE, pid_a, site_a)


@pytest.mark.asyncio
async def test_another_sites_ticket_is_refused_and_burned(beanie_test_db):
    pid_a, pid_b = await _pocket(), await _pocket()
    site_a = await _site(pid_a)
    site_b = await _site(pid_b, url="https://other.pawsites.example", script="other-1")
    async with _client() as c:
        ticket = _ticket(await _link(c, pid_a))
        cross = await _redeem(c, await _token(site_b), site_id=str(site_b.id), ticket=ticket)
        after = await _redeem(c, await _token(site_a), site_id=str(site_a.id), ticket=ticket)
    assert (cross.status_code, _err(cross)) == (400, "invalid_ticket")
    assert (after.status_code, _err(after)) == (400, "invalid_ticket")


@pytest.mark.asyncio
async def test_a_bad_token_and_a_missing_site_answer_identically(beanie_test_db):
    pid_a, pid_b = await _pocket(), await _pocket()
    site_a = await _site(pid_a)
    site_b = await _site(pid_b, url="https://other.pawsites.example", script="other-1")
    async with _client() as c:
        ticket = _ticket(await _link(c, pid_a))
        sid = str(site_a.id)
        none = await _redeem(c, None, site_id=sid, ticket=ticket)
        wrong = await _redeem(c, "x" * 43, site_id=sid, ticket=ticket)
        other = await _redeem(c, await _token(site_b), site_id=sid, ticket=ticket)
        missing = await _redeem(c, "x" * 43, site_id="0" * 24, ticket=ticket)
        # A refused token never spends the ticket: the real site still can.
        ok = await _redeem(c, await _token(site_a), site_id=sid, ticket=ticket)
    bodies = {json.dumps(r.json(), sort_keys=True) for r in (none, wrong, other, missing)}
    assert len(bodies) == 1
    for resp in (none, wrong, other, missing):
        assert (resp.status_code, _err(resp)) == (401, "unauthorized")
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_failed_auth_is_capped_per_ip_and_site_without_spending_the_sites_bucket(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.integrations import owner_sign_in

    monkeypatch.setattr(owner_sign_in, "AUTH_FAIL_PER_MIN", 2)
    monkeypatch.setattr(owner_sign_in, "REDEEM_SITE_PER_MIN", 2)
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        ticket = _ticket(await _link(c, pid))
        bad = [
            (await _redeem(c, "x" * 43, site_id=str(site.id), ticket="t" * 43)).status_code
            for _ in range(3)
        ]
        # Bad tokens never touched the site's redeem bucket: the real Worker still gets in.
        ok = await _redeem(c, await _token(site), site_id=str(site.id), ticket=ticket)
    assert bad == [401, 401, 429]
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_redeem_is_capped_per_site(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import owner_sign_in

    monkeypatch.setattr(owner_sign_in, "REDEEM_SITE_PER_MIN", 2)
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        token = await _token(site)
        codes = [
            (await _redeem(c, token, site_id=str(site.id), ticket="t" * 43)).status_code
            for _ in range(3)
        ]
    assert codes == [400, 400, 429]


@pytest.mark.asyncio
async def test_an_expired_or_tampered_ticket_is_refused(beanie_test_db):
    from pocketpaw_ee.cloud._core import redis_client

    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        expired = _ticket(await _link(c, pid))
        await redis_client.get_redis().delete(f"site_owner_ticket_state:{expired}")
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


# --------------------------------------------------------------------- check


async def _user(email: str, *, active: bool = True, verified: bool = True, member: bool = True):
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

    user = User(
        email=email,
        hashed_password="x",
        is_active=active,
        is_verified=verified,
        workspaces=[WorkspaceMembership(workspace=WS)] if member else [],
    )
    await user.insert()
    return user


async def _account_for(user_id: str, pid: str, site) -> str:
    async with _client(user_id) as c:
        ticket = _ticket(await _link(c, pid))
        resp = await _redeem(c, await _token(site), site_id=str(site.id), ticket=ticket)
    return resp.json()["result"]["account_id"]


async def _roles(site, *accounts: str) -> list:
    token, sid = await _token(site), str(site.id)
    async with _client() as c:
        return [
            (await _check(c, token, site_id=sid, account_id=a)).json()["result"]["role"]
            for a in accounts
        ]


@pytest.mark.asyncio
async def test_check_answers_the_current_role_and_drops_removed_people(beanie_test_db):
    from pocketpaw_ee.cloud.models.pocket import Pocket

    owner, mate = await _user("asha@example.com"), await _user("ravi@example.com")
    pid = await _pocket(owner=str(owner.id))
    site = await _site(pid)
    await _with_team(pid, team=[str(mate.id)])
    owner_acct = await _account_for(str(owner.id), pid, site)
    mate_acct = await _account_for(str(mate.id), pid, site)
    assert await _roles(site, owner_acct, mate_acct, "f" * 64) == ["owner", "staff", None]

    await _with_team(pid)  # the teammate is taken off the team
    doc = await Pocket.get(pid)
    doc.owner = "user-new-owner"  # the pocket is transferred
    await doc.save()
    assert await _roles(site, owner_acct, mate_acct) == [None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offboard",
    ["removed from the workspace", "deactivated", "unverified"],
)
async def test_check_gives_an_offboarded_teammate_no_role(beanie_test_db, offboard):
    owner, mate = await _user("asha@example.com"), await _user("ravi@example.com")
    pid = await _pocket(owner=str(owner.id))
    site = await _site(pid)
    await _with_team(pid, team=[str(mate.id)])  # still on the pocket's team
    mate_acct = await _account_for(str(mate.id), pid, site)
    assert await _roles(site, mate_acct) == ["staff"]

    if offboard == "removed from the workspace":
        mate.workspaces = []
    elif offboard == "deactivated":
        mate.is_active = False
    else:
        mate.is_verified = False
    await mate.save()
    assert await _roles(site, mate_acct) == [None]


@pytest.mark.asyncio
async def test_check_needs_the_sites_own_token(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid)
    acct = await _account_for(OWNER, pid, site)
    async with _client() as c:
        wrong = await _check(c, "x" * 43, site_id=str(site.id), account_id=acct)
        none = await _check(c, None, site_id=str(site.id), account_id=acct)
        bad = await _check(c, await _token(site), site_id=str(site.id))
    assert (wrong.status_code, _err(wrong)) == (401, "unauthorized")
    assert (none.status_code, _err(none)) == (401, "unauthorized")
    assert (bad.status_code, _err(bad)) == (400, "invalid_request")


# ---------------------------------------------------------------------- peek


async def _peek(c: AsyncClient, token: str | None, **body):
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await c.post("/api/v1/integrations/owner-sign-in/peek", json=body, headers=headers)


@pytest.mark.asyncio
async def test_peek_shows_who_without_spending_the_ticket(beanie_test_db):
    pid = await _pocket()
    site = await _site(pid)
    async with _client() as c:
        ticket = _ticket(await _link(c, pid))
        token, sid = await _token(site), str(site.id)
        peeked = await _peek(c, token, site_id=sid, ticket=ticket)
        redeemed = await _redeem(c, token, site_id=sid, ticket=ticket)
    assert peeked.status_code == 200
    assert peeked.json()["result"] == {
        "name": "Asha Rao",
        "email": "asha@example.com",
        "role": "owner",
    }
    assert "account_id" not in peeked.json()["result"]
    assert redeemed.status_code == 200  # the peek did not spend it


@pytest.mark.asyncio
async def test_peek_needs_the_sites_token_and_its_own_ticket(beanie_test_db):
    pid_a, pid_b = await _pocket(), await _pocket()
    site_a = await _site(pid_a)
    site_b = await _site(pid_b, url="https://other.pawsites.example", script="other-1")
    async with _client() as c:
        ticket = _ticket(await _link(c, pid_a))
        wrong = await _peek(c, "x" * 43, site_id=str(site_a.id), ticket=ticket)
        cross = await _peek(c, await _token(site_b), site_id=str(site_b.id), ticket=ticket)
        gone = await _peek(c, await _token(site_a), site_id=str(site_a.id), ticket="t" * 43)
        still = await _redeem(c, await _token(site_a), site_id=str(site_a.id), ticket=ticket)
    assert (wrong.status_code, _err(wrong)) == (401, "unauthorized")
    assert (cross.status_code, _err(cross)) == (400, "invalid_ticket")
    assert (gone.status_code, _err(gone)) == (400, "invalid_ticket")
    assert still.status_code == 200  # neither peek spent it


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
    site = await _site(pid, token=False)
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
    site = await _site(pid, token=False)
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


# ------------------------------------------------- the platform-minted secret


@pytest.mark.asyncio
async def test_a_pending_row_filled_meanwhile_is_never_overwritten(beanie_test_db, monkeypatch):
    """ensure_generated_secret is insert-if-absent: a stale read of a pending row
    cannot replace a value someone set in between."""
    from pocketpaw_ee.sites import site_secrets

    pid = await _pocket()
    await site_secrets.request_secret(
        workspace_id=WS, user_id=OWNER, pocket_id=pid, name="SESSION_SECRET", description="x"
    )
    stale = await site_secrets._row(WS, pid, "SESSION_SECRET")
    await site_secrets.set_secret(
        workspace_id=WS, user_id=OWNER, pocket_id=pid, name="SESSION_SECRET", value="owner-value"
    )

    async def _stale_row(*_a):
        return stale

    real_row = site_secrets._row
    monkeypatch.setattr(site_secrets, "_row", _stale_row)
    minted = await site_secrets.ensure_generated_secret(
        workspace_id=WS, pocket_id=pid, name="SESSION_SECRET", description="d"
    )
    monkeypatch.setattr(site_secrets, "_row", real_row)
    assert minted is False
    deployed = await site_secrets.secrets_for_deploy(SimpleNamespace(workspace=WS, pocket_id=pid))
    assert deployed["SESSION_SECRET"] == "owner-value"


@pytest.mark.asyncio
async def test_two_mints_at_once_make_one_value(beanie_test_db):
    from pocketpaw_ee.sites import site_secrets

    pid = await _pocket()
    results = await asyncio.gather(
        *(
            site_secrets.ensure_generated_secret(
                workspace_id=WS, pocket_id=pid, name="BETTER_AUTH_SECRET", description="d"
            )
            for _ in range(2)
        )
    )
    assert sorted(results) == [False, True]
    listed = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert [s.name for s in listed.secrets] == ["BETTER_AUTH_SECRET"]
