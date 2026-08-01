"""The /sessions runtime routes require a session, and are scoped to its owner.

Created 2026-08-01, resolving three of the ``REVIEW`` entries in
tests/cloud/auth/test_route_auth_audit.py. That audit measures the per-route
FastAPI dependency layer only, so an entry filed under "authorisation happens
inside the handler" has to be read by hand to know whether it does. These
three now carry route-level guards instead, and this file is the standing
assertion that they keep them.

Two invariants, both of which need holding together — either alone is not
enough:

  * **A session is required.** ``GET /sessions/runtime``,
    ``POST /sessions/runtime/create`` and ``POST /sessions/{id}/touch`` all
    resolve identity through the same dependencies as their sibling routes in
    this router.
  * **Reads and writes are scoped to that identity.** The listing is filtered
    to the caller's workspace AND owner in the store query, and ``touch``
    refuses a session the caller does not own. Authenticating without scoping
    would answer a valid request with rows that are not the caller's, so both
    halves are asserted separately below.

``POST /sessions/runtime/create`` mints a random string and touches nothing, so
it has nothing to scope. It is guarded anyway: "reachable without a session
but currently harmless" is a property that quietly stops holding the moment
somebody makes the handler persist something, and that change would not look
like a security change to whoever writes it.

Why this file exists rather than a manual check: ``localhost_auth_bypass``
defaults to True and grants full access to any loopback caller, so exercising
these by hand on a dev box cannot distinguish "requires auth" from "let me in
because I am on localhost". The assertions below run against the ASGI app with
no session at all.
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.cloud.sessions.router import router as sessions_router

_PASSWORD = "StrongPass123!"


@pytest_asyncio.fixture
async def app(mongo_db, monkeypatch):  # noqa: ARG001
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)

    # Pin the runtime-sessions route to the Mongo store. Without this it
    # resolves the process-wide memory manager, which in a test process is the
    # FILE store reading the developer's own ~/.pocketpaw/memory — so the test
    # would assert against whatever happens to be on the machine. Cloud always
    # runs Mongo here (``verify_cloud_memory_backend`` refuses to boot
    # otherwise), so this matches the deployment the guard is written for.
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

    class _Manager:
        _store = MongoMemoryStore()

    monkeypatch.setattr("pocketpaw.memory.get_memory_manager", lambda: _Manager())

    application = FastAPI()
    add_error_handler(application)
    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(sessions_router, prefix="/api/v1")
    # Entitlement, not identity — overridden so a licence check cannot be
    # mistaken for the auth result this file is measuring.
    application.dependency_overrides[require_license] = lambda: None
    return application


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest_asyncio.fixture
async def anon(app):
    async with _client(app) as client:
        yield client


async def _seed_member(email: str, workspace_name: str) -> tuple[User, Workspace]:
    """A user with a real workspace membership — the shape the guards expect."""
    async for db in get_user_db():
        user = await UserManager(db).create(UserCreate(email=email, password=_PASSWORD))
        break
    ws = Workspace(name=workspace_name, slug=workspace_name.lower(), owner=str(user.id))
    await ws.insert()
    user.workspaces.append(WorkspaceMembership(workspace=str(ws.id), role="owner"))
    user.active_workspace = str(ws.id)
    await user.save()
    return user, ws


async def _sign_in(client: AsyncClient, email: str) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code in (200, 204), resp.text


async def _seed_session(user: User, ws: Workspace, *, session_id: str, title: str) -> Session:
    doc = Session(
        sessionId=session_id,
        context_type="pocket",
        pocket="pk-1",
        workspace=str(ws.id),
        owner=str(user.id),
        title=title,
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# GET /sessions/runtime
# ---------------------------------------------------------------------------


async def test_runtime_session_list_requires_a_session(anon, app):
    alice, ws = await _seed_member("alice@acme.com", "Acme")
    await _seed_session(alice, ws, session_id="websocket_alice", title="Q3 revenue plan")

    resp = await anon.get("/api/v1/sessions/runtime")

    assert resp.status_code == 401, resp.text
    # Titles are user-authored content, so the scope check is protecting
    # more than metadata.
    assert "Q3 revenue plan" not in resp.text


async def test_runtime_session_list_is_scoped_to_the_caller(app):
    alice, alice_ws = await _seed_member("alice@acme.com", "Acme")
    bob, bob_ws = await _seed_member("bob@globex.com", "Globex")
    await _seed_session(alice, alice_ws, session_id="websocket_alice", title="Acme roadmap")
    await _seed_session(bob, bob_ws, session_id="websocket_bob", title="Globex layoffs")

    async with _client(app) as client:
        await _sign_in(client, "alice@acme.com")
        resp = await client.get("/api/v1/sessions/runtime")

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()["sessions"]}
    assert ids == {"websocket_alice"}
    assert "Globex layoffs" not in resp.text
    # ``total`` is part of the envelope and must count the same scoped set:
    # a scoped list with an unscoped total is still an unscoped answer.
    assert resp.json()["total"] == 1


async def test_runtime_session_list_returns_the_callers_own_sessions(app):
    alice, ws = await _seed_member("alice@acme.com", "Acme")
    await _seed_session(alice, ws, session_id="websocket_mine", title="Mine")

    async with _client(app) as client:
        await _sign_in(client, "alice@acme.com")
        resp = await client.get("/api/v1/sessions/runtime")

    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()["sessions"]] == ["websocket_mine"]


# ---------------------------------------------------------------------------
# POST /sessions/runtime/create
# ---------------------------------------------------------------------------


async def test_runtime_session_create_requires_a_session(anon):
    resp = await anon.post("/api/v1/sessions/runtime/create")
    assert resp.status_code == 401, resp.text


async def test_runtime_session_create_works_for_a_signed_in_caller(app):
    await _seed_member("alice@acme.com", "Acme")
    async with _client(app) as client:
        await _sign_in(client, "alice@acme.com")
        resp = await client.post("/api/v1/sessions/runtime/create")

    assert resp.status_code == 200
    assert resp.json()["id"].startswith("websocket_")


# ---------------------------------------------------------------------------
# POST /sessions/{session_id}/touch
# ---------------------------------------------------------------------------


async def test_touch_requires_a_session(anon, app):
    alice, ws = await _seed_member("alice@acme.com", "Acme")
    doc = await _seed_session(alice, ws, session_id="websocket_touch", title="Mine")
    before = (await Session.get(doc.id)).lastActivity

    resp = await anon.post("/api/v1/sessions/websocket_touch/touch")

    assert resp.status_code == 401, resp.text
    refreshed = await Session.get(doc.id)
    assert refreshed.lastActivity == before
    assert refreshed.messageCount == 0


async def test_touch_requires_session_ownership(app):
    alice, alice_ws = await _seed_member("alice@acme.com", "Acme")
    await _seed_member("bob@globex.com", "Globex")
    doc = await _seed_session(alice, alice_ws, session_id="websocket_alice", title="Mine")

    async with _client(app) as client:
        await _sign_in(client, "bob@globex.com")
        resp = await client.post("/api/v1/sessions/websocket_alice/touch")

    # Ownership, not merely authentication: touch writes to the document and
    # emits SessionUpdated onto the OWNER's realtime feed, so a caller who
    # cannot read the session must not be able to move it either.
    assert resp.status_code == 403, resp.text
    refreshed = await Session.get(doc.id)
    assert refreshed.messageCount == 0


async def test_touch_works_for_the_owner(app):
    alice, ws = await _seed_member("alice@acme.com", "Acme")
    doc = await _seed_session(alice, ws, session_id="websocket_owned", title="Mine")

    async with _client(app) as client:
        await _sign_in(client, "alice@acme.com")
        resp = await client.post("/api/v1/sessions/websocket_owned/touch")

    assert resp.status_code == 204, resp.text
    refreshed = await Session.get(doc.id)
    assert refreshed.messageCount == 1


async def test_touch_still_resolves_the_websocket_prefix_fallback(app):
    # ``touch`` strips a "websocket_" prefix when the literal id misses, for
    # callers that hold the prefixed key. Ownership must be enforced on the
    # session it lands on, not skipped because the first lookup failed.
    alice, ws = await _seed_member("alice@acme.com", "Acme")
    doc = await _seed_session(alice, ws, session_id="bare-id", title="Mine")

    async with _client(app) as client:
        await _sign_in(client, "alice@acme.com")
        resp = await client.post("/api/v1/sessions/websocket_bare-id/touch")

    assert resp.status_code == 204, resp.text
    assert (await Session.get(doc.id)).messageCount == 1


async def test_touching_a_missing_session_stays_quiet(app):
    # An unknown id is a no-op, not an error — this is a fire-and-forget
    # activity ping and a 404 storm from a stale client helps nobody. Note the
    # asymmetry with the not-owned case above, which DOES raise: a caller can
    # therefore tell "exists but isn't yours" from "doesn't exist". That is
    # already true of GET / PATCH / DELETE on the same id, so this route is
    # consistent with its siblings rather than opening anything new.
    await _seed_member("alice@acme.com", "Acme")
    async with _client(app) as client:
        await _sign_in(client, "alice@acme.com")
        resp = await client.post("/api/v1/sessions/websocket_nope/touch")

    assert resp.status_code == 204, resp.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/sessions/runtime"),
        ("POST", "/api/v1/sessions/runtime/create"),
        ("POST", "/api/v1/sessions/anything/touch"),
    ],
)
async def test_every_runtime_route_requires_a_session(anon, method, path):
    # Stated once over all three, so a route added to this family later
    # inherits the requirement instead of having to remember it.
    resp = await anon.request(method, path)
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
