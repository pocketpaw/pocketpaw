# tests/cloud/test_pocket_widget_spec_gate.py
# Created: 2026-05-24 (#1208) — service-level coverage for the catalog +
# action-wiring gates that now run on ``widgets[].spec`` in
# ``agent_add_widget`` and ``agent_update_widget``.
#
# Before #1208 these chat-driven write paths bypassed the same gates the
# pocket-level surface (``agent_replace_node`` and friends) gets, so an
# agent-authored widget could land in Mongo with a hallucinated
# dispatcher verb (``action: "fetch"``) or an unwired "Refresh" button —
# the failure modes #1194/#1196 covered at the pocket level.
#
# What's covered:
#   - ``agent_add_widget`` rejects a widget whose ``spec.props.on_click``
#     uses an invented verb; nothing is persisted.
#   - ``agent_update_widget`` rejects the same payload on the update path.
#   - ``agent_add_widget`` rejects a live-labelled "Refresh" button with
#     an empty ``on_click`` — the #1196 class applied to a widget spec.
#   - ``agent_add_widget`` accepts a canonical ``run_source`` button
#     (positive control — sanity check the gate isn't over-eager).
#   - Manifest-unavailable best-effort: when the catalog allow-list can't
#     be fetched, the gate is a no-op and the widget lands as before.
#   - Native widgets (``type="native"``) skip the gate even with a stale
#     spec — they carry no rippleSpec by contract.

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.models.pocket import Widget as _WidgetDoc  # noqa: E402
from pocketpaw_ee.cloud.pockets import service as pockets_service  # noqa: E402

# ---------------------------------------------------------------------------
# Test doc + patch helpers
# ---------------------------------------------------------------------------


class _FakeHomePocketDoc:
    """Stand-in for a ``_PocketDoc`` whose widgets array starts empty.

    Mirrors the surface ``agent_add_widget`` and ``agent_update_widget``
    read: ``id`` / ``workspace`` / ``owner`` / a ``widgets`` list of
    ``_WidgetDoc`` sub-models. ``save()`` is an async no-op that bumps a
    counter so the tests can assert "nothing persisted" by checking that
    ``saves == 0`` after a rejection.
    """

    def __init__(
        self,
        pocket_id: str,
        widgets: list[_WidgetDoc] | None = None,
    ) -> None:
        self.id = pocket_id
        self.workspace = "w-home"
        self.name = "Home"
        self.description = ""
        self.type = "home"
        self.icon = ""
        self.color = ""
        self.owner = "u-home"
        self.visibility = "private"
        self.team: list[str] = []
        self.agents: list[str] = []
        self.widgets: list[_WidgetDoc] = list(widgets or [])
        self.rippleSpec: dict[str, Any] | None = None
        self.share_link_token: str | None = None
        self.share_link_access = "view"
        self.shared_with: list[str] = []
        self.tool_specs: list[Any] = []
        self.saves = 0

    async def save(self) -> None:
        self.saves += 1

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "_id": self.id,
            "workspace": self.workspace,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "icon": self.icon,
            "color": self.color,
            "owner": self.owner,
            "visibility": self.visibility,
            "team": list(self.team),
            "agents": list(self.agents),
            "widgets": [w.model_dump(by_alias=True) for w in self.widgets],
            "rippleSpec": self.rippleSpec,
            "share_link_token": self.share_link_token,
            "share_link_access": self.share_link_access,
            "shared_with": list(self.shared_with),
            "tool_specs": list(self.tool_specs),
        }


def _patch_seams(
    doc: _FakeHomePocketDoc,
    allowed_types: list[str] | None = None,
) -> ExitStack:
    """Patch the seams ``agent_add_widget`` / ``agent_update_widget`` touch:
    doc fetch, emit, payload builder, per-stream identity ContextVars, and
    the manifest allow-list. ``allowed_types`` becomes the manifest stub —
    pass ``None`` to simulate a manifest outage (gate becomes a no-op).
    """
    stack = ExitStack()
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._PocketDoc.get",
            new=AsyncMock(return_value=doc),
        ),
    )
    stack.enter_context(
        patch("pocketpaw_ee.cloud.pockets.service.emit", new=AsyncMock()),
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._pocket_event_payload",
            new=AsyncMock(return_value={"pocket_id": doc.id}),
        ),
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=doc.workspace),
        ),
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value=doc.owner),
        ),
    )

    # Stub the catalog allow-list so the gate has a deterministic answer
    # without hitting the network. ``None`` means "manifest unavailable" —
    # the gate skips, mirroring the production best-effort posture.
    async def _allowed() -> list[str] | None:
        return allowed_types

    stack.enter_context(
        patch.object(pockets_service, "_catalog_allowed_types", _allowed),
    )
    return stack


# Default allow-list that lets a button widget through the catalog walk.
# The action-wiring gate is what we actually want to fire in the negative
# tests — the catalog walk is just here so we don't trip on the type name.
_BUTTON_OK_TYPES = ["button", "chart", "flex", "stat", "heading"]


