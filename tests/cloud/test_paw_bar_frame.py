# tests/cloud/test_paw_bar_frame.py — Paw Bar glass FRAME endpoint + CSP origin
# model (A1).
# Created 2026-07-15: covers GET /paw-bar/frame (the iframe document + the CSP
# frame-ancestors embedder gate) and the CSP/parent-origin helper functions.
# Three layers:
#   * Pure-function proofs (no I/O): the frame-ancestors builder emits EXACTLY the
#     Site's allowed_origins, fails closed (None) on an empty/unusable allowlist,
#     sanitizes header-injection attempts, and the parent-origin validator only
#     echoes an allowlisted origin.
#   * Endpoint (httpx): valid key → 200 + CSP frame-ancestors header + a body that
#     seeds window.__PAWBAR__; empty allowed_origins → 403 (fail-closed refuse); a
#     blank / unknown / revoked key → 401; NO X-Frame-Options; and the world-visible
#     key + attacker-influenceable params can't break out of the inline <script>.
#   * Dual-mode chat: an iframe-mode request (Origin == our frame origin) passes the
#     origin gate that would reject it inline; a non-frame disallowed origin is still
#     403; the frozen inline path (Origin in allowed_origins) still works.

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarBlock, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

_VALID_KEY = "site_key_" + "a" * 24
_FRAME_ORIGIN = "https://frame.pocketpaw.test"


# --------------------------------------------------------------------------- #
# Layer 1 — the CSP + parent-origin helpers (pure functions, no I/O)
# --------------------------------------------------------------------------- #


def test_frame_ancestors_emits_exactly_allowed_origins():
    from pocketpaw_ee.paw_bar.router import _frame_ancestors_csp

    assert _frame_ancestors_csp(["brewco.com"]) == "frame-ancestors brewco.com"
    assert (
        _frame_ancestors_csp(["brewco.com", "shop.example.com"])
        == "frame-ancestors brewco.com shop.example.com"
    )


def test_frame_ancestors_fails_closed_on_empty():
    """An empty (or all-unusable) allowlist yields None — the endpoint refuses to
    render rather than emit a source-less policy. Mirrors origin_allowed, NOT the
    _origin_allowed empty=allow-all footgun."""
    from pocketpaw_ee.paw_bar.router import _frame_ancestors_csp

    assert _frame_ancestors_csp([]) is None
    assert _frame_ancestors_csp(["", "   "]) is None


def test_frame_ancestors_sanitizes_header_injection():
    """allowed_origins is owner-controlled and flows into a response header — a
    value carrying a space / ``;`` / newline must be dropped, never allowed to
    inject a second CSP directive or split the header."""
    from pocketpaw_ee.paw_bar.router import _frame_ancestors_csp

    csp = _frame_ancestors_csp(
        ["brewco.com", "evil.com; default-src *", "bad\nhost", "https://ok.example.com/path"]
    )
    # The two malformed entries are dropped; the scheme+path entry is reduced to host.
    assert csp == "frame-ancestors brewco.com ok.example.com"
    assert ";" not in csp
    assert "\n" not in csp


def test_safe_parent_origin_only_echoes_allowlisted():
    from pocketpaw_ee.paw_bar.router import _safe_parent_origin

    allowed = ["brewco.com"]
    # Allowlisted parent → echoed as a clean scheme://host origin.
    assert _safe_parent_origin("https://brewco.com", allowed) == "https://brewco.com"
    # Not on the allowlist → dropped.
    assert _safe_parent_origin("https://evil.example", allowed) == ""
    # Junk / empty → dropped.
    assert _safe_parent_origin("", allowed) == ""
    assert _safe_parent_origin("not an origin", allowed) == ""
    assert _safe_parent_origin("javascript:alert(1)", allowed) == ""


# --------------------------------------------------------------------------- #
# Layer 2 — the endpoint (httpx)
# --------------------------------------------------------------------------- #


async def _site(**ov):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        script_name="",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


