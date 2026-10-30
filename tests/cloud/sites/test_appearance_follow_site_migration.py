# tests/cloud/sites/test_appearance_follow_site_migration.py — stored appearances
# move off the old look defaults so the bar follows the website.
#
# ``ConciergeAppearance`` now says "follow the site" with accent "", font "site"
# and radius None. Appearances saved before that store the old defaults
# (#3b6fe0 / system / 20) as literal values, which the new semantics would read as
# overrides. ``sites.migrate_appearance_follow_site`` rewrites exactly those.
# Pinned here:
#   * the three old defaults become "follow the site"; any other value is kept;
#   * a row with no stored appearance only gets the version stamp;
#   * a second run changes nothing, and an owner who picks #3b6fe0 after the
#     migration keeps it;
#   * --dry-run writes nothing; an empty database passes.
# Rows are made "old" by unsetting ``concierge_appearance_version`` (and, where a
# test needs the old literals, setting them) after insert: the on-disk shape.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites.migrate_appearance_follow_site import (
    migrate_appearance_follow_site,
)

pytestmark = pytest.mark.asyncio


async def _old_site(name: str, appearance: dict[str, Any] | None) -> Site:
    """A Site as a pre-follow-site row stores it."""
    site = Site(workspace="ws-look", pocket_id=name, owner="user:maya", name=name)
    await site.insert()
    coll = Site.get_pymongo_collection()
    await coll.update_one({"_id": site.id}, {"$unset": {"concierge_appearance_version": ""}})
    if appearance is None:
        await coll.update_one({"_id": site.id}, {"$unset": {"concierge_appearance": ""}})
    else:
        await coll.update_one({"_id": site.id}, {"$set": {"concierge_appearance": appearance}})
    return site


async def _raw(site: Site) -> dict[str, Any]:
    return await Site.get_pymongo_collection().find_one({"_id": site.id})


_OLD_DEFAULTS = {"accent": "#3b6fe0", "font": "system", "radius": 20, "blur": 28}


async def test_the_old_defaults_move_to_follow_the_site(mongo_db) -> None:  # noqa: ARG001
    site = await _old_site("pk-defaults", dict(_OLD_DEFAULTS))

    stats = await migrate_appearance_follow_site()

    look = (await _raw(site))["concierge_appearance"]
    assert look["accent"] == ""
    assert look["font"] == "site"
    assert look["radius"] is None
    assert look["blur"] == 28
    assert (await _raw(site))["concierge_appearance_version"] == 2
    assert (stats.examined, stats.changed) == (1, 1)
    # And the bar now emits nothing for those facets.
    tokens = (await Site.get(site.id)).concierge_appearance.tokens()
    assert not {"--pawbar-accent", "--pawbar-font", "--pawbar-radius"} & set(tokens)


async def test_an_upper_case_old_accent_is_the_old_default_too(mongo_db) -> None:  # noqa: ARG001
    site = await _old_site("pk-upper", {"accent": "#3B6FE0"})
    await migrate_appearance_follow_site()
    assert (await _raw(site))["concierge_appearance"]["accent"] == ""


async def test_values_the_owner_picked_are_kept(mongo_db) -> None:  # noqa: ARG001
    site = await _old_site(
        "pk-custom", {"accent": "#ff0055", "font": "serif", "radius": 8, "colors": {"ink": "#111"}}
    )

    stats = await migrate_appearance_follow_site()

    look = (await _raw(site))["concierge_appearance"]
    assert (look["accent"], look["font"], look["radius"]) == ("#ff0055", "serif", 8)
    assert look["colors"] == {"ink": "#111"}
    assert (await _raw(site))["concierge_appearance_version"] == 2
    assert (stats.examined, stats.changed) == (1, 0)


async def test_a_partly_custom_look_keeps_its_custom_facets(mongo_db) -> None:  # noqa: ARG001
    site = await _old_site("pk-mixed", {"accent": "#ff0055", "font": "system", "radius": 20})
    await migrate_appearance_follow_site()
    look = (await _raw(site))["concierge_appearance"]
    assert (look["accent"], look["font"], look["radius"]) == ("#ff0055", "site", None)


async def test_a_row_with_no_appearance_is_only_stamped(mongo_db) -> None:  # noqa: ARG001
    site = await _old_site("pk-none", None)

    stats = await migrate_appearance_follow_site()

    raw = await _raw(site)
    assert "concierge_appearance" not in raw
    assert raw["concierge_appearance_version"] == 2
    assert (stats.examined, stats.changed) == (1, 0)


async def test_a_second_run_never_resets_a_later_choice(mongo_db) -> None:  # noqa: ARG001
    site = await _old_site("pk-later", dict(_OLD_DEFAULTS))
    await migrate_appearance_follow_site()

    # The owner deliberately picks the old blue after the migration ran.
    await Site.get_pymongo_collection().update_one(
        {"_id": site.id}, {"$set": {"concierge_appearance.accent": "#3b6fe0"}}
    )
    stats = await migrate_appearance_follow_site()

    assert (await _raw(site))["concierge_appearance"]["accent"] == "#3b6fe0"
    assert stats.examined == 0


async def test_a_new_site_is_never_selected(mongo_db) -> None:  # noqa: ARG001
    site = Site(workspace="ws-look", pocket_id="pk-new", owner="user:maya", name="new")
    site.concierge_appearance.accent = "#3b6fe0"
    await site.insert()

    stats = await migrate_appearance_follow_site()

    assert stats.examined == 0
    assert (await _raw(site))["concierge_appearance"]["accent"] == "#3b6fe0"


async def test_a_dry_run_writes_nothing(mongo_db) -> None:  # noqa: ARG001
    site = await _old_site("pk-dry", dict(_OLD_DEFAULTS))

    stats = await migrate_appearance_follow_site(dry_run=True)

    raw = await _raw(site)
    assert raw["concierge_appearance"]["accent"] == "#3b6fe0"
    assert "concierge_appearance_version" not in raw
    assert (stats.examined, stats.changed) == (1, 1)


async def test_an_empty_database_passes(mongo_db) -> None:  # noqa: ARG001
    stats = await migrate_appearance_follow_site()
    assert (stats.examined, stats.changed, stats.skipped) == (0, 0, 0)