# ---------------------------------------------------------------------------
# agent_add_widget — verb hallucination is rejected, nothing persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_add_widget_rejects_hallucinated_action_verb() -> None:
    """A button widget whose ``on_click`` uses an invented dispatcher verb
    (``action: "fetch"``) must be rejected before the doc is saved.

    Verb hallucination is the #1194 failure mode applied to a widget
    spec — the renderer's event dispatcher silently no-ops unknown
    verbs, so a button that looks live in the canvas does nothing at
    runtime. The gate's corrective error names the offending verb so
    the agent can retry with a real one (``run_source``).
    """
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430111")
    widget = {
        "name": "Refresh sales",
        "type": "button",
        "spec": {
            "type": "button",
            "props": {
                "label": "Refresh",
                "on_click": {"action": "fetch", "source": "sales"},
            },
        },
    }
    with _patch_seams(doc, allowed_types=_BUTTON_OK_TYPES):
        view, err = await pockets_service.agent_add_widget(doc.id, widget)

    assert view is None
    assert err is not None
    # Error names the bad verb so the specialist sees exactly what to fix.
    assert "fetch" in err
    # Nothing landed in the widget list and the doc was never saved.
    assert doc.widgets == []
    assert doc.saves == 0


# ---------------------------------------------------------------------------
# agent_update_widget — same gate on the update path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_update_widget_rejects_hallucinated_action_verb() -> None:
    """An update that overwrites ``spec`` with a hallucinated verb is
    rejected the same way ``add_widget`` rejects it on creation.

    The pre-existing widget's spec is preserved on rejection — the
    rejected payload never reaches the in-place setattr loop.
    """
    original_spec = {
        "type": "button",
        "props": {
            "label": "Refresh",
            "on_click": {"action": "run_source", "source": "sales"},
        },
    }
    widget = _WidgetDoc(
        name="Refresh sales",
        type="button",
        icon="refresh-cw",
        color="#0A84FF",
        spec=original_spec,
    )
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430222", widgets=[widget])

    bad_spec = {
        "type": "button",
        "props": {
            "label": "Refresh",
            "on_click": {"action": "backend_fetch", "source": "sales"},
        },
    }
    with _patch_seams(doc, allowed_types=_BUTTON_OK_TYPES):
        view, err = await pockets_service.agent_update_widget(
            doc.id,
            widget.id,
            {"spec": bad_spec},
        )

    assert view is None
    assert err is not None
    assert "backend_fetch" in err
    # The existing widget's spec was NOT overwritten by the rejected payload.
    assert doc.widgets[0].spec == original_spec
    assert doc.saves == 0


# ---------------------------------------------------------------------------
# agent_add_widget — #1196 sibling: live-labelled, unwired Refresh button
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_add_widget_rejects_empty_on_click_on_refresh_button() -> None:
    """A button labelled "Refresh" whose ``on_click`` is missing entirely
    is the #1196 failure mode applied at the widget-spec layer.

    A live label promises live behaviour; the renderer has no handler to
    invoke, so the button is decorative. The gate must reject this with
    a message that names the unwired handler so the agent re-emits with
    a real source/api binding.
    """
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430333")
    widget = {
        "name": "Refresh sales",
        "type": "button",
        "spec": {
            "type": "button",
            "props": {
                "label": "Refresh",
                # No on_click at all — looks live, does nothing.
            },
        },
    }
    with _patch_seams(doc, allowed_types=_BUTTON_OK_TYPES):
        view, err = await pockets_service.agent_add_widget(doc.id, widget)

    assert view is None
    assert err is not None
    # The unwired-live-button gate uses "Refresh" as the label in its
    # violation payload and "on_click" / "live" in the reason string.
    assert "Refresh" in err or "on_click" in err
    assert doc.widgets == []
    assert doc.saves == 0


# ---------------------------------------------------------------------------
# agent_add_widget — positive control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_add_widget_accepts_canonical_run_source_button() -> None:
    """A button wired to ``run_source`` against a declared source key is
    the canonical pattern the catalog wants. The gate must NOT block it.

    This is the positive control: it would catch a future regression
    where the gate became over-eager and started rejecting valid
    payloads. The spec carries a ``sources`` block so the unwired-button
    walker sees ``run_source`` references a real key.
    """
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430444")
    widget = {
        "name": "Refresh sales",
        "type": "button",
        "spec": {
            "type": "button",
            "props": {
                "label": "Refresh",
                "on_click": {"action": "run_source", "source": "sales"},
            },
            "sources": {"sales": {"kind": "static", "data": []}},
        },
    }
    with _patch_seams(doc, allowed_types=_BUTTON_OK_TYPES):
        view, err = await pockets_service.agent_add_widget(doc.id, widget)

    assert err is None, f"gate falsely rejected a valid widget: {err!r}"
    assert view is not None
    # The widget landed in the array and the doc was saved exactly once.
    assert len(doc.widgets) == 1
    assert doc.widgets[0].name == "Refresh sales"
    assert doc.saves == 1


