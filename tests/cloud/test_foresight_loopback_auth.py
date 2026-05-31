# tests/cloud/test_foresight_loopback_auth.py
# Created: 2026-05-26 (feat/foresight-v12-skill-and-loopback-auth) — RFC 08
# v1.0 wave 4. Tests for ``loopback_or_request_context`` — the JWT-or-
# loopback dependency the foresight router now resolves through. Verifies:
#   - Loopback + full header trio → context built from headers, no JWT.
#   - Loopback but missing any header → fall through to JWT path (Forbidden
#     when no token).
#   - Non-loopback origin + header trio → fall through to JWT path.
#   - Internal header set to "false" → fall through.
#   - Loopback origin + JWT user (no internal header) → JWT path picks up.
#   - Cross-tenant attempt (workspace header doesn't match real user) →
#     loopback bypass trusts the header verbatim (dev-grade); the follow-up
#     PR tightens this to a signed JWT.
"""HTTP-layer tests for the foresight router's loopback auth bypass."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import (
    INTERNAL_HEADER,
    USER_HEADER,
    WORKSPACE_HEADER,
    RequestContext,
)
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.foresight.router import router as foresight_router
from pocketpaw_ee.cloud.license import require_license

# ---------------------------------------------------------------------------
# App harness — uses the REAL ``loopback_or_request_context`` dep (no
# override). The license dep stays overridden because licensing isn't the
# subject under test here.
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Build a foresight app with REAL auth dep — only license is stubbed."""
    app = FastAPI()
    add_error_handler(app)
    app.include_router(foresight_router)
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def loopback_client(mongo_db: Any) -> AsyncClient:
    """Client whose transport stamps a loopback client address.

    httpx's ``ASGITransport`` defaults the client scope's ``client`` to
    ``("127.0.0.1", 123)`` which is what ``request.client.host`` reads;
    that's the same address the local chat agent presents in production.
    """
    app = _build_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def non_loopback_client(mongo_db: Any) -> AsyncClient:
    """Client whose transport stamps a non-loopback address — simulates a
    public-internet caller. The bypass must refuse this even if the
    internal headers are present."""
    app = _build_app()
    transport = ASGITransport(app=app, client=("203.0.113.1", 1234))  # TEST-NET-3
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


# ---------------------------------------------------------------------------
# Loopback + full header trio → bypass succeeds
# ---------------------------------------------------------------------------


async def test_loopback_with_full_headers_grants_workspace_access(
    loopback_client: AsyncClient,
) -> None:
    """The chat-agent happy path: loopback + ``X-PocketPaw-Internal: true``
    + workspace/user ids → the foresight CRUD endpoints return real data
    (an empty list, here, since no scenarios exist)."""

    response = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w-loopback",
            USER_HEADER: "u-agent",
        },
    )

    assert response.status_code == 200
    body = response.json()
    # Empty workspace → empty items list, deterministic envelope.
    assert body == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }


async def test_loopback_bypass_threads_workspace_to_create(
    loopback_client: AsyncClient,
) -> None:
    """The workspace id from the header MUST propagate into the create
    flow — otherwise the loopback bypass would silently leak across
    tenants. We round-trip a create + list to confirm the doc lands in
    the same workspace the header named."""

    yaml_body = (
        "name: loopback-test\n"
        "sub_type: decision_forecast\n"
        "n_ticks: 1\n"
        "personas:\n"
        "  - name: anne\n"
        "    role: approver\n"
        "    ocean:\n"
        "      conscientiousness: 0.5\n"
    )

    create_resp = await loopback_client.post(
        "/foresight/scenarios/custom",
        json={
            "name": "Loopback Test",
            "sub_type": "decision_forecast",
            "yaml_body": yaml_body,
        },
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w-loopback",
            USER_HEADER: "u-agent",
        },
    )

    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["workspace_id"] == "w-loopback"
    assert created["name"] == "Loopback Test"

    # Cross-tenant check: a list call from a DIFFERENT workspace must NOT
    # see this scenario. The bypass trusts whatever workspace id it's
    # handed — tenant isolation rides on the service-level filter.
    list_resp_other = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w-other",
            USER_HEADER: "u-agent",
        },
    )
    assert list_resp_other.status_code == 200
    assert list_resp_other.json()["total"] == 0

    # Same workspace sees the doc.
    list_resp_same = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w-loopback",
            USER_HEADER: "u-agent",
        },
    )
    assert list_resp_same.status_code == 200
    assert list_resp_same.json()["total"] == 1


# ---------------------------------------------------------------------------
# Loopback but missing / wrong headers → fall through to JWT path
# ---------------------------------------------------------------------------


async def test_loopback_without_internal_header_falls_through(
    loopback_client: AsyncClient,
) -> None:
    """Loopback origin but ``X-PocketPaw-Internal`` absent — the bypass
    declines and the JWT path fires; with no token present, that returns
    a 403 ``auth.required``."""

    response = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            WORKSPACE_HEADER: "w-loopback",
            USER_HEADER: "u-agent",
            # Crucially no INTERNAL_HEADER.
        },
    )

    assert response.status_code == 403
    body = response.json()
    assert body == {"error": {"code": "auth.required", "message": "Authentication required"}}


async def test_loopback_with_internal_false_falls_through(
    loopback_client: AsyncClient,
) -> None:
    """``X-PocketPaw-Internal: false`` (or any non-``true`` value) MUST
    NOT activate the bypass. A literal string match against
    ``"true"`` (case-insensitive) is the only accepted form."""

    response = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "false",
            WORKSPACE_HEADER: "w-loopback",
            USER_HEADER: "u-agent",
        },
    )

    assert response.status_code == 403


