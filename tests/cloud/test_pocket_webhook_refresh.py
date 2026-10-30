# tests/cloud/test_pocket_webhook_refresh.py — RFC 04 M3.
# Created: 2026-05-22 — Coverage for the pocket data-source webhook-refresh
# routes:
#
#   POST /pockets/{id}/sources/{source}/refresh — inbound webhook trigger
#   GET  /pockets/{id}/backend/webhook          — read the webhook secret
#   POST /pockets/{id}/backend/webhook/rotate   — rotate the webhook secret
#
# The service functions + the source executor are monkeypatched so the
# tests pin the route wiring (secret auth, status codes, the not-an-oracle
# property) without a Mongo connection or real outbound HTTP.
#
# What this pins:
#   - A valid webhook secret refreshes the named source.
#   - A wrong / missing secret is rejected with 403.
#   - The 403 is IDENTICAL whether or not the pocket exists — the endpoint
#     is not a tenant-existence oracle.
#   - A valid secret cannot run a source that is not webhook-refresh.
#   - The auto-refresh budget caps a webhook flood (skipped, not queued).

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.pockets import (
    _refresh_budget,  # noqa: E402
    source_executor,  # noqa: E402
)
from pocketpaw_ee.cloud.pockets import service as pockets_service  # noqa: E402
from pocketpaw_ee.cloud.pockets.router import router  # noqa: E402
from pocketpaw_ee.cloud.shared.deps import (  # noqa: E402
    current_user_id,
    current_workspace_id,
    require_pocket_owner,
)

FAKE_USER = "user-alice"
FAKE_WORKSPACE = "ws-alpha"
GOOD_SECRET = "s3cret-webhook-token"

# Executor-creds tuple as resolve_webhook_pocket returns it (with the
# trailing workspace_id).
_CREDS = ("https://api.example.com", "none", None, "", [], None, FAKE_WORKSPACE)


@pytest.fixture(autouse=True)
def _reset_budget():
    _refresh_budget.reset_budget()
    yield
    _refresh_budget.reset_budget()


@pytest.fixture
def app() -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler

    a = FastAPI()
    add_error_handler(a)
    a.include_router(router)
    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[require_pocket_owner] = lambda: None
    a.dependency_overrides[current_user_id] = lambda: FAKE_USER
    a.dependency_overrides[current_workspace_id] = lambda: FAKE_WORKSPACE
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _patch_webhook_source(monkeypatch, *, secret=GOOD_SECRET, source_refresh=("webhook",)):
    """Patch the service so one pocket has a webhook source + secret."""

    async def _resolve(pocket_id, presented):
        # Mirrors the real service: only a non-empty matching secret wins.
        if presented and secret and presented == secret:
            return _CREDS
        return None

    async def _spec(_ws, _pid):
        return {
            "sources": {
                "prs": {
                    "method": "GET",
                    "path": "/pulls",
                    "bind": "state.prs",
                    "refresh": list(source_refresh),
                }
            }
        }

    monkeypatch.setattr(pockets_service, "resolve_webhook_pocket", _resolve)
    monkeypatch.setattr(pockets_service, "get_pocket_ripple_spec", _spec)


def _patch_executor(monkeypatch, calls: list):
    async def _fake_run_sources(*, pocket_id, only_source=None, **_kw):
        calls.append((pocket_id, only_source))
        return {"ran": [{"source": only_source, "bind": "prs", "value": [1]}], "errors": []}

    monkeypatch.setattr(source_executor, "run_sources", _fake_run_sources)


# ---------------------------------------------------------------------------
# POST /pockets/{id}/sources/{source}/refresh — valid secret
# ---------------------------------------------------------------------------


