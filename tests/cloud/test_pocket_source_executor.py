# tests/cloud/test_pocket_source_executor.py — RFC 04 alpha.
# Created: 2026-05-21 — Coverage for the read-only pocket source executor,
# the SSRF boundary of the feature. No real network calls — outbound HTTP
# is faked via httpx.MockTransport and socket.getaddrinfo is monkeypatched
# so fake hostnames "resolve" to a public IP.
# Updated: 2026-05-21 (PR #1177 security pass) — run_sources now takes a
# required user_id; added coverage for basic-auth base64 encoding, the
# per-(pocket, user) rate-limit key, the async-safe limiter under
# asyncio.gather, and the audit-log entry written for every run.
#
# What this pins:
#   - SSRF rejections: absolute-URL path, `..` traversal, path to a
#     different host, internal base_url, host that resolves internal.
#   - Response-size cap (512 KB).
#   - Refresh-policy selection: pocket_open vs manual vs only_source vs all.
#   - bind-path `state.` stripping.
#   - Happy path with a mocked transport returning JSON.
#   - Redirect -> source error (redirects not followed).
#   - Per-(pocket, user) rate-limit breach + async-safety.
#   - Auth header shaping per auth_type (bearer / api_key / basic).
#   - Audit-log entry written per run.

from __future__ import annotations

import json

import httpx
import pytest
from pocketpaw_ee.cloud.pockets import source_executor


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """Clear the module-level rate-limit log between tests."""
    source_executor._run_log.clear()
    yield
    source_executor._run_log.clear()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Make every hostname resolve to a public IP so the DNS guard passes.

    Individual tests that need an internal-resolving host override this.
    """

    def _fake_getaddrinfo(host, *_args, **_kwargs):
        # 8.8.8.8 — a genuinely public IP (TEST-NET ranges read as
        # is_private=True on modern Python and would trip the guard).
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo)


def _mock_client_patch(monkeypatch, handler):
    """Patch httpx.AsyncClient so the executor uses a MockTransport.

    The executor builds its own AsyncClient; we wrap the constructor to
    inject ``transport=MockTransport(handler)`` while preserving the
    follow_redirects=False / timeout settings the executor passes.
    """
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(source_executor.httpx, "AsyncClient", _factory)


BASE = "https://api.example.com"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_returns_parsed_json(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pulls"
        return httpx.Response(200, json=[{"id": 1, "title": "PR one"}])

    _mock_client_patch(monkeypatch, handler)

    spec = {"sources": {"prs": {"method": "GET", "path": "/pulls", "bind": "state.prs"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["errors"] == []
    assert len(result["ran"]) == 1
    ran = result["ran"][0]
    assert ran["source"] == "prs"
    assert ran["bind"] == "prs"  # `state.` stripped
    assert ran["value"] == [{"id": 1, "title": "PR one"}]


async def test_bind_without_state_prefix_passes_through(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "prs"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"][0]["bind"] == "prs"


# ---------------------------------------------------------------------------
# SSRF rejections
# ---------------------------------------------------------------------------


async def test_absolute_url_path_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    spec = {"sources": {"s": {"method": "GET", "path": "https://evil.com/x", "bind": "x"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"] == []
    assert result["errors"][0]["source"] == "s"
    assert result["errors"][0]["code"] == "bad_path"


async def test_dotdot_traversal_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    spec = {"sources": {"s": {"method": "GET", "path": "/a/../../etc/passwd", "bind": "x"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"] == []
    assert result["errors"][0]["code"] == "bad_path"


async def test_encoded_dotdot_traversal_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    spec = {"sources": {"s": {"method": "GET", "path": "/a/%2e%2e/secret", "bind": "x"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"] == []
    assert result["errors"][0]["code"] == "bad_path"


async def test_protocol_relative_path_to_other_host_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    # //evil.com/x has a netloc -> absolute, must be rejected.
    spec = {"sources": {"s": {"method": "GET", "path": "//evil.com/x", "bind": "x"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"] == []
    assert result["errors"][0]["code"] == "bad_path"


async def test_internal_base_url_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    with pytest.raises(ValueError):
        await source_executor.run_sources(
            pocket_id="p1",
            user_id="runner-1",
            ripple_spec=spec,
            base_url="http://127.0.0.1",
            auth_type="none",
            auth_header=None,
            token="",
        )


async def test_host_resolving_internal_rejected(monkeypatch):
    """DNS rebinding guard — a public name resolving to a private IP."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))

    def _internal_getaddrinfo(host, *_a, **_k):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _internal_getaddrinfo)

    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"] == []
    assert result["errors"][0]["code"] == "bad_host"


