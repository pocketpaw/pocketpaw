# tests/cloud/test_pocket_action_executor.py — RFC 05 M2a.
# Created: 2026-05-22 — Coverage for the pocket WRITE-action executor, the
# write half of the pocket data layer. No real network calls — outbound
# HTTP is faked via httpx.MockTransport and socket.getaddrinfo is
# monkeypatched so fake hostnames "resolve" to a public IP.
#
# What this pins:
#   - ActionBinding parses and IGNORES M2b governance fields
#     (requires_instinct / instinct_policy / outcome).
#   - INSTINCT-REJECT (fail-closed): a truthy raw `requires_instinct`
#     yields code `instinct_required` and makes NO call.
#   - The allowlist matrix: method mismatch, path mismatch, query string
#     stripped before match, a literal `*` in a hallucinated path, the
#     happy match.
#   - SSRF guards on the WRITE path: `..` traversal, absolute-URL path,
#     a host that resolves internal, a 3xx redirect, an oversize body.
#   - The write rate limit is a SEPARATE counter from the read executor's
#     (a read budget never drains the write budget and vice versa).
#   - The Idempotency-Key header is present; a client-supplied key wins.
#   - The happy path returns the backend's parsed JSON + on_success.

from __future__ import annotations

import json

import httpx
import pytest
from pocketpaw_ee.cloud.pockets import action_executor, source_executor
from pocketpaw_ee.cloud.pockets.action_executor import ActionBinding, run_action

BASE = "https://api.example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear both module-level rate-limit logs between tests."""
    action_executor._action_log.clear()
    source_executor._run_log.clear()
    yield
    action_executor._action_log.clear()
    source_executor._run_log.clear()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Make every hostname resolve to a public IP so the DNS guard passes.

    Tests that need an internal-resolving host override this.
    """

    def _fake_getaddrinfo(host, *_args, **_kwargs):
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

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _factory)


def _write_action(method: str = "POST", path: str = "/leases/42/renew") -> dict:
    """A minimal, well-formed raw action dict."""
    return {"kind": "write_binding", "method": method, "path": path, "params": {}}


def _allow(method: str = "POST", pattern: str = "/leases/*/renew") -> list[dict]:
    """A one-rule write allowlist."""
    return [{"method": method, "path_pattern": pattern}]


# ---------------------------------------------------------------------------
# ActionBinding parsing
# ---------------------------------------------------------------------------


def test_action_binding_parses_minimal():
    binding = ActionBinding.model_validate(_write_action())
    assert binding.kind == "write_binding"
    assert binding.method == "POST"
    assert binding.path == "/leases/42/renew"
    assert binding.params == {}
    assert binding.confirm is False
    assert binding.on_success == []
    assert binding.on_error == []


def test_action_binding_ignores_m2b_governance_fields():
    """`requires_instinct` / `instinct_policy` / `outcome` are M2b fields —
    ActionBinding ignores them on parse (extra: ignore) so an M2b-authored
    spec stays parseable by the M2a runtime."""
    raw = {
        **_write_action(),
        "requires_instinct": True,
        "instinct_policy": "approve_per_row",
        "outcome": "renewal_completed",
    }
    binding = ActionBinding.model_validate(raw)
    # The governance fields are dropped, not declared on the model.
    assert not hasattr(binding, "requires_instinct")
    assert "requires_instinct" not in binding.model_dump()
    assert "outcome" not in binding.model_dump()


def test_action_binding_rejects_non_write_verb():
    with pytest.raises(Exception):
        ActionBinding.model_validate({"method": "GET", "path": "/x"})


# ---------------------------------------------------------------------------
# Instinct-reject (fail-closed)
# ---------------------------------------------------------------------------


async def test_instinct_required_rejects_before_any_call(monkeypatch):
    """A truthy raw `requires_instinct` is rejected with code
    `instinct_required` and NO HTTP call is made."""
    called = {"hit": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)

    raw = {**_write_action(), "requires_instinct": True}
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="mark_renewed",
        raw_action=raw,
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is False
    assert result["code"] == "instinct_required"
    assert called["hit"] is False


async def test_instinct_policy_alone_does_not_reject(monkeypatch):
    """`instinct_policy` without a truthy `requires_instinct` does NOT
    trigger the fail-closed gate — only `requires_instinct` does."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    raw = {**_write_action(), "instinct_policy": "approve_per_row"}
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="mark_renewed",
        raw_action=raw,
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Allowlist matrix
# ---------------------------------------------------------------------------