def test_webhook_refresh_runs_source_on_valid_secret(monkeypatch, client):
    calls: list = []
    _patch_webhook_source(monkeypatch)
    _patch_executor(monkeypatch, calls)

    res = client.post(
        "/pockets/pocket-1/sources/prs/refresh",
        headers={"X-Pocket-Webhook-Secret": GOOD_SECRET},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ran"][0]["source"] == "prs"
    assert calls == [("pocket-1", "prs")]


# ---------------------------------------------------------------------------
# Invalid / missing secret — rejected, and not an oracle
# ---------------------------------------------------------------------------


def test_webhook_refresh_rejects_wrong_secret(monkeypatch, client):
    _patch_webhook_source(monkeypatch)
    _patch_executor(monkeypatch, [])

    res = client.post(
        "/pockets/pocket-1/sources/prs/refresh",
        headers={"X-Pocket-Webhook-Secret": "wrong-secret"},
    )
    assert res.status_code == 403


def test_webhook_refresh_rejects_missing_secret(monkeypatch, client):
    _patch_webhook_source(monkeypatch)
    _patch_executor(monkeypatch, [])

    res = client.post("/pockets/pocket-1/sources/prs/refresh")
    assert res.status_code == 403


def test_webhook_refresh_same_error_for_missing_pocket(monkeypatch, client):
    """The not-an-oracle property — a wrong secret on a REAL pocket and a
    wrong secret on a NON-EXISTENT pocket return the byte-identical 403."""

    async def _resolve(_pocket_id, _presented):
        # The real service returns None for BOTH 'bad secret' and 'no such
        # pocket' — so the route cannot tell them apart.
        return None

    monkeypatch.setattr(pockets_service, "resolve_webhook_pocket", _resolve)

    real = client.post(
        "/pockets/real-pocket/sources/prs/refresh",
        headers={"X-Pocket-Webhook-Secret": "wrong"},
    )
    missing = client.post(
        "/pockets/does-not-exist/sources/prs/refresh",
        headers={"X-Pocket-Webhook-Secret": "wrong"},
    )
    assert real.status_code == missing.status_code == 403
    # Identical body — no distinguishing detail leaks.
    assert real.json() == missing.json()


# ---------------------------------------------------------------------------
# Valid secret cannot run a non-webhook source
# ---------------------------------------------------------------------------


def test_valid_secret_cannot_run_non_webhook_source(monkeypatch, client):
    """A source whose refresh policy lacks `webhook` is not runnable via
    the webhook endpoint even with a valid secret — returns 404."""
    calls: list = []
    # The 'prs' source is pocket_open/manual only — NOT webhook.
    _patch_webhook_source(monkeypatch, source_refresh=("pocket_open", "manual"))
    _patch_executor(monkeypatch, calls)

    res = client.post(
        "/pockets/pocket-1/sources/prs/refresh",
        headers={"X-Pocket-Webhook-Secret": GOOD_SECRET},
    )
    assert res.status_code == 404
    assert calls == []


def test_valid_secret_unknown_source_is_404(monkeypatch, client):
    _patch_webhook_source(monkeypatch)
    _patch_executor(monkeypatch, [])

    res = client.post(
        "/pockets/pocket-1/sources/nonexistent/refresh",
        headers={"X-Pocket-Webhook-Secret": GOOD_SECRET},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Budget caps a webhook flood
# ---------------------------------------------------------------------------


def test_webhook_flood_is_capped_by_budget(monkeypatch, client):
    """Once the per-pocket auto-refresh budget is spent, further webhook
    hits return 200 with a `skipped` marker — NOT queued, no backend call."""
    calls: list = []
    _patch_webhook_source(monkeypatch)
    _patch_executor(monkeypatch, calls)
    monkeypatch.setattr(_refresh_budget, "max_per_hour", lambda: 2)

    statuses = []
    for _ in range(5):
        r = client.post(
            "/pockets/pocket-1/sources/prs/refresh",
            headers={"X-Pocket-Webhook-Secret": GOOD_SECRET},
        )
        statuses.append(r.json())

    # First 2 hits run the source; the rest are skipped (rate-limited).
    assert len(calls) == 2
    skipped = [s for s in statuses if s.get("skipped") == "rate_limited"]
    assert len(skipped) == 3


# ---------------------------------------------------------------------------
# GET / rotate webhook secret (owner-only)
# ---------------------------------------------------------------------------


def test_get_webhook_secret_returns_secret(monkeypatch, client):
    async def _get(_ws, _pid):
        return GOOD_SECRET

    monkeypatch.setattr(pockets_service, "get_webhook_secret", _get)
    res = client.get("/pockets/pocket-1/backend/webhook")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["secret"] == GOOD_SECRET
    assert body["pocket_id"] == "pocket-1"
    assert "/sources/{source}/refresh" in body["refresh_path"]


def test_get_webhook_secret_none_before_rotate(monkeypatch, client):
    async def _get(_ws, _pid):
        return None

    monkeypatch.setattr(pockets_service, "get_webhook_secret", _get)
    res = client.get("/pockets/pocket-1/backend/webhook")
    assert res.status_code == 200
    assert res.json()["secret"] is None


def test_rotate_webhook_secret_returns_new_secret(monkeypatch, client):
    captured = {}

    async def _rotate(workspace_id, user_id, pocket_id):
        captured.update(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
        return "freshly-rotated-secret"

    monkeypatch.setattr(pockets_service, "rotate_webhook_secret", _rotate)
    res = client.post("/pockets/pocket-1/backend/webhook/rotate")
    assert res.status_code == 200, res.text
    assert res.json()["secret"] == "freshly-rotated-secret"
    assert captured == {
        "workspace_id": FAKE_WORKSPACE,
        "user_id": FAKE_USER,
        "pocket_id": "pocket-1",
    }
