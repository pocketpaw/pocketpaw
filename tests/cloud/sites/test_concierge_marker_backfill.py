# tests/cloud/sites/test_concierge_marker_backfill.py — the CR-12 backfill keeps
# every live bar live.
#
# Created 2026-09-28 (feat/concierge-manual-create, CR-12). CR-12 requires
# ``Site.concierge_created_at`` to serve a concierge and flips
# ``concierge_enabled`` to default False. Rows written before it carry neither
# (many never stored the switch at all), so without
# ``sites.migrate_concierge_marker`` the deploy silences every bar. Pinned here:
#   * a bar bound to a LIVE agent becomes an existing legacy concierge, still on
#     (or still off, if its owner had switched it off);
#   * everything else becomes "none", with the switch written as it was;
#   * a second run changes nothing, and never marks a row the first run left as
#     none (that would be automatic creation by the back door);
#   * an empty database passes, and an unreachable bar store writes nothing.
# Rows are made "pre-CR-12" by unsetting the two fields after insert, which is
# the shape an old document has on disk.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.auth.site_keys import concierge_available
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites.migrate_concierge_marker import backfill_concierge_marker

from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

pytestmark = pytest.mark.asyncio

_WS = "ws-backfill"
_OWNER = "user:maya"


@pytest_asyncio.fixture
async def store(tmp_path, mongo_db):  # noqa: ARG001 — mongo_db initialises Beanie
    s = PawBarStore(tmp_path / "backfill.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=s):
        yield s


async def _old_site(pocket_id: str, *, stored_enabled: bool | None = None) -> Site:
    """A Site as a pre-CR-12 row stores it: no marker, and the switch only if set."""
    site = Site(workspace=_WS, pocket_id=pocket_id, owner=_OWNER, name=pocket_id)
    await site.insert()
    # Pre-CR-1 rows carry no runtime either, which is what the backfill fills in.
    unset: dict[str, Any] = {"concierge_created_at": "", "concierge_runtime": ""}
    if stored_enabled is None:
        unset["concierge_enabled"] = ""
    coll = Site.get_pymongo_collection()
    await coll.update_one({"_id": site.id}, {"$unset": unset})
    if stored_enabled is not None:
        await coll.update_one({"_id": site.id}, {"$set": {"concierge_enabled": stored_enabled}})
    return site


async def _raw(site: Site) -> dict[str, Any]:
    return await Site.get_pymongo_collection().find_one({"_id": site.id})


async def _bar(store: PawBarStore, pocket_id: str, agent_id: str = "") -> PawBarWidget:
    return await store.create_widget(
        PawBarWidget(
            pocket_id=pocket_id,
            owner=_OWNER,
            name=pocket_id,
            workspace_id=_WS,
            agent_id=agent_id,
            spec=PawBarSpec(widget_id="pending", pocket_id=pocket_id, blocks=[]),
        )
    )


async def _bind_live_agent(store: PawBarStore, site: Site) -> str:
    from pocketpaw_ee.paw_bar.agent_provisioning import ensure_site_agent

    widget = await _bar(store, site.pocket_id)
    agent_id = await ensure_site_agent(site, widget)
    assert agent_id
    return agent_id


async def test_a_bound_site_stays_a_live_concierge(store) -> None:
    site = await _old_site("pk-bound")
    await _bind_live_agent(store, site)
    assert concierge_available(await Site.get(site.id)) is False, "the outage without it"

    stats = await backfill_concierge_marker(store=store)

    raw = await _raw(site)
    assert isinstance(raw["concierge_created_at"], datetime)
    assert raw["concierge_enabled"] is True
    assert raw["concierge_runtime"] == "legacy"
    assert concierge_available(await Site.get(site.id)) is True
    assert (stats.marked, stats.unmarked) == (1, 0)


async def test_an_owner_who_had_switched_it_off_keeps_it_off(store) -> None:
    site = await _old_site("pk-bound-off", stored_enabled=False)
    await _bind_live_agent(store, site)

    await backfill_concierge_marker(store=store)

    raw = await _raw(site)
    # A bound bar is an existing concierge, and the owner's "off" survives it.
    assert isinstance(raw["concierge_created_at"], datetime)
    assert raw["concierge_enabled"] is False


async def test_an_unbound_site_is_left_at_none_with_its_switch_written(store) -> None:
    unbound = await _old_site("pk-unbound")
    await _bar(store, "pk-unbound")
    no_bar = await _old_site("pk-nobar", stored_enabled=False)

    stats = await backfill_concierge_marker(store=store)

    raw = await _raw(unbound)
    assert "concierge_created_at" in raw and raw["concierge_created_at"] is None
    assert raw["concierge_enabled"] is True, "the old effective value, written explicitly"
    assert (await _raw(no_bar))["concierge_enabled"] is False
    assert concierge_available(await Site.get(unbound.id)) is False
    assert (stats.marked, stats.unmarked) == (0, 2)


async def test_a_bar_bound_to_a_deleted_agent_is_none(store) -> None:
    site = await _old_site("pk-dangling")
    await _bar(store, "pk-dangling", agent_id="6aba05fcae658c38aec34999")

    await backfill_concierge_marker(store=store)

    assert (await _raw(site))["concierge_created_at"] is None


async def test_a_second_run_changes_nothing_and_never_marks_a_later_bind(store) -> None:
    site = await _old_site("pk-later")
    await _bar(store, "pk-later")
    await backfill_concierge_marker(store=store)

    # Someone binds an agent to the bar AFTER the backfill ran. That is not the
    # owner creating a concierge, so the next run must not treat it as one.
    from pocketpaw_ee.paw_bar.agent_provisioning import ensure_site_agent

    widget = (await store.list_widgets(pocket_id="pk-later", workspace_id=_WS, limit=1))[0]
    await ensure_site_agent(await Site.get(site.id), widget)

    stats = await backfill_concierge_marker(store=store)
    assert stats.examined == 0
    assert (await _raw(site))["concierge_created_at"] is None


async def test_rows_the_new_model_writes_are_never_selected(store) -> None:
    """Relies on Beanie storing ``None`` as null (keep_nulls): a new row carries
    the marker field, so only pre-CR-12 rows are ever classified."""
    fresh = Site(workspace=_WS, pocket_id="pk-new", owner=_OWNER)
    await fresh.insert()
    raw = await _raw(fresh)
    assert "concierge_created_at" in raw and raw["concierge_created_at"] is None

    created = Site(
        workspace=_WS,
        pocket_id="pk-created",
        owner=_OWNER,
        concierge_created_at=datetime.now(UTC),
    )
    await created.insert()

    stats = await backfill_concierge_marker(store=store)
    assert stats.examined == 0


async def test_an_empty_database_passes(store) -> None:
    stats = await backfill_concierge_marker(store=store)
    assert stats.examined == 0 and stats.refused is False


async def test_an_unreachable_bar_store_writes_nothing(tmp_path, mongo_db) -> None:  # noqa: ARG001
    site = await _old_site("pk-blind")
    missing = PawBarStore(tmp_path / "nowhere" / "paw_bar.db")

    stats = await backfill_concierge_marker(store=missing)

    assert stats.refused is True
    raw = await _raw(site)
    assert "concierge_created_at" not in raw
    assert "concierge_enabled" not in raw


async def test_a_dry_run_writes_nothing(store) -> None:
    site = await _old_site("pk-dry")
    await _bind_live_agent(store, site)

    stats = await backfill_concierge_marker(store=store, dry_run=True)

    assert stats.marked == 1
    assert "concierge_created_at" not in await _raw(site)
