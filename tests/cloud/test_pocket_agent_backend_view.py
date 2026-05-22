# test_pocket_agent_backend_view.py — RFC 04 alpha follow-up 2.
# Created: 2026-05-22 — closes the gap where the pocket EDIT specialist had
#   NO signal that a backend was already configured. The backend credential
#   lives in a separate collection (security design D1), so nothing
#   surfaced it to the agent — it would ask the user for a URL it could not
#   see. This pins that agent_view / get_pocket now carries a non-secret
#   `backend` summary ({base_url, auth_type, configured}), and that the
#   token NEVER reaches agent context.
#
# Updated: 2026-05-22 (RFC 05 M2a) — the backend summary gained an
#   `allowed_writes` key (the per-pocket write allowlist — a non-secret
#   the edit specialist needs to author write actions). Assertions updated
#   to the new four-key shape.
#
# Exercises the real Beanie path against the in-memory mongomock-motor DB
# (mongo_db fixture) with the w1/u1 SSE-stream identity (agent_identity).

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    """set_pocket_backend encrypts the token — it needs AUTH_SECRET."""
    monkeypatch.setenv("AUTH_SECRET", "agent-backend-view-test-secret")


async def _make_pocket(**fields):
    """Insert a fresh Pocket through the normal Beanie path."""
    from pocketpaw_ee.cloud.models.pocket import Pocket

    base = dict(
        workspace="w1",
        name="Test Pocket",
        description="",
        type="custom",
        icon="",
        color="",
        owner="u1",
        visibility="workspace",
        rippleSpec={"version": "1.0", "ui": {"id": "n_root0000", "type": "flex"}},
    )
    base.update(fields)
    doc = Pocket(**base)
    await doc.insert()
    return doc


@pytest.fixture
def agent_identity():
    """Attach the default w1 / u1 SSE-stream identity so agent_view /
    _agent_load_doc pass their workspace + edit-access checks."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


# ---------------------------------------------------------------------------
# agent_view backend summary
# ---------------------------------------------------------------------------


async def test_agent_view_reports_not_configured_when_no_backend(mongo_db, agent_identity):
    """A pocket with no backend credential row gets
    ``backend = {"configured": False}``."""
    from pocketpaw_ee.cloud.pockets.service import agent_view

    doc = await _make_pocket()
    view, err = await agent_view(str(doc.id))

    assert err is None
    assert view is not None
    assert view["backend"] == {"configured": False}


async def test_agent_view_includes_configured_backend_summary(mongo_db, agent_identity):
    """When a backend IS configured, agent_view carries the non-secret
    summary — base_url + auth_type + configured."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    doc = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=str(doc.id),
        base_url="https://jsonplaceholder.typicode.com",
        auth_type="none",
        auth_token="",
    )

    view, err = await pockets_service.agent_view(str(doc.id))

    assert err is None
    assert view is not None
    assert view["backend"] == {
        "base_url": "https://jsonplaceholder.typicode.com",
        "auth_type": "none",
        "configured": True,
        # RFC 05 M2a: the summary carries the write allowlist (empty by
        # default — fail-closed). RFC 05 M2b.1: and the approval route
        # (None by default — the owner approves).
        "allowed_writes": [],
        "approval_route": None,
    }


