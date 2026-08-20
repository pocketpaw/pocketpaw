# tests/cloud/test_paw_bar_frame.py — Paw Bar glass FRAME endpoint + CSP origin
# model (A1).
# Created 2026-07-15: covers GET /paw-bar/frame (the iframe document + the CSP
# frame-ancestors embedder gate) and the CSP/parent-origin helper functions.
# Three layers:
# Updated 2026-08-21 (dashboard preview ancestor): the builder frames a site's real
#   published page, so the bar's iframe sits TWO deep — dashboard → site page → bar —
#   and frame-ancestors is matched against EVERY ancestor. No Site allowlist named the
#   dashboard, so the bar was refused in every preview with
#   "Framing '<backend>' violates ... frame-ancestors". New coverage: the dashboard
#   origin is admitted alongside the allowlist, sourced from PAWBAR_DASHBOARD_ORIGIN or
#   (unset — the shipped state) the declared CORS origins; it is sanitized like any
#   allowlist entry; it never revives an empty allowlist; and with neither source set
#   the header is byte-identical to before. An autouse fixture clears both vars so the
#   exact-header assertions stay hermetic.
# Updated 2026-07-30 (frame-ancestors port fix): the expected header now carries a
#   ``:*`` port on every portless entry. A CSP host-source with no port matches only
#   the scheme's DEFAULT port, so a site served on any other port could not be framed
#   and the bar rendered as an empty grey box. These assertions pinned that. The
#   header-injection guard is unchanged and still covered.
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


@pytest.fixture(autouse=True)
def _no_dashboard_origin(monkeypatch):
    """Every test here states its own dashboard config. Clearing both sources keeps
    the exact-header assertions hermetic on a machine that happens to export one."""
    monkeypatch.delenv("PAWBAR_DASHBOARD_ORIGIN", raising=False)
    monkeypatch.delenv("POCKETPAW_API_CORS_ALLOWED_ORIGINS", raising=False)


# --------------------------------------------------------------------------- #
# Layer 1 — the CSP + parent-origin helpers (pure functions, no I/O)
# --------------------------------------------------------------------------- #


def test_frame_ancestors_emits_exactly_allowed_origins():
    from pocketpaw_ee.paw_bar.router import _frame_ancestors_csp

    assert _frame_ancestors_csp(["brewco.com"]) == "frame-ancestors brewco.com:*"
    assert (
        _frame_ancestors_csp(["brewco.com", "shop.example.com"])
        == "frame-ancestors brewco.com:* shop.example.com:*"
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
    assert csp == "frame-ancestors brewco.com:* ok.example.com:*"
    assert ";" not in csp
    assert "\n" not in csp


def test_ancestor_sources_dedupes_repeats():
    """The dashboard origin is appended to the Site's allowlist, and in local dev it
    IS one of the seeded hosts — so the same source can arrive twice. A repeat is
    harmless to a browser but makes the header a puzzle to read; it collapses to one,
    first occurrence wins, order otherwise preserved."""
    from pocketpaw_ee.paw_bar.router import _ancestor_sources

    assert _ancestor_sources(["localhost", "brewco.com", "localhost", "https://localhost/"]) == [
        "localhost:*",
        "brewco.com:*",
    ]


def test_public_frame_ancestors_appends_configured_dashboard_origin(monkeypatch):
    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "https://app.example.com")
    from pocketpaw_ee.paw_bar.router import _public_frame_ancestors

    assert (
        _public_frame_ancestors(["brewco.com"])
        == "frame-ancestors brewco.com:* https://app.example.com:*"
    )


def test_public_frame_ancestors_still_fails_closed_on_empty_allowlist(monkeypatch):
    """A declared dashboard origin must NOT resurrect a Site that has no embedders.
    Fail-closed is decided on the Site's own allowlist alone; the dashboard entry is
    additive on top of a policy that already had at least one source."""
    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "https://app.example.com")
    from pocketpaw_ee.paw_bar.router import _public_frame_ancestors

    assert _public_frame_ancestors([]) is None
    assert _public_frame_ancestors(["", "   "]) is None