# ---------------------------------------------------------------------------
# Response-size cap
# ---------------------------------------------------------------------------


async def test_oversize_response_rejected(monkeypatch):
    big = json.dumps([{"x": "a" * 1000}] * 600)  # > 512 KB
    assert len(big.encode()) > source_executor._MAX_RESPONSE_BYTES

    _mock_client_patch(
        monkeypatch,
        lambda r: httpx.Response(200, content=big, headers={"content-type": "application/json"}),
    )
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"] == []
    assert result["errors"][0]["code"] == "too_large"


# ---------------------------------------------------------------------------
# Refresh-policy selection
# ---------------------------------------------------------------------------


def _multi_source_spec() -> dict:
    return {
        "sources": {
            "open_only": {"method": "GET", "path": "/a", "bind": "a", "refresh": ["pocket_open"]},
            "manual_only": {"method": "GET", "path": "/b", "bind": "b", "refresh": ["manual"]},
            "both": {
                "method": "GET",
                "path": "/c",
                "bind": "c",
                "refresh": ["pocket_open", "manual"],
            },
        }
    }


async def test_trigger_pocket_open_selects_open_sources(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=_multi_source_spec(),
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        trigger="pocket_open",
    )
    ran_sources = {r["source"] for r in result["ran"]}
    assert ran_sources == {"open_only", "both"}


async def test_trigger_manual_selects_manual_sources(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=_multi_source_spec(),
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        trigger="manual",
    )
    ran_sources = {r["source"] for r in result["ran"]}
    assert ran_sources == {"manual_only", "both"}


async def test_only_source_runs_just_that_source(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=_multi_source_spec(),
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        only_source="manual_only",
    )
    assert {r["source"] for r in result["ran"]} == {"manual_only"}


