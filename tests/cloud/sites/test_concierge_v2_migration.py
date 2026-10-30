# tests/cloud/sites/test_concierge_v2_migration.py — moving existing legacy
# concierges to the v2 runtime once the eval gate opens.
#
# ``sites.migrate_concierge_v2`` rewrites ``concierge_runtime`` from "legacy" to
# "v2" on sites that have a concierge, but only while
# ``concierge_gate.default_concierge_runtime()`` says "v2", and never for a site
# that leans on something only the legacy agent run does. Pinned here:
#   * a plain legacy concierge (dedicated, untouched agent; no actions, or only the
#     add_to_cart / checkout card verbs) moves, and keeps its agent binding;
#   * each legacy-only case is skipped and listed with its reason: a declared
#     server-executed verb, a hand-bound agent, an owner-customised agent, a bar
#     with no live agent, and a site with no bar;
#   * a second run changes nothing; a dry run writes nothing; a shut gate writes
#     nothing; a site with no concierge, or already on v2, is never touched.

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import migrate_concierge_v2
from pocketpaw_ee.sites.migrate_concierge_v2 import migrate_concierges_to_v2

from pocketpaw.paw_bar.models import PawBarActionSpec, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

pytestmark = pytest.mark.asyncio

_WS = "ws-v2-move"
_OWNER = "user:maya"