def test_dashboard_ancestor_keeps_an_explicit_scheme():
    """The reported header ALREADY listed ``localhost:*`` and the bar was blocked
    anyway — because a schemeless host-source resolves against the FRAME's scheme, so
    against an https backend it means ``https://localhost``. A dashboard on plain
    http (every local session pointed at a deployed backend) needs the scheme kept,
    or appending it changes nothing."""
    from pocketpaw_ee.paw_bar.router import _sanitize_dashboard_ancestor

    assert _sanitize_dashboard_ancestor("http://localhost:5173") == "http://localhost:5173"
    assert _sanitize_dashboard_ancestor("https://app.example.com") == "https://app.example.com:*"
    # No scheme declared → the allowlist's schemeless form, unchanged.
    assert _sanitize_dashboard_ancestor("app.example.com") == "app.example.com:*"
    # No host-source spelling for a non-browser scheme → dropped, never guessed at.
    assert _sanitize_dashboard_ancestor("tauri://localhost") is None
    # Header injection is refused here exactly as it is for allowed_origins.
    assert _sanitize_dashboard_ancestor("https://evil.example; default-src *") is None
    assert _sanitize_dashboard_ancestor("https://bad\nhost") is None


def test_dashboard_preview_ancestors_prefers_the_explicit_var(monkeypatch):
    from pocketpaw_ee.paw_bar.router import _dashboard_preview_ancestors

    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("POCKETPAW_API_CORS_ALLOWED_ORIGINS", '["https://ignored.example"]')
    assert _dashboard_preview_ancestors() == ["https://app.example.com"]


def test_dashboard_preview_ancestors_reads_cors_origins_in_either_shape(monkeypatch):
    """``api_cors_allowed_origins`` is a pydantic list, so the env form is JSON — but
    operators write CSV (``.env.example`` even documents CSV for a sibling var). Read
    the raw env permissively rather than re-implementing pydantic's parsing: both
    shapes reach the same sanitizer, and anything malformed is dropped there."""
    from pocketpaw_ee.paw_bar.router import _dashboard_preview_ancestors

    monkeypatch.setenv("POCKETPAW_API_CORS_ALLOWED_ORIGINS", '["https://a.example"]')
    assert _dashboard_preview_ancestors() == ["https://a.example"]

    monkeypatch.setenv("POCKETPAW_API_CORS_ALLOWED_ORIGINS", "https://a.example,https://b.example")
    assert _dashboard_preview_ancestors() == ["https://a.example", "https://b.example"]

    monkeypatch.setenv("POCKETPAW_API_CORS_ALLOWED_ORIGINS", "  ")
    assert _dashboard_preview_ancestors() == []


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
    assert (
        res.headers["content-security-policy"] == "frame-ancestors brewco.com:* shop.example.com:*"
    )
    body = res.text
    # The document seeds window.__PAWBAR__ before loading the app.
    assert "window.__PAWBAR__" in body
    assert '"mode": "concierge"' in body
    assert '"widgetId": "pp_seed"' in body
    assert '"siteKey": "site_key_' in body  # world-visible embed key, by design
    assert "/pawbar-app/pawbar.js" in body
    assert "/pawbar-app/pawbar.css" in body


@pytest.mark.asyncio
async def test_frame_admits_the_dashboard_as_an_ancestor(frame_client, monkeypatch):
    """The reported bug. The builder previews a site by framing its real published
    page, so the bar's iframe sits TWO deep — dashboard → site page → bar — and
    frame-ancestors is matched against EVERY ancestor, not just the immediate parent.
    Nothing in the publish path knows the dashboard exists, so no Site allowlist ever
    named it and the bar was refused in every preview."""
    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "https://app.example.com")
    await _site(allowed_origins=["brewco.com"])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})

    assert res.status_code == 200
    assert (
        res.headers["content-security-policy"]
        == "frame-ancestors brewco.com:* https://app.example.com:*"
    )


@pytest.mark.asyncio
async def test_frame_admits_an_http_dashboard_against_an_https_frame(frame_client, monkeypatch):
    """The reported case, end to end: a local dashboard driving a DEPLOYED https
    backend. The Site allowlist already seeds ``localhost``, and the bar was still
    blocked, because that bare source reads as https against an https frame. The
    dashboard entry carries the scheme, so the header now says what it means."""
    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "http://localhost:5173")
    await _site(allowed_origins=["localhost", "127.0.0.1", "site.pawsites.workers.dev"])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})

    assert res.status_code == 200
    assert res.headers["content-security-policy"] == (
        "frame-ancestors localhost:* 127.0.0.1:* site.pawsites.workers.dev:* http://localhost:5173"
    )