async def test_allowlist_happy_match(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"renewed": True}))
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="mark_renewed",
        raw_action=_write_action("POST", "/leases/42/renew"),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/leases/*/renew"),
    )
    assert result["ok"] is True
    assert result["response"] == {"renewed": True}


async def test_allowlist_method_mismatch_rejected(monkeypatch):
    """A DELETE action against a POST-only allowlist is rejected — no call."""
    called = {"hit": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="del",
        raw_action=_write_action("DELETE", "/leases/42/renew"),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/leases/*/renew"),
    )
    assert result["ok"] is False
    assert result["code"] == "not_allowed"
    assert called["hit"] is False


async def test_allowlist_path_mismatch_rejected(monkeypatch):
    """A path that no pattern covers is rejected — no call."""
    called = {"hit": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="del",
        raw_action=_write_action("POST", "/users/9/delete"),
        path="/users/9/delete",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/leases/*/renew"),
    )
    assert result["ok"] is False
    assert result["code"] == "not_allowed"
    assert called["hit"] is False


async def test_allowlist_strips_query_before_match(monkeypatch):
    """A `?x=y` on the resolved path must not defeat a `/leases/*` pattern —
    the query is stripped before the allowlist check."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="patch",
        raw_action=_write_action("PATCH", "/leases/42?notify=true"),
        path="/leases/42?notify=true",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("PATCH", "/leases/*"),
    )
    assert result["ok"] is True


async def test_allowlist_empty_rejects_everything(monkeypatch):
    """Fail-closed: an empty allowlist matches nothing — no write fires."""
    called = {"hit": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="mark_renewed",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=[],
    )
    assert result["ok"] is False
    assert result["code"] == "not_allowed"
    assert called["hit"] is False


async def test_allowlist_literal_star_in_path_does_not_match_concrete_pattern(monkeypatch):
    """A hallucinated path that literally contains `*` must NOT match a
    concrete (glob-free) allowlist pattern. fnmatchcase treats the pattern
    as the glob and the path as a literal — a literal `*` in the path is
    just a character that the concrete pattern cannot match."""
    called = {"hit": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json={})

    _mock_client_patch(monkeypatch, handler)
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="weird",
        raw_action=_write_action("POST", "/leases/*/renew"),
        path="/leases/*/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        # Concrete pattern, no glob — only matches the exact string.
        allowed_writes=_allow("POST", "/leases/42/renew"),
    )
    assert result["ok"] is False
    assert result["code"] == "not_allowed"
    assert called["hit"] is False


# ---------------------------------------------------------------------------
# SSRF guards on the write path
# ---------------------------------------------------------------------------


async def test_dotdot_traversal_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action("POST", "/a/../../etc/passwd"),
        path="/a/../../etc/passwd",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/*"),
    )
    assert result["ok"] is False
    assert result["code"] == "bad_path"


async def test_absolute_url_path_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action("POST", "https://evil.com/x"),
        path="https://evil.com/x",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/*"),
    )
    assert result["ok"] is False
    assert result["code"] == "bad_path"


async def test_internal_base_url_rejected(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action("POST", "/x"),
        path="/x",
        params={},
        base_url="http://127.0.0.1",
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/x"),
    )
    assert result["ok"] is False
    assert result["code"] == "bad_base_url"


async def test_host_resolving_internal_rejected(monkeypatch):
    """DNS rebinding guard — a public name resolving to a private IP."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))

    def _internal_getaddrinfo(host, *_a, **_k):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _internal_getaddrinfo)
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action("POST", "/x"),
        path="/x",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/x"),
    )
    assert result["ok"] is False
    assert result["code"] == "bad_host"


async def test_redirect_is_an_error(monkeypatch):
    """A 3xx is treated as an error — redirects are never followed."""
    _mock_client_patch(
        monkeypatch,
        lambda r: httpx.Response(302, headers={"location": "https://evil.com/x"}),
    )
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action("POST", "/leases/42/renew"),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/leases/*/renew"),
    )
    assert result["ok"] is False
    assert result["code"] == "redirect"