@pytest_asyncio.fixture
async def frame_client(mongo_db):
    """A public app client for GET /paw-bar/frame. The frame endpoint reads only the
    Beanie Site (no paw_bar store), so no store patch is needed."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.mark.asyncio
async def test_frame_valid_key_renders_with_csp(frame_client):
    await _site(allowed_origins=["brewco.com", "shop.example.com"])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY, "w": "pp_seed"})

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    # The embedder gate: frame-ancestors with EXACTLY the Site's allowed_origins.
    assert res.headers["content-security-policy"] == "frame-ancestors brewco.com shop.example.com"
    body = res.text
    # The document seeds window.__PAWBAR__ before loading the app.
    assert "window.__PAWBAR__" in body
    assert '"mode": "concierge"' in body
    assert '"widgetId": "pp_seed"' in body
    assert '"siteKey": "site_key_' in body  # world-visible embed key, by design
    assert "/pawbar-app/pawbar.js" in body
    assert "/pawbar-app/pawbar.css" in body


@pytest.mark.asyncio
async def test_frame_sends_no_x_frame_options(frame_client):
    """XFO is obsolete beside frame-ancestors; a conflicting XFO:DENY would block
    the frame from ever rendering, so we must NOT send it."""
    await _site()
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200
    assert "x-frame-options" not in {k.lower() for k in res.headers}


@pytest.mark.asyncio
async def test_frame_empty_allowed_origins_is_403(frame_client):
    """Fail closed: a Site with no allowlist refuses to render (no CSP to gate the
    embedder means anyone could frame it)."""
    await _site(allowed_origins=[])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_frame_blank_key_is_401(frame_client):
    await _site()
    # Missing/blank key resolves as a too-short key → 401 (never a 422).
    res = await frame_client.get("/paw-bar/frame")
    assert res.status_code == 401
    res2 = await frame_client.get("/paw-bar/frame", params={"key": "short"})
    assert res2.status_code == 401


@pytest.mark.asyncio
async def test_frame_unknown_key_is_401(frame_client):
    await _site()
    res = await frame_client.get("/paw-bar/frame", params={"key": "site_key_" + "z" * 24})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_frame_revoked_key_is_401(frame_client):
    await _site(revoked=True)
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_frame_parent_origin_validated(frame_client):
    await _site(allowed_origins=["brewco.com"])
    # An allowlisted parent origin is echoed into the bootstrap config.
    ok = await frame_client.get(
        "/paw-bar/frame", params={"key": _VALID_KEY, "po": "https://brewco.com"}
    )
    assert '"parentOrigin": "https://brewco.com"' in ok.text
    # A non-allowlisted parent origin is dropped (empty), never trusted verbatim.
    bad = await frame_client.get(
        "/paw-bar/frame", params={"key": _VALID_KEY, "po": "https://evil.example"}
    )
    assert '"parentOrigin": ""' in bad.text


@pytest.mark.asyncio
async def test_frame_escapes_script_breakout(frame_client):
    """The attacker-influenceable widget id / parent-origin params cannot break out
    of the inline <script> — a literal </script> sequence must be escaped."""
    await _site()
    res = await frame_client.get(
        "/paw-bar/frame",
        params={"key": _VALID_KEY, "w": "</script><script>alert(1)</script>"},
    )
    assert res.status_code == 200
    # No raw closing tag survived inside the config; < was escaped to <.
    assert "</script><script>alert(1)" not in res.text
    assert "\\u003c/script" in res.text


# --------------------------------------------------------------------------- #
# Layer 3 — dual-mode chat origin gate
# --------------------------------------------------------------------------- #


def _spec(pocket_id="pocket-1") -> PawBarSpec:
    return PawBarSpec(
        widget_id="pp_seed",
        pocket_id=pocket_id,
        blocks=[PawBarBlock(type="text", content="Hi")],
    )


def _widget(**ov) -> PawBarWidget:
    d = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=_spec(),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
        rate_limit_per_min=60,
        per_customer_limit_per_min=10,
    )
    d.update(ov)
    return PawBarWidget(**d)


class _FakeExecutor:
    def __init__(self, transport) -> None:
        self.transport = transport
        self.submitted: list = []

    async def submit(self, spec) -> None:
        self.submitted.append(spec)
        await self.transport.append_event(
            spec.run_id, "chunk", {"content": "We open at 8am!", "type": "text"}
        )
        await self.transport.append_event(
            spec.run_id, "stream_end", {"assistant_message_id": "m1", "cancelled": False}
        )


@pytest_asyncio.fixture
async def chat_client(tmp_path, mongo_db):
    from unittest.mock import patch

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    store = PawBarStore(tmp_path / "concierge.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, store


def _payload(widget_id: str, **ov) -> dict:
    p = dict(
        widget_id=widget_id,
        signed_key=_VALID_KEY,
        customer_ref="cust-1",
        message="What time do you open?",
    )
    p.update(ov)
    return p


def _stub_run(monkeypatch):
    from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

    transport = InMemoryStreamTransport()
    fake_exec = _FakeExecutor(transport)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: fake_exec)

    async def _fake_create_run(spec):
        return SimpleNamespace(run_id=spec.run_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)
    return fake_exec


@pytest.mark.asyncio
async def test_chat_frame_origin_passes_gate(chat_client, monkeypatch):
    """Iframe mode: the chat request carries OUR frame origin (not the embedder's
    domain), which is NOT in Site.allowed_origins — but because the embedder was
    already gated by the frame CSP at render, the frame origin is accepted."""
    client, store = chat_client
    await _site()  # allowed_origins=["brewco.com"] — does NOT include the frame origin
    widget = await store.create_widget(_widget(agent_id="agent-xyz"))
    monkeypatch.setenv("PAWBAR_FRAME_ORIGIN", _FRAME_ORIGIN)
    fake_exec = _stub_run(monkeypatch)

    res = await client.post(
        "/paw-bar/chat", json=_payload(widget.id), headers={"Origin": _FRAME_ORIGIN}
    )
    assert res.status_code == 200
    assert len(fake_exec.submitted) == 1  # reached dispatch


@pytest.mark.asyncio
async def test_chat_non_frame_disallowed_origin_still_403(chat_client, monkeypatch):
    """Frame mode does not open a hole: a request from an origin that is neither our
    frame origin NOR an allowlisted embedder is still refused (fail-closed)."""
    client, store = chat_client
    await _site()
    widget = await store.create_widget(_widget(agent_id="agent-xyz"))
    monkeypatch.setenv("PAWBAR_FRAME_ORIGIN", _FRAME_ORIGIN)

    res = await client.post(
        "/paw-bar/chat", json=_payload(widget.id), headers={"Origin": "https://evil.example"}
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_chat_frozen_inline_origin_still_works(chat_client, monkeypatch):
    """The frozen inline widget path is preserved: a request whose Origin host is on
    Site.allowed_origins passes the (now Site-converged) gate even with the frame
    origin configured."""
    client, store = chat_client
    await _site()
    widget = await store.create_widget(_widget(agent_id="agent-xyz"))
    monkeypatch.setenv("PAWBAR_FRAME_ORIGIN", _FRAME_ORIGIN)
    fake_exec = _stub_run(monkeypatch)

    res = await client.post(
        "/paw-bar/chat", json=_payload(widget.id), headers={"Origin": "https://brewco.com"}
    )
    assert res.status_code == 200
    assert len(fake_exec.submitted) == 1