@pytest.mark.asyncio
async def test_frame_falls_back_to_the_declared_cors_origins(frame_client, monkeypatch):
    """No deploy we ship sets PAWBAR_DASHBOARD_ORIGIN — which is exactly how this
    broke. The operator has already declared where the frontend lives, as the API's
    CORS origins, so an unset var reads those instead of staying silently broken."""
    monkeypatch.setenv("POCKETPAW_API_CORS_ALLOWED_ORIGINS", '["https://app.example.com"]')
    await _site(allowed_origins=["brewco.com"])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})

    assert (
        res.headers["content-security-policy"]
        == "frame-ancestors brewco.com:* https://app.example.com:*"
    )


@pytest.mark.asyncio
async def test_frame_ancestors_unchanged_when_no_dashboard_is_configured(frame_client):
    """Neither source set (the autouse fixture clears both) → the header is EXACTLY
    the Site's allowlist. In particular the ``localhost:5173`` default that
    ``_dashboard_origin`` applies to the session-authed OWNER preview must not leak
    into the public frame, where it would name every visitor's own machine as a
    permitted embedder of a customer's bar."""
    await _site(allowed_origins=["brewco.com"])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})

    assert res.headers["content-security-policy"] == "frame-ancestors brewco.com:*"


@pytest.mark.asyncio
async def test_frame_sanitizes_the_dashboard_origin(frame_client, monkeypatch):
    """The var is operator-controlled data flowing into a response HEADER, so it goes
    through the SAME sanitizer allowed_origins does: an unusable value is dropped and
    the Site's own policy still renders — never a split header, never a 403."""
    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "https://app.example.com; default-src *")
    await _site(allowed_origins=["brewco.com"])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})

    assert res.status_code == 200
    assert res.headers["content-security-policy"] == "frame-ancestors brewco.com:*"


@pytest.mark.asyncio
async def test_frame_dashboard_origin_does_not_revive_an_empty_allowlist(frame_client, monkeypatch):
    """Fail-closed is decided on the SITE's allowlist alone. A Site with no embedders
    stays unrenderable even when a dashboard origin is declared — the owner preview
    has its own session-authed endpoint for that."""
    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "https://app.example.com")
    await _site(allowed_origins=[])
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})

    assert res.status_code == 403


@pytest.mark.asyncio
async def test_frame_assets_carry_cache_busting_version(frame_client, tmp_path, monkeypatch):
    """The asset URLs must carry ?v=<newest bundle mtime> so a deploy busts every
    embedder's browser cache (StaticFiles sends no Cache-Control; heuristic
    caching pinned the first live demo to a stale bundle)."""
    (tmp_path / "pawbar.js").write_text("// bundle")
    (tmp_path / "pawbar.css").write_text("/* styles */")
    monkeypatch.setenv("PAWBAR_APP_DIR", str(tmp_path))
    expected = max(
        int((tmp_path / "pawbar.js").stat().st_mtime),
        int((tmp_path / "pawbar.css").stat().st_mtime),
    )

    await _site()
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200
    assert f"/pawbar-app/pawbar.js?v={expected}" in res.text
    assert f"/pawbar-app/pawbar.css?v={expected}" in res.text


@pytest.mark.asyncio
async def test_frame_assets_version_zero_when_bundle_missing(frame_client, tmp_path, monkeypatch):
    """No bundle dropped in yet → ?v=0 (assets 404 either way; the frame must not 500)."""
    monkeypatch.setenv("PAWBAR_APP_DIR", str(tmp_path / "empty"))
    await _site()
    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200
    assert "/pawbar-app/pawbar.js?v=0" in res.text


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


class TestDeadFrameShell:
    """A refused frame renders INSIDE a visible iframe on the customer's site —
    the body must be a blank self-removing shell, never an error payload
    (2026-07-30 captain report: literal JSON on the page while disabled)."""

    def test_dead_frame_is_blank_html_403(self) -> None:
        from pocketpaw_ee.paw_bar.router import _dead_frame_response

        res = _dead_frame_response("", ["brewco.com"])
        assert res.status_code == 403
        body = res.body.decode()
        assert body.startswith("<!doctype html>")
        # Never an error payload — no JSON detail shape anywhere in the shell.
        assert '"detail"' not in body and "concierge_disabled" not in body
        assert res.headers["cache-control"] == "no-store"

    def test_dead_frame_posts_dead_to_validated_parent_only(self) -> None:
        from pocketpaw_ee.paw_bar.router import _dead_frame_response

        allowed = _dead_frame_response("https://brewco.com", ["brewco.com"]).body.decode()
        assert "type:'pawbar:dead'" in allowed and "https://brewco.com" in allowed
        # An unlisted parent gets NO script — nothing is posted anywhere.
        stranger = _dead_frame_response("https://evil.example", ["brewco.com"]).body.decode()
        assert "postMessage" not in stranger
