# tests/cloud/surface/test_other_hand_handler.py — the /other-hand surface.
#
# Created: 2026-08-25 (feat/other-hand-surface, Otherhand v1). Covers:
#   * the preamble happy path — the snapshot path and free_y both reach the
#     agent, in the shape the frozen contract's section 3 specifies;
#   * the degraded path — a turn with the hints missing renders a usable string
#     and never a preamble pointing at a page that is not there;
#   * the never-raise invariant, driven by a handler-internal failure;
#   * the meta wire path — ``resolve_surface_context`` actually carries
#     snapshot_path/free_y from the client dict to the handler. Three modules
#     have to agree (domain / dto / service._meta_from_request) and the failure
#     mode of forgetting one is silent: the DTO validates the field away and the
#     handler sees ``None`` forever.
#   * the resolved PROFILE — ripple off, the two pocket-creation ids denied, the
#     page-ops system prompt present.
#
# No ``mongo_db`` fixture: this handler reads nothing. pytest-asyncio runs in
# auto mode, so async tests need no module-level mark (and a module mark would
# wrongly tag the sync profile tests).

from __future__ import annotations

from pocketpaw_ee.cloud.surface import (
    SurfaceKind,
    SurfaceMeta,
    resolve_profile,
    resolve_surface_context,
)
from pocketpaw_ee.cloud.surface.handlers import other_hand as handler

WORKSPACE = "ws-surface-otherhand"
USER = "u-otherhand"

SNAPSHOT = "/var/pocketpaw/workspaces/ws-surface-otherhand/other_hand/page-1.png"

# The two ids an allow-list provably cannot strip: they are unioned back by
# ``claude_sdk.POCKET_CREATION_GRANT`` and their servers are in
# ``ALWAYS_ALLOWED_MCP_SERVERS``. Only the deny removes them.
_POCKET_CREATION_IDS = (
    "mcp__pocketpaw_pocket_specialist__create",
    "mcp__pocketpaw_pocket_planner__plan_pocket",
)


# --- preamble ---------------------------------------------------------------


async def test_preamble_carries_snapshot_path_and_free_y() -> None:
    """Happy path: the agent is told where the page is and where it may draw."""
    preamble = await handler.build_preamble(
        WORKSPACE,
        USER,
        SurfaceMeta(route_path="/other-hand", snapshot_path=SNAPSHOT, free_y="820"),
    )
    text = preamble.text

    assert '<surface kind="other_hand"' in text
    # The path — the agent's only route to seeing the page.
    assert SNAPSHOT in text
    assert "Read it" in text
    # free_y, as the rule the agent must respect.
    assert "y=820" in text
    assert "y >= 820" in text
    # The output contract's headline instruction.
    assert "page-ops" in text
    # The page is the surface, not a chat.
    assert "not a chat" in text.lower()
    # A key was claimed, and it names what the preamble depends on.
    assert preamble.cache_key
    assert "820" in preamble.cache_key


async def test_preamble_key_moves_when_the_page_does() -> None:
    """A new free_y is a new preamble — a key that held still would tell the
    agent to draw over ink the user just laid down."""
    first = await handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(snapshot_path=SNAPSHOT, free_y="400")
    )
    second = await handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(snapshot_path=SNAPSHOT, free_y="900")
    )
    assert first.cache_key != second.cache_key


async def test_preamble_without_snapshot_hints_does_not_invent_a_page() -> None:
    """A turn carrying no snapshot renders the surface tag and stops.

    This handler has no upstream service, so "missing hints" IS its failure
    mode: an older client, a snapshot POST that failed, a stray turn stamped
    other_hand. Naming a path that does not exist would teach the agent to
    report on a page it never saw.
    """
    preamble = await handler.build_preamble(WORKSPACE, USER, SurfaceMeta())
    text = preamble.text

    assert isinstance(text, str)
    assert '<surface kind="other_hand"' in text
    assert "no page image" in text
    # Crucially: no instruction to emit ops against a page that isn't there.
    assert "y >=" not in text


