# tests/cloud/test_pocket_auth_header_propagation.py — issue #1201.
# Created: 2026-05-24 — Runtime contract test for outbound auth header
# propagation across BOTH pocket HTTP executors (read + write).
#
# What this pins:
#   `_http_guard._auth_headers` is unit-tested directly for header SHAPE
#   (bearer / api_key / basic). What was missing — and what this file locks —
#   is the RUNTIME wire: that `source_executor.run_sources` (GET sources) and
#   `action_executor.run_action` (write actions) actually attach the built
#   header to the outbound HTTPX request for each of the three auth types.
#   Six cases total: 3 auth types x 2 executor paths.
#
# Why a separate file (rather than extending the per-executor test files):
#   the per-executor tests cover one executor each and grew a single bearer /
#   api_key / basic case in the SOURCE file only. The write path had no
#   header-propagation cases at all. This file is the single locked contract
#   for the auth wire across both paths so a future regression in either
#   executor surfaces in one obvious place.
#
# How:
#   patch `httpx.AsyncClient` inside each executor module to inject an
#   `httpx.MockTransport` that captures the outbound request's headers and
#   returns a stub `Response`. Same pattern as
#   `test_pocket_source_executor.py` / `test_pocket_action_executor.py`.

from __future__ import annotations

import base64

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.pockets import action_executor, source_executor  # noqa: E402

BASE = "https://api.example.com"


# ---------------------------------------------------------------------------
# Fixtures — clear rate-limit logs, force a public DNS answer for the guard.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    source_executor._run_log.clear()
    action_executor._action_log.clear()
    yield
    source_executor._run_log.clear()
    action_executor._action_log.clear()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    def _fake_getaddrinfo(host, *_args, **_kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo)


def _patch_source_client(monkeypatch, handler):
    """Wrap `source_executor.httpx.AsyncClient` with a MockTransport."""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(source_executor.httpx, "AsyncClient", _factory)


def _patch_action_client(monkeypatch, handler):
    """Wrap `action_executor.httpx.AsyncClient` with a MockTransport."""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _factory)


def _source_spec() -> dict:
    return {"sources": {"echo": {"method": "GET", "path": "/echo", "bind": "state.echo"}}}


def _write_action(path: str = "/echo") -> dict:
    return {"kind": "write_binding", "method": "POST", "path": path, "params": {}}


def _allow(path_pattern: str = "/echo") -> list[dict]:
    return [{"method": "POST", "path_pattern": path_pattern}]


# ---------------------------------------------------------------------------
# Sources/run — auth header reaches the outbound request
# ---------------------------------------------------------------------------


async def test_sources_run_propagates_bearer_header(monkeypatch):
    """`auth_type=bearer` → `Authorization: Bearer <token>` on the wire."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _patch_source_client(monkeypatch, handler)

    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="u1",
        ripple_spec=_source_spec(),
        base_url=BASE,
        auth_type="bearer",
        auth_header=None,
        token="tok-abc-123",
    )
    assert result["errors"] == []
    assert seen["authorization"] == "Bearer tok-abc-123"


async def test_sources_run_propagates_api_key_custom_header(monkeypatch):
    """`auth_type=api_key` with `auth_header=X-Custom-Key` → that header
    carries the token verbatim on the outbound request."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx normalizes header names to lowercase on access.
        seen["x-custom-key"] = request.headers.get("x-custom-key")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _patch_source_client(monkeypatch, handler)

    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="u1",
        ripple_spec=_source_spec(),
        base_url=BASE,
        auth_type="api_key",
        auth_header="X-Custom-Key",
        token="key-xyz",
    )
    assert result["errors"] == []
    assert seen["x-custom-key"] == "key-xyz"
    # api_key must not double-up as a bearer header.
    assert seen["authorization"] is None


async def test_sources_run_propagates_basic_header_base64_encoded(monkeypatch):
    """`auth_type=basic` → `Authorization: Basic <b64(user:pass)>`. The raw
    `user:pass` MUST NOT appear on the wire."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _patch_source_client(monkeypatch, handler)

    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="u1",
        ripple_spec=_source_spec(),
        base_url=BASE,
        auth_type="basic",
        auth_header=None,
        token="alice:s3cret",
    )
    assert result["errors"] == []
    expected = base64.b64encode(b"alice:s3cret").decode()
    assert seen["authorization"] == f"Basic {expected}"
    assert "alice:s3cret" not in (seen["authorization"] or "")


# ---------------------------------------------------------------------------
# Actions/run — auth header reaches the outbound request
# ---------------------------------------------------------------------------


async def test_actions_run_propagates_bearer_header(monkeypatch):
    """The write executor attaches `Authorization: Bearer <token>`."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _patch_action_client(monkeypatch, handler)

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="echo",
        raw_action=_write_action(),
        path="/echo",
        params={},
        base_url=BASE,
        auth_type="bearer",
        auth_header=None,
        token="tok-def-456",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True
    assert seen["authorization"] == "Bearer tok-def-456"


async def test_actions_run_propagates_api_key_custom_header(monkeypatch):
    """The write executor honors a custom header name for `api_key`."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x-internal-key"] = request.headers.get("x-internal-key")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _patch_action_client(monkeypatch, handler)

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="echo",
        raw_action=_write_action(),
        path="/echo",
        params={},
        base_url=BASE,
        auth_type="api_key",
        auth_header="X-Internal-Key",
        token="key-write-789",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True
    assert seen["x-internal-key"] == "key-write-789"
    assert seen["authorization"] is None


async def test_actions_run_propagates_basic_header_base64_encoded(monkeypatch):
    """The write executor base64-encodes `basic` credentials — the raw
    `user:pass` never reaches the wire."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _patch_action_client(monkeypatch, handler)

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="echo",
        raw_action=_write_action(),
        path="/echo",
        params={},
        base_url=BASE,
        auth_type="basic",
        auth_header=None,
        token="writer:w-pass",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True
    expected = base64.b64encode(b"writer:w-pass").decode()
    assert seen["authorization"] == f"Basic {expected}"
    assert "writer:w-pass" not in (seen["authorization"] or "")
