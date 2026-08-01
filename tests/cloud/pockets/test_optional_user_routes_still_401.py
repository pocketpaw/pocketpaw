"""The four pockets routes that authorise inside the handler, not at the door.

Created 2026-08-01. These take fastapi-users' OPTIONAL user dependency, so a
caller with no session reaches the handler and the routing layer makes no
decision. That is legitimate — each has a reason it cannot use a normal guard,
from the loopback internal bypass to a per-pocket webhook secret — but it means
tests/cloud/auth/test_route_auth_audit.py cannot measure them, and until now
nothing did.

They were read by hand and found sound, and the audit's allowlist now names the
specific in-handler check for each. This file exists because a named claim that
nothing enforces decays: a refactor that drops the 401 branch out of
``_resolve_reconcile_identity`` would leave the audit still cheerfully
asserting the check is there. The point is not to re-prove authorisation logic
the pockets suites already cover — it is to pin the ONE property the audit
takes on faith, that a caller with no session is refused.

Note why this cannot be checked by hand on a dev box: ``localhost_auth_bypass``
defaults to True and grants full access to loopback callers, so a manual curl
cannot distinguish "requires auth" from "let me in because I am on localhost".
These run against the ASGI app with no session at all.
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets.router import router as pockets_router


@pytest_asyncio.fixture
async def anon(mongo_db):  # noqa: ARG001
    app = FastAPI()
    add_error_handler(app)
    app.include_router(pockets_router, prefix="/api/v1")
    # Entitlement, not identity — overridden so a licence denial cannot be
    # mistaken for the auth refusal this file is measuring.
    app.dependency_overrides[require_license] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.parametrize(
    ("path", "body", "check"),
    [
        # Router raises 401 when neither the loopback internal bypass nor a
        # session yields an identity; merge_spec then re-checks edit access.
        ("/api/v1/pockets/pk-1/spec/merge", {"merge": {"state": {}}}, "auth.required"),
        # _resolve_reconcile_identity raises the same 401 for both reconcile
        # routes, which is why they share a helper.
        ("/api/v1/pockets/pk-1/reconcile/preview", None, "auth.required"),
        ("/api/v1/pockets/pk-1/reconcile/apply", None, "auth.required"),
    ],
)
async def test_a_caller_with_no_session_is_refused(anon, path, body, check):
    resp = await anon.post(path, json=body) if body else await anon.post(path)

    assert resp.status_code == 401, f"{path} -> {resp.status_code}: {resp.text}"
    assert check in resp.text


async def test_identity_is_resolved_before_the_pocket_is_looked_up(anon):
    # Both pocket ids below are nonexistent. Identical 401s mean the identity
    # check runs first, so the route cannot be used to learn which pocket ids
    # exist.
    a = await anon.post("/api/v1/pockets/definitely-not-real/reconcile/preview")
    b = await anon.post("/api/v1/pockets/also-not-real/reconcile/preview")

    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


async def test_the_webhook_refresh_route_refuses_a_missing_secret(anon):
    # The odd one out: authenticated by a per-pocket secret rather than a
    # session, because the caller is an upstream backend with no PocketPaw
    # account. A wrong secret, a missing secret, and a pocket that does not
    # exist all return the same 403, which keeps it from answering "does this
    # pocket exist".
    resp = await anon.post("/api/v1/pockets/pk-1/sources/src-1/refresh")

    assert resp.status_code == 403, resp.text
    assert "pocket_webhook.unauthorized" in resp.text


async def test_the_webhook_route_answers_a_wrong_secret_and_a_missing_pocket_alike(anon):
    wrong_secret = await anon.post(
        "/api/v1/pockets/pk-1/sources/src-1/refresh",
        headers={"X-Pocket-Webhook-Secret": "not-the-secret"},
    )
    missing_pocket = await anon.post(
        "/api/v1/pockets/no-such-pocket/sources/src-1/refresh",
        headers={"X-Pocket-Webhook-Secret": "not-the-secret"},
    )

    assert wrong_secret.status_code == missing_pocket.status_code == 403
    assert wrong_secret.json() == missing_pocket.json()