@pytest_asyncio.fixture
async def store(tmp_path, mongo_db):  # noqa: ARG001 — mongo_db initialises Beanie
    s = PawBarStore(tmp_path / "v2-move.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=s):
        yield s


@pytest.fixture
def gate(monkeypatch):
    """The gate's answer, open by default; a test flips it with ``gate("legacy")``."""
    state = {"runtime": "v2"}
    monkeypatch.setattr(migrate_concierge_v2, "_gate_runtime", lambda: state["runtime"])

    def _set(runtime: str) -> None:
        state["runtime"] = runtime

    return _set


async def _concierge_site(pocket_id: str, *, runtime: str = "legacy") -> Site:
    from datetime import UTC, datetime

    site = Site(
        workspace=_WS,
        pocket_id=pocket_id,
        owner=_OWNER,
        name=pocket_id,
        concierge_runtime=runtime,
        concierge_enabled=True,
        concierge_created_at=datetime.now(UTC),
    )
    await site.insert()
    return site


async def _bar(store: PawBarStore, pocket_id: str, actions: list[PawBarActionSpec] | None = None):
    return await store.create_widget(
        PawBarWidget(
            pocket_id=pocket_id,
            owner=_OWNER,
            name=pocket_id,
            workspace_id=_WS,
            spec=PawBarSpec(
                widget_id="pending",
                pocket_id=pocket_id,
                blocks=[],
                actions=actions or [],
                checkout_url="https://shop.test/checkout/{cart_ref}",
            ),
        )
    )


async def _legacy_concierge(
    store: PawBarStore, pocket_id: str, actions: list[PawBarActionSpec] | None = None
) -> tuple[Site, str]:
    """A legacy concierge as CR-12's create leaves it: bar + dedicated agent."""
    from pocketpaw_ee.paw_bar.agent_provisioning import ensure_site_agent

    site = await _concierge_site(pocket_id)
    widget = await _bar(store, pocket_id, actions)
    agent_id = await ensure_site_agent(site, widget)
    assert agent_id
    return site, agent_id


async def _runtime(site: Site) -> Any:
    raw = await Site.get_pymongo_collection().find_one({"_id": site.id})
    return raw.get("concierge_runtime")


async def _agent_doc(agent_id: str):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.agent import Agent

    return await Agent.get(PydanticObjectId(agent_id))


_CART = [
    PawBarActionSpec(verb="add_to_cart", policy="auto", args={"product_id": "str"}),
    PawBarActionSpec(verb="checkout", policy="auto"),
]


async def test_a_plain_legacy_concierge_moves_and_keeps_its_agent(store, gate) -> None:
    site, agent_id = await _legacy_concierge(store, "pk-plain")

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(site) == "v2"
    widgets = await store.list_widgets(pocket_id="pk-plain", workspace_id=_WS, limit=1)
    assert widgets[0].agent_id == agent_id, "the legacy binding is kept, not deleted"
    assert (stats.examined, stats.moved, stats.skipped) == (1, 1, [])


async def test_cart_verbs_alone_are_not_legacy_only(store, gate) -> None:
    site, _ = await _legacy_concierge(store, "pk-cart", _CART)

    await migrate_concierges_to_v2(store=store)

    assert await _runtime(site) == "v2"


async def test_a_declared_server_verb_is_skipped(store, gate) -> None:
    actions = [
        *_CART,
        PawBarActionSpec(verb="book_table", policy="gated", args={"date": "str"}, label="Book"),
    ]
    site, _ = await _legacy_concierge(store, "pk-gated", actions)

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(site) == "legacy"
    assert stats.moved == 0
    assert [(s.site_id, s.reason) for s in stats.skipped] == [
        (str(site.id), "declares server-run actions: book_table")
    ]


async def test_an_argless_gated_verb_is_skipped(store, gate) -> None:
    site, _ = await _legacy_concierge(
        store, "pk-ping", [PawBarActionSpec(verb="call_me_back", policy="gated")]
    )

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(site) == "legacy"
    assert stats.skipped[0].reason == "declares server-run actions: call_me_back"


async def test_a_hand_bound_agent_is_skipped(store, gate) -> None:
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    site = await _concierge_site("pk-hand")
    widget = await _bar(store, "pk-hand")
    agent = await agents_service.create(
        agents_service.legacy_ctx(_OWNER, _WS),
        _WS,
        CreateAgentRequest(name="Barista", slug="barista", visibility="workspace"),
    )
    await store.update_fields(widget.id, {"agent_id": agent.id}, workspace_id=_WS)

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(site) == "legacy"
    assert stats.skipped[0].reason == "bound to a hand-picked agent (barista)"


@pytest.mark.parametrize(
    ("field", "value", "named"),
    [
        ("system_prompt", "Always upsell the large bag.", "system_prompt"),
        ("model", "anthropic:claude-opus-4", "model"),
        ("tools", ["web_search"], "tools"),
        ("skill_refs", ["skill:pricing"], "skill_refs"),
        ("soul_persona", "You are Bean, a grumpy barista.", "persona"),
    ],
)
async def test_an_owner_customised_agent_is_skipped(store, gate, field, value, named) -> None:
    site, agent_id = await _legacy_concierge(store, f"pk-custom-{field}")
    doc = await _agent_doc(agent_id)
    setattr(doc.config, field, value)
    await doc.save()

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(site) == "legacy"
    assert stats.skipped[0].reason == f"its agent is customised: {named}"


async def test_a_bar_with_no_live_agent_is_skipped(store, gate) -> None:
    unbound = await _concierge_site("pk-unbound")
    await _bar(store, "pk-unbound")
    dangling = await _concierge_site("pk-dangling")
    widget = await _bar(store, "pk-dangling")
    await store.update_fields(widget.id, {"agent_id": "6aba05fcae658c38aec34999"}, workspace_id=_WS)

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(unbound) == "legacy"
    assert await _runtime(dangling) == "legacy"
    assert sorted(s.reason for s in stats.skipped) == [
        "its bar has no agent",
        "its bar's agent no longer exists",
    ]


async def test_a_site_with_no_bar_is_skipped(store, gate) -> None:
    site = await _concierge_site("pk-nobar")

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(site) == "legacy"
    assert stats.skipped[0].reason == "it has no bar"


async def test_sites_without_a_concierge_or_already_on_v2_are_untouched(store, gate) -> None:
    none = Site(workspace=_WS, pocket_id="pk-none", owner=_OWNER, name="none")
    await none.insert()
    await _bar(store, "pk-none")
    already = await _concierge_site("pk-already", runtime="v2")

    stats = await migrate_concierges_to_v2(store=store)

    assert await _runtime(none) == "legacy", "no concierge: the create path picks its runtime"
    assert await _runtime(already) == "v2"
    assert stats.examined == 0


async def test_a_second_run_changes_nothing(store, gate) -> None:
    moved, _ = await _legacy_concierge(store, "pk-once")
    kept, _ = await _legacy_concierge(
        store, "pk-kept", [PawBarActionSpec(verb="book_table", args={"date": "str"})]
    )

    first = await migrate_concierges_to_v2(store=store)
    second = await migrate_concierges_to_v2(store=store)

    assert (first.moved, len(first.skipped)) == (1, 1)
    assert (second.moved, len(second.skipped)) == (0, 1), "a skipped site is re-checked, not moved"
    assert await _runtime(moved) == "v2"
    assert await _runtime(kept) == "legacy"


async def test_a_dry_run_writes_nothing(store, gate) -> None:
    site, _ = await _legacy_concierge(store, "pk-dry")

    stats = await migrate_concierges_to_v2(store=store, dry_run=True)

    assert stats.moved == 1, "the dry run reports what it would move"
    assert await _runtime(site) == "legacy"


async def test_a_shut_gate_writes_nothing(store, gate) -> None:
    site, _ = await _legacy_concierge(store, "pk-shut")
    gate("legacy")

    stats = await migrate_concierges_to_v2(store=store)

    assert stats.gate_shut is True
    assert (stats.examined, stats.moved) == (0, 0)
    assert await _runtime(site) == "legacy"


async def test_the_real_gate_is_asked(store, monkeypatch) -> None:
    """Without the fixture's stub, the module asks ``default_concierge_runtime``."""
    site, _ = await _legacy_concierge(store, "pk-real-gate")
    monkeypatch.setattr(
        "pocketpaw_ee.paw_bar.concierge_gate.default_concierge_runtime", lambda: "legacy"
    )

    stats = await migrate_concierges_to_v2(store=store)

    assert stats.gate_shut is True
    assert await _runtime(site) == "legacy"