# ---------------------------------------------------------------------------
# Manifest-unavailable best-effort — gate becomes a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_add_widget_skips_gate_when_manifest_unavailable() -> None:
    """When the widget manifest can't be fetched the catalog gate is a
    no-op — same best-effort posture as ``_gate_catalog`` for the
    pocket-level surface. A spec that would otherwise be rejected lands
    so the agent isn't blocked by a transient manifest outage.

    The action-wiring half (verb + unwired-button walkers) lives inside
    the catalog gate's strict branch, so it skips alongside the catalog
    walk when no manifest is reachable. The MCP-layer verb check (#1208)
    still fires because it doesn't need a manifest — but this test
    exercises the service-layer path directly, so we expect the write to
    succeed.
    """
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430555")
    widget = {
        "name": "Refresh sales",
        "type": "button",
        "spec": {
            "type": "button",
            "props": {
                "label": "Refresh",
                "on_click": {"action": "fetch"},  # would normally be rejected
            },
        },
    }
    with _patch_seams(doc, allowed_types=None):
        view, err = await pockets_service.agent_add_widget(doc.id, widget)

    assert err is None
    assert view is not None
    assert len(doc.widgets) == 1
    assert doc.saves == 1


# ---------------------------------------------------------------------------
# Native widgets skip the gate even with a stale spec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_add_widget_native_type_skips_gate() -> None:
    """A native widget carries no rippleSpec by contract; the gate must
    short-circuit on ``type="native"`` so a defensive caller passing in a
    stale spec dict doesn't get blocked. Mirrors the MCP-layer
    ``_validate_widget_spec`` posture.
    """
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430666")
    widget = {
        "name": "AgentStatus",
        "type": "native",
        # Stale spec — would fail the gate if we didn't skip on native.
        "spec": {"type": "button", "props": {"on_click": {"action": "fetch"}}},
    }
    with _patch_seams(doc, allowed_types=_BUTTON_OK_TYPES):
        view, err = await pockets_service.agent_add_widget(doc.id, widget)

    assert err is None, f"native widget falsely rejected: {err!r}"
    assert view is not None
    assert len(doc.widgets) == 1
    assert doc.saves == 1


# ---------------------------------------------------------------------------
# agent_update_widget — non-spec patches skip the gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_update_widget_without_spec_skips_gate() -> None:
    """A patch that only touches ``name`` / ``color`` / ``span`` does NOT
    re-run the gate against the existing spec. The gate is scoped to
    "the new spec the caller is supplying" — re-validating an
    already-persisted spec on every name change would be a footgun.
    """
    widget = _WidgetDoc(
        name="Refresh sales",
        type="button",
        icon="refresh-cw",
        color="#0A84FF",
        spec={
            "type": "button",
            "props": {
                "label": "Refresh",
                "on_click": {"action": "run_source", "source": "sales"},
            },
        },
    )
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430777", widgets=[widget])

    with _patch_seams(doc, allowed_types=_BUTTON_OK_TYPES):
        view, err = await pockets_service.agent_update_widget(
            doc.id,
            widget.id,
            {"name": "Refresh sales today"},
        )

    assert err is None
    assert view is not None
    # Name changed, spec untouched, one save.
    assert doc.widgets[0].name == "Refresh sales today"
    assert doc.saves == 1


# ---------------------------------------------------------------------------
# MCP-layer intermediate option — verb check fires without a manifest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_validate_widget_spec_rejects_unknown_verb_without_manifest() -> None:
    """The MCP-layer ``_validate_widget_spec`` (the lighter intermediate
    option from #1208) now runs the OSS-side ``validate_action_verbs``
    walker BEFORE the manifest check, so a hallucinated verb is rejected
    even when the manifest can't be reached.

    This complements the service-layer gate: it's a cheap belt-and-
    suspenders that gives the agent a tight corrective hint at the MCP
    boundary, saving a cloud round-trip on the most common failure mode.
    """
    from pocketpaw_ee.agent.mcp_servers.pockets import _validate_widget_spec

    widget = {
        "type": "button",
        "spec": {
            "type": "button",
            "props": {
                "label": "Refresh",
                "on_click": {"action": "fetch"},
            },
        },
    }
    # Patch the manifest fetch to None — verb check still runs because
    # it's a pure function with no manifest dependency.
    with patch(
        "pocketpaw_ee.agent.mcp_servers.pockets._get_manifest_for_validation",
        new=AsyncMock(return_value=None),
    ):
        err = await _validate_widget_spec(widget)

    assert err is not None
    assert "fetch" in err


@pytest.mark.asyncio
async def test_mcp_validate_widget_spec_accepts_canonical_verb() -> None:
    """Positive control for the MCP-layer verb check — a canonical verb
    passes the verb walker. The manifest then has nothing to validate
    (no manifest fetched in this test), so the function returns None.
    """
    from pocketpaw_ee.agent.mcp_servers.pockets import _validate_widget_spec

    widget = {
        "type": "button",
        "spec": {
            "type": "button",
            "props": {
                "label": "Refresh",
                "on_click": {"action": "run_source", "source": "sales"},
            },
        },
    }
    with patch(
        "pocketpaw_ee.agent.mcp_servers.pockets._get_manifest_for_validation",
        new=AsyncMock(return_value=None),
    ):
        err = await _validate_widget_spec(widget)

    assert err is None
