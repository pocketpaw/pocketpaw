"""Regression tests for ASG-1 — scoped discover + additive agent fields.

Created 2026-07-15 (feat/agent-scoped-discover-fields):

1. Scoped discover (the gallery default) must NEVER surface a public agent
   owned by another member — the load-bearing invariant. Also proves the
   ``scoped=False`` legacy union and the explicit ``visibility="public"``
   mode still return public agents (unchanged).
2. The new presentation fields (``welcome_message`` / ``conversation_starters``
   / ``voice`` / ``appearance``) and top-level ``tags`` round-trip through
   create -> get -> update on the wire dict.

Exercises the real Beanie query path against the in-memory ``mongo_db``
mongomock fixture, so the ``$or`` union the service builds is actually run.
Agents are created with ``soul_enabled=False`` so the eager-soul pool
materialization (which needs the OSS AgentPool) never fires in unit scope.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import (
    CreateAgentRequest,
    DiscoverRequest,
    UpdateAgentRequest,
    agent_to_dict,
)

pytestmark = pytest.mark.asyncio

W1 = "w1"
W2 = "w2"
VIEWER = "u_viewer"
OTHER = "u_other"


def _ctx(user_id: str, workspace_id: str):
    return agents_service.legacy_ctx(user_id, workspace_id)


async def _make(owner: str, workspace: str, slug: str, visibility: str):
    """Create an agent as ``owner`` in ``workspace`` (soul disabled)."""
    body = CreateAgentRequest(
        name=slug,
        slug=slug,
        visibility=visibility,
        soul_enabled=False,
    )
    return await agents_service.create(_ctx(owner, workspace), workspace, body)


async def _seed_discover_fixture():
    """Owner's private + a peer's workspace/public agents, plus a cross-ws
    public agent. Returns nothing — tests query via ``discover``."""
    await _make(VIEWER, W1, "mine-private", "private")  # A — owner clause
    await _make(OTHER, W1, "peer-workspace", "workspace")  # B — workspace clause
    await _make(OTHER, W1, "peer-public", "public")  # C — must be excluded
    await _make(OTHER, W2, "cross-ws-public", "public")  # D — must be excluded


async def _slugs(body: DiscoverRequest) -> set[str]:
    items = await agents_service.discover(_ctx(VIEWER, W1), W1, body)
    return {a.slug for a in items}


async def test_scoped_discover_excludes_public(mongo_db):  # noqa: ARG001
    """LOAD-BEARING: the default scoped union returns {owner==me} ∪
    {visibility==workspace} and NEVER a public agent owned by someone else."""
    await _seed_discover_fixture()

    slugs = await _slugs(DiscoverRequest())  # scoped defaults to True

    assert slugs == {"mine-private", "peer-workspace"}
    assert "peer-public" not in slugs
    assert "cross-ws-public" not in slugs


async def test_discover_scoped_defaults_true():
    """The gallery relies on scoped being the default — guard it."""
    assert DiscoverRequest().scoped is True


async def test_unscoped_discover_restores_public_union(mongo_db):  # noqa: ARG001
    """scoped=False keeps the legacy cross-workspace public union so existing
    callers that opt out see public agents again."""
    await _seed_discover_fixture()

    slugs = await _slugs(DiscoverRequest(scoped=False))

    assert slugs == {
        "mine-private",
        "peer-workspace",
        "peer-public",
        "cross-ws-public",
    }


async def test_explicit_public_visibility_unchanged_by_scoped(mongo_db):  # noqa: ARG001
    """Explicit ``visibility="public"`` is an intentional public query — it
    still returns public agents even though ``scoped`` defaults to True."""
    await _seed_discover_fixture()

    # scoped=True (default) must not suppress an EXPLICIT public request.
    items = await agents_service.discover(
        _ctx(VIEWER, W1), W1, DiscoverRequest(visibility="public")
    )
    slugs = {a.slug for a in items}

    assert slugs == {"peer-public", "cross-ws-public"}


async def test_config_fields_and_tags_round_trip(mongo_db):  # noqa: ARG001
    """welcome_message / conversation_starters / voice / appearance / tags
    persist through create -> get and update -> get on the wire dict."""
    create = CreateAgentRequest(
        name="Concierge",
        slug="concierge",
        visibility="workspace",
        soul_enabled=False,
        welcome_message="Hi, how can I help?",
        conversation_starters=["Book a demo", "Pricing"],
        voice={"provider": "elevenlabs", "voice_id": "abc123"},
        appearance={"theme": "dark", "accent": "#4f46e5"},
        tags=["support", "sales"],
    )
    created = await agents_service.create(_ctx(VIEWER, W1), W1, create)

    # Read back through the wire mapper — this is what the router returns.
    wire = agent_to_dict(await agents_service.get(created.id))
    assert wire["config"]["welcome_message"] == "Hi, how can I help?"
    assert wire["config"]["conversation_starters"] == ["Book a demo", "Pricing"]
    assert wire["config"]["voice"] == {"provider": "elevenlabs", "voice_id": "abc123"}
    assert wire["config"]["appearance"] == {"theme": "dark", "accent": "#4f46e5"}
    assert wire["tags"] == ["support", "sales"]

    # Update every new field and confirm persistence (owner is the caller).
    update = UpdateAgentRequest(
        welcome_message="Welcome back!",
        conversation_starters=["Track my order"],
        voice={"provider": "openai", "voice_id": "nova"},
        appearance={"theme": "light"},
        tags=["support"],
    )
    await agents_service.update(_ctx(VIEWER, W1), created.id, update)

    wire2 = agent_to_dict(await agents_service.get(created.id))
    assert wire2["config"]["welcome_message"] == "Welcome back!"
    assert wire2["config"]["conversation_starters"] == ["Track my order"]
    assert wire2["config"]["voice"] == {"provider": "openai", "voice_id": "nova"}
    assert wire2["config"]["appearance"] == {"theme": "light"}
    assert wire2["tags"] == ["support"]


async def test_new_fields_default_empty_on_wire(mongo_db):  # noqa: ARG001
    """An agent created without the new fields exposes safe empty defaults —
    proves the change is additive (no migration, old create paths unaffected)."""
    created = await agents_service.create(
        _ctx(VIEWER, W1),
        W1,
        CreateAgentRequest(name="Plain", slug="plain", soul_enabled=False),
    )
    wire = agent_to_dict(await agents_service.get(created.id))
    assert wire["config"]["welcome_message"] == ""
    assert wire["config"]["conversation_starters"] == []
    assert wire["config"]["voice"] is None
    assert wire["config"]["appearance"] == {}
    assert wire["tags"] == []
