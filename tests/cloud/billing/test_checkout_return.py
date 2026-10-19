# tests/cloud/billing/test_checkout_return.py — the post-checkout RETURN hop.
#
# Repro for "Cross-site POST form submissions are forbidden" after paying on the
# plan checkout. Dodo routes Indian cards through Razorpay, and Razorpay sends
# the buyer back with a cross-site form POST to the checkout's return_url. That
# url used to be the APP's /settings/billing page: a SvelteKit server rejects the
# POST on its CSRF origin check, and the static SPA / Tauri build has no server to
# take a POST at all. The return_url now points at a public backend endpoint that
# accepts GET or POST, discards the body, validates the app origin against the
# CORS allowlist, and answers 303 so the browser lands on the app with a GET.

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.billing import service as billing

BACKEND = "https://api.pocketpaw.test"
APP = "https://app.acme.com"


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    """A deployment whose backend is BACKEND and whose CORS allowlist trusts APP."""
    from pocketpaw.api import cors

    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", BACKEND)
    monkeypatch.setattr(cors, "allowed_origins", lambda: [*cors.BUILTIN_ORIGINS, APP])


def _set_return_base(monkeypatch, value: str) -> None:
    from pocketpaw.config import get_settings

    monkeypatch.setattr(get_settings(), "dodo_checkout_return_base", value, raising=False)


@pytest_asyncio.fixture
async def client():
    """ONLY the public return router, with no auth or license override: the route
    must work for a caller holding no session at all."""
    from pocketpaw_ee.cloud.billing.checkout_return import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


# ---- the return urls handed to Dodo ------------------------------------- #


def test_return_urls_point_at_the_backend_return_endpoint():
    """THE REPRO: the gateway must return the buyer to the backend, never straight
    to the app page (which cannot accept Razorpay's cross-site POST)."""
    return_url, cancel_url = billing._checkout_return_urls(APP)

    for url, outcome in ((return_url, "success"), (cancel_url, "cancel")):
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == BACKEND
        assert parts.path == f"/api/v1/billing/checkout/return/{outcome}"
        assert parse_qs(parts.query) == {"to": [APP]}


def test_return_urls_fall_back_to_the_configured_base(monkeypatch):
    _set_return_base(monkeypatch, "https://app.example.com/")
    return_url, _ = billing._checkout_return_urls(None)
    assert parse_qs(urlsplit(return_url).query) == {"to": ["https://app.example.com"]}


def test_return_urls_without_any_origin_are_unchanged(monkeypatch):
    _set_return_base(monkeypatch, "")
    assert billing._checkout_return_urls(None) == (None, None)
    assert billing._checkout_return_urls("") == (None, None)


# ---- the endpoint -------------------------------------------------------- #


async def test_post_with_a_form_body_redirects_303_to_the_app(client):
    """Razorpay's cross-site form POST lands here and is turned into a GET."""
    resp = await client.post(
        "/api/v1/billing/checkout/return/success",
        params={"to": APP},
        data={"razorpay_payment_id": "pay_123", "razorpay_signature": "sig"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{APP}/settings/billing?checkout=success"


async def test_get_redirects_the_same_way(client):
    resp = await client.get("/api/v1/billing/checkout/return/success", params={"to": APP})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{APP}/settings/billing?checkout=success"


async def test_cancel_outcome(client):
    resp = await client.get("/api/v1/billing/checkout/return/cancel", params={"to": APP})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{APP}/settings/billing?checkout=cancel"


async def test_gateway_appended_params_do_not_change_the_outcome(client):
    """Dodo appends its own ``status`` / ids to the return url. The outcome lives in
    the path, so a gateway ``status=failed`` cannot rewrite it, and none of the
    gateway's params are forwarded to the app."""
    resp = await client.get(
        "/api/v1/billing/checkout/return/success",
        params={"to": APP, "status": "failed", "subscription_id": "sub_1"},
    )
    assert resp.headers["location"] == f"{APP}/settings/billing?checkout=success"


async def test_unknown_outcome_lands_without_a_checkout_flag(client):
    resp = await client.get("/api/v1/billing/checkout/return/hacked", params={"to": APP})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{APP}/settings/billing"


async def test_localhost_dev_origin_is_trusted(client):
    resp = await client.get(
        "/api/v1/billing/checkout/return/success", params={"to": "http://localhost:4173"}
    )
    assert resp.headers["location"] == "http://localhost:4173/settings/billing?checkout=success"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example",
        "https://app.acme.com.evil.example",
        "https://app.acme.com/path",
        "https://user@app.acme.com",
        "//evil.example",
        "javascript:alert(1)",
        "",
    ],
)
async def test_untrusted_target_never_redirects_off_site(client, monkeypatch, target):
    """Not on the allowlist and no configured default: a plain page, no Location."""
    _set_return_base(monkeypatch, "")
    resp = await client.post(
        "/api/v1/billing/checkout/return/success", params={"to": target}, data={"x": "1"}
    )
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert "text/html" in resp.headers["content-type"]
    assert "Payment received" in resp.text
    assert "evil" not in resp.text


async def test_untrusted_target_falls_back_to_the_configured_app(client, monkeypatch):
    _set_return_base(monkeypatch, "https://app.example.com")
    resp = await client.get(
        "/api/v1/billing/checkout/return/success", params={"to": "https://evil.example"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://app.example.com/settings/billing?checkout=success"


async def test_cancel_page_without_target_says_cancelled(client, monkeypatch):
    _set_return_base(monkeypatch, "")
    resp = await client.get("/api/v1/billing/checkout/return/cancel")
    assert resp.status_code == 200
    assert "Payment cancelled" in resp.text


async def test_post_body_is_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    await client.post(
        "/api/v1/billing/checkout/return/success",
        params={"to": "https://evil.example"},
        data={"razorpay_payment_id": "pay_SECRET_42"},
    )
    assert "pay_SECRET_42" not in caplog.text


def test_return_routes_carry_no_auth_or_license_dependency():
    from fastapi.routing import APIRoute
    from pocketpaw_ee.cloud.billing.checkout_return import router

    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    assert {m for r in routes for m in r.methods} == {"GET", "POST"}
    for route in routes:
        assert route.dependant.dependencies == []