async def test_oversize_response_rejected(monkeypatch):
    big = json.dumps([{"x": "a" * 1000}] * 600)  # > 512 KB
    assert len(big.encode()) > action_executor._MAX_RESPONSE_BYTES

    _mock_client_patch(
        monkeypatch,
        lambda r: httpx.Response(200, content=big, headers={"content-type": "application/json"}),
    )
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action("POST", "/leases/42/renew"),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/leases/*/renew"),
    )
    assert result["ok"] is False
    assert result["code"] == "too_large"


# ---------------------------------------------------------------------------
# Rate limit — write counter isolated from the read counter
# ---------------------------------------------------------------------------


async def test_write_rate_limit_breach(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))

    async def _one():
        return await run_action(
            pocket_id="p-rl",
            user_id="u1",
            action="a",
            raw_action=_write_action(),
            path="/leases/42/renew",
            params={},
            base_url=BASE,
            auth_type="none",
            auth_header=None,
            token="",
            allowed_writes=_allow(),
        )

    for _ in range(action_executor._ACTION_RATE_LIMIT_MAX):
        assert (await _one())["ok"] is True
    breach = await _one()
    assert breach["ok"] is False
    assert breach["code"] == "rate_limited"


async def test_write_rate_limit_isolated_from_read_budget(monkeypatch):
    """Burning the READ budget for (pocket, user) must not drain the WRITE
    budget for the same key, and vice versa — the counters are separate
    dicts."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))

    # Drain the WRITE budget fully.
    for _ in range(action_executor._ACTION_RATE_LIMIT_MAX):
        await run_action(
            pocket_id="shared",
            user_id="u1",
            action="a",
            raw_action=_write_action(),
            path="/leases/42/renew",
            params={},
            base_url=BASE,
            auth_type="none",
            auth_header=None,
            token="",
            allowed_writes=_allow(),
        )
    breach = await run_action(
        pocket_id="shared",
        user_id="u1",
        action="a",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert breach["code"] == "rate_limited"

    # The READ budget for the SAME (pocket, user) is untouched.
    assert source_executor._run_log.get(("shared", "u1")) in (None, [])
    assert not await source_executor._rate_limited("shared", "u1")


async def test_read_budget_does_not_drain_write_budget(monkeypatch):
    """The reverse: exhausting the read counter leaves the write counter
    with its full budget."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))

    # Drain the READ counter for (pocket, user) directly.
    for _ in range(source_executor._RATE_LIMIT_MAX):
        await source_executor._rate_limited("shared2", "u1")
    assert await source_executor._rate_limited("shared2", "u1")

    # A write for the same key still goes through — separate counter.
    result = await run_action(
        pocket_id="shared2",
        user_id="u1",
        action="a",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Idempotency-Key header
# ---------------------------------------------------------------------------


async def test_idempotency_key_generated_when_omitted(monkeypatch):
    """When the client omits a key the executor sends a generated one."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("idempotency-key")
        return httpx.Response(200, json={"ok": 1})

    _mock_client_patch(monkeypatch, handler)
    await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert seen["key"]
    assert len(seen["key"]) >= 16


async def test_client_supplied_idempotency_key_is_honored(monkeypatch):
    """A client-supplied key is sent verbatim — a write retried after a
    timeout carries the SAME key so the backend can dedupe it."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("idempotency-key")
        return httpx.Response(200, json={"ok": 1})

    _mock_client_patch(monkeypatch, handler)
    await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
        idempotency_key="client-key-123",
    )
    assert seen["key"] == "client-key-123"


# ---------------------------------------------------------------------------
# Happy path — body + on_success
# ---------------------------------------------------------------------------


async def test_happy_path_sends_params_and_carries_on_success(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(201, json={"id": 42, "status": "renewed"})

    _mock_client_patch(monkeypatch, handler)
    raw = {
        **_write_action("POST", "/leases/42/renew"),
        "params": {"proposed_rent": 2000},
        "on_success": [{"action": "run_source", "source": "leases"}],
        "on_error": [{"action": "toast", "variant": "error"}],
    }
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="mark_renewed",
        raw_action=raw,
        path="/leases/42/renew",
        params={"proposed_rent": 2000},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/leases/*/renew"),
    )
    assert result["ok"] is True
    assert result["status"] == 201
    assert result["response"] == {"id": 42, "status": "renewed"}
    assert result["on_success"] == [{"action": "run_source", "source": "leases"}]
    assert result["on_error"] == [{"action": "toast", "variant": "error"}]
    assert seen["method"] == "POST"
    assert seen["body"] == {"proposed_rent": 2000}


async def test_backend_4xx_becomes_http_error(monkeypatch):
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(422, json={"err": "bad"}))
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is False
    assert result["code"] == "http_error"


async def test_malformed_binding_is_bad_binding(monkeypatch):
    """A raw action missing `method` is a `bad_binding` rejection."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    result = await run_action(
        pocket_id="p1",
        user_id="u1",
        action="a",
        raw_action={"kind": "write_binding", "path": "/x"},  # no method
        path="/x",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow("POST", "/x"),
    )
    assert result["ok"] is False
    assert result["code"] == "bad_binding"
