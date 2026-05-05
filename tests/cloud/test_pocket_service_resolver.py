"""service.get resolves $source markers in rippleSpec on read.

Pure unit test — does not require a Mongo connection. Mocks the doc
fetch and the source registry seam.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ee.cloud.pockets import service as pocket_service


def _fake_doc(spec: dict) -> SimpleNamespace:
    """Mimic the _PocketDoc shape that _pocket_to_domain expects."""
    return SimpleNamespace(
        id="pocket-1",
        workspace="w1",
        name="Test",
        description="",
        type="custom",
        icon="",
        color="",
        owner="u1",
        team=[],
        agents=[],
        widgets=[],
        rippleSpec=spec,
        visibility="workspace",
        share_link_token=None,
        share_link_access="view",
        shared_with=[],
        tool_specs=[],
    )


async def test_get_resolves_workspace_pockets_marker():
    # Pre-import so the @register side-effects fire before we patch the registry.
    import ee.cloud.ripple_sources  # noqa: F401

    spec = {
        "state": {
            "all": {"$source": "workspace.pockets"},
            "draft": "",
        },
        "ui": {"type": "flex", "props": {"direction": "column"}, "children": []},
    }

    fake_pockets = [{"id": "p1", "name": "X", "type": "custom", "icon": "", "color": ""}]

    async def _fake_workspace_pockets(ctx, args):
        return fake_pockets

    with (
        patch(
            "ee.cloud.pockets.service._fetch_pocket",
            new=AsyncMock(return_value=_fake_doc(spec)),
        ),
        patch.dict(
            "ee.cloud.ripple_resolver._REGISTRY",
            {"workspace.pockets": _fake_workspace_pockets},
        ),
    ):
        out = await pocket_service.get(pocket_id="pocket-1", user_id="u1")

    state = out["rippleSpec"]["state"]
    assert state["all"] == fake_pockets
    assert state["draft"] == ""  # untouched literal
    # Marker syntax must NOT remain in the resolved output.
    assert "$source" not in str(state["all"])


async def test_get_passes_through_when_no_markers():
    spec = {
        "state": {"draft": "", "tasks": [{"id": "t1", "title": "x"}]},
        "ui": {"type": "stat"},
    }
    with patch(
        "ee.cloud.pockets.service._fetch_pocket",
        new=AsyncMock(return_value=_fake_doc(spec)),
    ):
        out = await pocket_service.get(pocket_id="pocket-1", user_id="u1")
    assert out["rippleSpec"]["state"] == spec["state"]
    assert out["rippleSpec"]["ui"] == spec["ui"]


async def test_get_with_no_ripple_spec_does_not_crash():
    """A pocket with rippleSpec=None must not break the resolver hookup."""
    with patch(
        "ee.cloud.pockets.service._fetch_pocket",
        new=AsyncMock(return_value=_fake_doc(None)),
    ):
        out = await pocket_service.get(pocket_id="pocket-1", user_id="u1")
    # Wire dict shape may vary, but the call must not raise.
    assert "rippleSpec" in out or out.get("rippleSpec") is None


async def test_get_falls_back_to_raw_spec_when_resolver_raises():
    """Plan contract: resolver failure must NOT raise from get. Fall back to raw spec."""
    spec = {"state": {"all": {"$source": "workspace.pockets"}}}

    with (
        patch(
            "ee.cloud.pockets.service._fetch_pocket",
            new=AsyncMock(return_value=_fake_doc(spec)),
        ),
        patch(
            "ee.cloud.ripple_resolver.resolve_ripple_spec",
            new=AsyncMock(side_effect=RuntimeError("walker exploded")),
        ),
    ):
        # Must not raise — the get function's try/except catches and falls through.
        out = await pocket_service.get(pocket_id="pocket-1", user_id="u1")

    # Raw (unresolved) spec is returned.
    assert out["rippleSpec"]["state"]["all"] == {"$source": "workspace.pockets"}


async def test_resolved_wire_dict_resolves_for_given_viewer():
    """Other boundaries (create return, event payload) call _resolved_wire_dict
    directly. Verify it resolves with the supplied viewer's context."""
    import ee.cloud.ripple_sources  # noqa: F401

    spec = {"state": {"all": {"$source": "workspace.pockets"}}}
    fake = [{"id": "p1", "name": "X", "type": "custom", "icon": "", "color": ""}]

    captured_user_id: list[str] = []

    async def _fake(ctx, args):
        captured_user_id.append(ctx.user_id)
        return fake

    with patch.dict("ee.cloud.ripple_resolver._REGISTRY", {"workspace.pockets": _fake}):
        out = await pocket_service._resolved_wire_dict(_fake_doc(spec), "alice")

    assert out["rippleSpec"]["state"]["all"] == fake
    assert captured_user_id == ["alice"]


async def test_event_payload_resolves_using_owner_as_viewer():
    """Multi-recipient broadcasts resolve against doc.owner — verifies the
    create/update WebSocket flow doesn't hand raw markers to the renderer."""
    import ee.cloud.ripple_sources  # noqa: F401

    spec = {"state": {"all": {"$source": "workspace.pockets"}}}
    fake = [{"id": "p1", "name": "Owner-View", "type": "custom", "icon": "", "color": ""}]

    captured_user_id: list[str] = []

    async def _fake(ctx, args):
        captured_user_id.append(ctx.user_id)
        return fake

    doc = _fake_doc(spec)  # owner="u1" per fixture
    with patch.dict("ee.cloud.ripple_resolver._REGISTRY", {"workspace.pockets": _fake}):
        payload = await pocket_service._pocket_event_payload(doc)

    assert payload["pocket"]["rippleSpec"]["state"]["all"] == fake
    assert captured_user_id == ["u1"]  # doc.owner