async def test_preamble_drops_unparseable_free_y() -> None:
    """A non-numeric free_y degrades rather than reaching the agent as a
    coordinate it would draw at."""
    preamble = await handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(snapshot_path=SNAPSHOT, free_y="not-a-number")
    )
    assert "no page image" in preamble.text


async def test_preamble_never_raises(monkeypatch) -> None:
    """The handler contract's fourth invariant, driven by an internal failure."""

    def _boom(_raw):
        raise RuntimeError("free_y exploded")

    monkeypatch.setattr(handler, "_clamp_free_y", _boom)
    preamble = await handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(snapshot_path=SNAPSHOT, free_y="820")
    )
    assert isinstance(preamble.text, str)
    assert '<surface kind="other_hand"' in preamble.text
    assert preamble.cache_key is None


# --- the meta wire path -----------------------------------------------------


async def test_resolver_carries_snapshot_hints_from_the_wire() -> None:
    """domain / dto / ``_meta_from_request`` all have to know the new fields.

    Forgetting any one of the three is SILENT — pydantic drops the unknown
    field, the handler sees ``None``, and the agent is never shown the page.
    Drive the real resolver from a raw client dict to pin all three at once.
    """
    ctx = await resolve_surface_context(
        WORKSPACE,
        USER,
        {
            "surface": "other_hand",
            "meta": {
                "route_path": "/other-hand",
                "snapshot_path": SNAPSHOT,
                "free_y": "640",
            },
        },
    )

    assert ctx.kind is SurfaceKind.OTHER_HAND
    assert ctx.meta.snapshot_path == SNAPSHOT
    assert ctx.meta.free_y == "640"
    assert SNAPSHOT in ctx.preamble
    assert "y >= 640" in ctx.preamble


# --- profile ----------------------------------------------------------------


def test_profile_denies_pocket_creation() -> None:
    """The non-negotiable one.

    An allow-list cannot strip these: ``POCKET_CREATION_GRANT`` unions them back
    and ``ALWAYS_ALLOWED_MCP_SERVERS`` keeps their servers alive through any
    allow-list. Only ``deny_mcp_tool_ids`` wins, and it is applied BEFORE the
    grant union. Without it, "draw me a mitosis diagram" builds a POCKET.
    """
    profile = resolve_profile(SurfaceKind.OTHER_HAND, SurfaceMeta())
    for tool_id in _POCKET_CREATION_IDS:
        assert tool_id in profile.deny_mcp_tool_ids


def test_profile_turns_ripple_off() -> None:
    """The inline-ripple "default to ui-spec" LAW is actively wrong on a
    page-ops surface — the same reason /sites svelte-create turns it off."""
    assert resolve_profile(SurfaceKind.OTHER_HAND, SurfaceMeta()).ripple_mode == "off"


def test_profile_carries_the_page_ops_output_contract() -> None:
    """The override is the positive half: a prohibition does not create a
    default, so the surface has to be told what the deliverable looks like."""
    override = resolve_profile(SurfaceKind.OTHER_HAND, SurfaceMeta()).system_message_override
    assert override

    # Coordinate space + margins (frozen by contract section 1).
    assert "1240" in override
    assert "1754" in override
    assert "1140" in override
    # The full op vocabulary — every type the renderer knows.
    for op_type in ("text", "line", "circle", "ellipse", "rect", "path", "arrow"):
        assert f'"t":"{op_type}"' in override
    # The reply shape and the free_y rule.
    assert "page-ops" in override
    assert "free_y" in override
    assert "size: 20" in override


def test_profile_keeps_read_available() -> None:
    """``Read`` is the whole vision path on this surface — the agent reads the
    page PNG off disk because nothing else carries an image to the model. A deny
    or an exclusive allow-list that removed it would blind the surface."""
    profile = resolve_profile(SurfaceKind.OTHER_HAND, SurfaceMeta())
    assert "Read" not in profile.deny_mcp_tool_ids
    assert profile.allowed_sdk_tools is None