async def test_agent_view_backend_summary_never_leaks_the_token(mongo_db, agent_identity):
    """The token is encrypted into a separate collection — it must never
    appear anywhere in the agent-facing view, even when auth is bearer."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    doc = await _make_pocket()
    secret = "super-secret-bearer-token-xyz"
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=str(doc.id),
        base_url="https://api.example.com",
        auth_type="bearer",
        auth_token=secret,
    )

    view, err = await pockets_service.agent_view(str(doc.id))

    assert err is None
    assert view is not None
    backend = view["backend"]
    assert backend["configured"] is True
    assert backend["auth_type"] == "bearer"
    # The summary carries only non-secret keys (RFC 05 M2a adds
    # `allowed_writes` — the write allowlist; M2b.1 adds `approval_route`
    # — the gated-write approver routing — both non-secret).
    assert set(backend) == {
        "base_url",
        "auth_type",
        "configured",
        "allowed_writes",
        "approval_route",
    }
    # The token must not appear anywhere in the serialized view.
    import json

    blob = json.dumps(view)
    assert secret not in blob
    assert "encrypted_token" not in blob
    assert "auth_token" not in blob


async def test_agent_view_backend_summary_is_workspace_scoped(mongo_db, agent_identity):
    """A backend row for a different workspace must not bleed into this
    pocket's summary — get_pocket_backend is workspace-scoped."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    doc = await _make_pocket()
    # A backend row keyed to the same pocket id but a different workspace.
    await pockets_service.set_pocket_backend(
        workspace_id="w2",
        user_id="u2",
        pocket_id=str(doc.id),
        base_url="https://other-workspace.example.com",
        auth_type="none",
        auth_token="",
    )

    view, err = await pockets_service.agent_view(str(doc.id))

    assert err is None
    assert view is not None
    # The pocket is in w1; the w2 backend row must not surface.
    assert view["backend"] == {"configured": False}


async def test_fetch_pocket_for_agent_carries_backend_summary(mongo_db, agent_identity):
    """The agent_context wrapper (what get_pocket returns to the tool)
    passes the backend summary straight through."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.pockets.agent_context import fetch_pocket_for_agent

    doc = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=str(doc.id),
        base_url="https://jsonplaceholder.typicode.com",
        auth_type="none",
        auth_token="",
    )

    result = await fetch_pocket_for_agent(str(doc.id))

    assert result["ok"] is True
    assert result["pocket"]["backend"] == {
        "base_url": "https://jsonplaceholder.typicode.com",
        "auth_type": "none",
        "configured": True,
        # RFC 05 M2a: the summary carries the write allowlist; M2b.1
        # adds the approval route (None — the owner approves).
        "allowed_writes": [],
        "approval_route": None,
    }


# ---------------------------------------------------------------------------
# fill_current_pocket — prompt token substitution
# ---------------------------------------------------------------------------


def test_fill_current_pocket_renders_configured_backend():
    from pocketpaw.ripple import fill_current_pocket
    from pocketpaw.ripple._pockets import _CURRENT_POCKET_BLOCK_TEMPLATE

    out = fill_current_pocket(
        _CURRENT_POCKET_BLOCK_TEMPLATE,
        "pkt-123",
        {"base_url": "https://api.example.com", "auth_type": "bearer", "configured": True},
    )
    assert "__POCKET_ID__" not in out
    assert "__BACKEND_SUMMARY__" not in out
    assert "pkt-123" in out
    assert "configured — https://api.example.com (auth: bearer)" in out


def test_fill_current_pocket_renders_not_configured():
    from pocketpaw.ripple import fill_current_pocket
    from pocketpaw.ripple._pockets import _CURRENT_POCKET_BLOCK_TEMPLATE

    out = fill_current_pocket(_CURRENT_POCKET_BLOCK_TEMPLATE, "pkt-123", {"configured": False})
    assert "__BACKEND_SUMMARY__" not in out
    assert "Backend: not configured" in out


def test_fill_current_pocket_renders_unknown_when_summary_is_none():
    """Callers that cannot await the backend read pass None — the line
    must say 'unknown', never imply there is no backend."""
    from pocketpaw.ripple import fill_current_pocket
    from pocketpaw.ripple._pockets import _CURRENT_POCKET_BLOCK_TEMPLATE

    out = fill_current_pocket(_CURRENT_POCKET_BLOCK_TEMPLATE, "pkt-123", None)
    assert "__BACKEND_SUMMARY__" not in out
    assert "configured state unknown" in out
    assert "not configured" not in out


def test_edit_specialist_prompt_has_no_unfilled_backend_token():
    """The raw edit-specialist prompt carries the token; after
    fill_current_pocket no literal token may remain."""
    from pocketpaw.ripple import POCKET_EDIT_SPECIALIST_PROMPT_MCP, fill_current_pocket

    assert "__BACKEND_SUMMARY__" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
    filled = fill_current_pocket(POCKET_EDIT_SPECIALIST_PROMPT_MCP, "pkt-1", None)
    assert "__BACKEND_SUMMARY__" not in filled
    assert "__POCKET_ID__" not in filled