async def test_loopback_missing_workspace_header_falls_through(
    loopback_client: AsyncClient,
) -> None:
    """Internal header + user id but no workspace id → bypass declines.
    A missing workspace would collapse the tenancy filter to ``None``
    and let the service read across tenants; reject early."""

    response = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "true",
            USER_HEADER: "u-agent",
        },
    )

    assert response.status_code == 403


async def test_loopback_missing_user_header_falls_through(
    loopback_client: AsyncClient,
) -> None:
    """Internal header + workspace id but no user id → bypass declines.
    A missing user collapses authorship and per-user audit, so reject."""

    response = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w-loopback",
        },
    )

    assert response.status_code == 403


async def test_loopback_blank_workspace_header_falls_through(
    loopback_client: AsyncClient,
) -> None:
    """Empty string workspace id is rejected by the same code path as
    missing — explicit guard against a header-injection that strips the
    value but keeps the name."""

    response = await loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "   ",
            USER_HEADER: "u-agent",
        },
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Non-loopback origin → bypass refuses even with full header trio
# ---------------------------------------------------------------------------


async def test_non_loopback_with_full_headers_falls_through(
    non_loopback_client: AsyncClient,
) -> None:
    """A public-internet caller can forge the headers but ``request.client.host``
    won't be a loopback address. The bypass MUST refuse — otherwise any
    external caller could mint workspace access by setting three headers."""

    response = await non_loopback_client.get(
        "/foresight/scenarios/custom",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w-loopback",
            USER_HEADER: "u-agent",
        },
    )

    assert response.status_code == 403
    body = response.json()
    assert body == {"error": {"code": "auth.required", "message": "Authentication required"}}


# ---------------------------------------------------------------------------
# RequestContext helper smokes — pure-Python checks on the
# ``_try_loopback_context`` decision logic without the FastAPI harness.
# ---------------------------------------------------------------------------


def _stub_request(
    *,
    host: str | None = "127.0.0.1",
    headers: dict[str, str] | None = None,
) -> Any:
    """Build a stand-in object compatible with the ``Request`` shape the
    helper inspects — ``request.client.host`` + ``request.headers.get``."""

    class _Client:
        def __init__(self, h: str | None) -> None:
            self.host = h

    class _Stub:
        def __init__(self) -> None:
            self.client = _Client(host) if host is not None else None
            self.headers = headers or {}

    return _Stub()


def test_try_loopback_context_returns_context_on_full_trio() -> None:
    from pocketpaw_ee.cloud._core.context import _try_loopback_context

    req = _stub_request(
        host="127.0.0.1",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w1",
            USER_HEADER: "u1",
        },
    )

    ctx = _try_loopback_context(req)
    assert isinstance(ctx, RequestContext)
    assert ctx.workspace_id == "w1"
    assert ctx.user_id == "u1"
    assert isinstance(ctx.started_at, datetime)
    assert ctx.started_at.tzinfo is UTC


def test_try_loopback_context_returns_none_on_non_loopback_host() -> None:
    from pocketpaw_ee.cloud._core.context import _try_loopback_context

    req = _stub_request(
        host="10.0.0.1",  # private but not loopback
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w1",
            USER_HEADER: "u1",
        },
    )

    assert _try_loopback_context(req) is None


def test_try_loopback_context_returns_none_on_no_client() -> None:
    """Test apps without an explicit client transport collapse
    ``request.client`` to ``None``. The helper must fail closed."""

    from pocketpaw_ee.cloud._core.context import _try_loopback_context

    req = _stub_request(
        host=None,
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w1",
            USER_HEADER: "u1",
        },
    )

    assert _try_loopback_context(req) is None


def test_try_loopback_context_accepts_ipv6_loopback() -> None:
    """``::1`` is the IPv6 loopback — dual-stack ASGI servers report it
    when the client connects via ``localhost`` on a v6-enabled host."""

    from pocketpaw_ee.cloud._core.context import _try_loopback_context

    req = _stub_request(
        host="::1",
        headers={
            INTERNAL_HEADER: "true",
            WORKSPACE_HEADER: "w1",
            USER_HEADER: "u1",
        },
    )

    ctx = _try_loopback_context(req)
    assert ctx is not None
    assert ctx.workspace_id == "w1"


def test_try_loopback_context_case_insensitive_internal_flag() -> None:
    """``true``, ``TRUE``, and ``True`` all activate the bypass; anything
    else (``yes``, ``1``, ``on``) does NOT."""

    from pocketpaw_ee.cloud._core.context import _try_loopback_context

    for value in ("true", "TRUE", "True", " true "):
        req = _stub_request(
            host="127.0.0.1",
            headers={
                INTERNAL_HEADER: value,
                WORKSPACE_HEADER: "w1",
                USER_HEADER: "u1",
            },
        )
        assert _try_loopback_context(req) is not None, f"expected accept for {value!r}"

    for value in ("yes", "1", "on", "true ish", ""):
        req = _stub_request(
            host="127.0.0.1",
            headers={
                INTERNAL_HEADER: value,
                WORKSPACE_HEADER: "w1",
                USER_HEADER: "u1",
            },
        )
        assert _try_loopback_context(req) is None, f"expected refuse for {value!r}"