async def test_no_trigger_runs_all_sources(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=_multi_source_spec(),
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert {r["source"] for r in result["ran"]} == {"open_only", "manual_only", "both"}


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


async def test_redirect_becomes_source_error(monkeypatch):
    _mock_client_patch(
        monkeypatch,
        lambda r: httpx.Response(302, headers={"location": "https://evil.com/x"}),
    )
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    result = await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert result["ran"] == []
    assert result["errors"][0]["code"] == "redirect"


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


async def test_rate_limit_breach_returns_rate_limited(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}

    # Burn the budget (10 runs/min).
    for _ in range(source_executor._RATE_LIMIT_MAX):
        await source_executor.run_sources(
            pocket_id="p-rl",
            user_id="runner-1",
            ripple_spec=spec,
            base_url=BASE,
            auth_type="none",
            auth_header=None,
            token="",
        )

    breach = await source_executor.run_sources(
        pocket_id="p-rl",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert breach["ran"] == []
    assert breach["errors"][0]["code"] == "rate_limited"


async def test_rate_limit_is_per_pocket(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    for _ in range(source_executor._RATE_LIMIT_MAX):
        await source_executor.run_sources(
            pocket_id="pocket-a",
            user_id="runner-1",
            ripple_spec=spec,
            base_url=BASE,
            auth_type="none",
            auth_header=None,
            token="",
        )
    # A different pocket still has its full budget.
    other = await source_executor.run_sources(
        pocket_id="pocket-b",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert len(other["ran"]) == 1


# ---------------------------------------------------------------------------
# Auth header shaping
# ---------------------------------------------------------------------------


async def test_bearer_auth_header(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="bearer",
        auth_header=None,
        token="tok123",
    )
    assert seen["auth"] == "Bearer tok123"


async def test_api_key_custom_header(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-internal-key")
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="api_key",
        auth_header="X-Internal-Key",
        token="abc",
    )
    assert seen["key"] == "abc"


async def test_basic_auth_header_is_base64_encoded(monkeypatch):
    """BLOCKER-1 — `basic` auth must base64-encode the user:pass credential."""
    import base64

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    await source_executor.run_sources(
        pocket_id="p1",
        user_id="runner-1",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="basic",
        auth_header=None,
        token="alice:s3cret",
    )
    expected = base64.b64encode(b"alice:s3cret").decode()
    assert seen["auth"] == f"Basic {expected}"
    # The raw user:pass must never appear unencoded on the wire.
    assert "alice:s3cret" not in seen["auth"]


# ---------------------------------------------------------------------------
# Rate limit — per (pocket, user) key + async safety
# ---------------------------------------------------------------------------


async def test_rate_limit_is_per_user(monkeypatch):
    """SHOULD-FIX-2 — one member exhausting a pocket's budget must not
    starve another member of the same shared pocket."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}

    # alice burns her whole budget on the shared pocket.
    for _ in range(source_executor._RATE_LIMIT_MAX):
        await source_executor.run_sources(
            pocket_id="shared-pocket",
            user_id="alice",
            ripple_spec=spec,
            base_url=BASE,
            auth_type="none",
            auth_header=None,
            token="",
        )
    alice_breach = await source_executor.run_sources(
        pocket_id="shared-pocket",
        user_id="alice",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert alice_breach["errors"][0]["code"] == "rate_limited"

    # bob still has his full budget on the SAME pocket.
    bob = await source_executor.run_sources(
        pocket_id="shared-pocket",
        user_id="bob",
        ripple_spec=spec,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
    )
    assert len(bob["ran"]) == 1


async def test_rate_limit_async_safe_under_gather(monkeypatch):
    """SHOULD-FIX-3 — concurrent runs must not race past the limit.

    Fires 4x the budget concurrently; with the lock, exactly
    _RATE_LIMIT_MAX runs are permitted and the rest are rate-limited.
    """
    import asyncio

    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}

    total = source_executor._RATE_LIMIT_MAX * 4
    results = await asyncio.gather(
        *(
            source_executor.run_sources(
                pocket_id="race-pocket",
                user_id="racer",
                ripple_spec=spec,
                base_url=BASE,
                auth_type="none",
                auth_header=None,
                token="",
            )
            for _ in range(total)
        )
    )
    permitted = sum(1 for r in results if r["ran"])
    limited = sum(
        1 for r in results if r["errors"] and r["errors"][0].get("code") == "rate_limited"
    )
    assert permitted == source_executor._RATE_LIMIT_MAX
    assert limited == total - source_executor._RATE_LIMIT_MAX


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def test_run_writes_audit_entry(monkeypatch):
    """SHOULD-FIX-5 — every source run writes one audit-log entry."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))

    logged: list = []

    class _FakeLogger:
        def log(self, event):
            logged.append(event)

    import pocketpaw.security.audit as audit_mod

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: _FakeLogger())

    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "state.s"}}}
    await source_executor.run_sources(
        pocket_id="audited-pocket",
        user_id="auditor",
        ripple_spec=spec,
        base_url=BASE + "/?token=leak",
        auth_type="bearer",
        auth_header=None,
        token="super-secret-token",
    )
    assert len(logged) == 1
    event = logged[0]
    assert event.actor == "auditor"
    assert event.action == "pocket.sources.run"
    assert event.target == "audited-pocket"
    assert event.status == "success"
    # The query string is stripped from the logged base URL; the token
    # value never appears anywhere in the entry.
    assert "token=leak" not in event.context["base_url"]
    assert "super-secret-token" not in str(event.context)


async def test_rate_limited_run_audits_as_rate_limited(monkeypatch):
    """A rate-limited run still writes an audit entry, status rate-limited."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))

    logged: list = []

    class _FakeLogger:
        def log(self, event):
            logged.append(event)

    import pocketpaw.security.audit as audit_mod

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: _FakeLogger())

    spec = {"sources": {"s": {"method": "GET", "path": "/x", "bind": "x"}}}
    for _ in range(source_executor._RATE_LIMIT_MAX + 1):
        await source_executor.run_sources(
            pocket_id="rl-audit",
            user_id="auditor",
            ripple_spec=spec,
            base_url=BASE,
            auth_type="none",
            auth_header=None,
            token="",
        )
    assert logged[-1].status == "rate-limited"
