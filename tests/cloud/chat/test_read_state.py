"""ReadState model + unread_service.mark_read / bump_mention against a real
(in-memory) MongoDB. Covers the atomic upsert behavior and the unique
(user, group) index."""

from __future__ import annotations

from datetime import datetime

import pytest


@pytest.mark.asyncio
async def test_read_state_defaults(beanie_memory_db):
    from pocketpaw_ee.cloud.models.read_state import ReadState

    rs = ReadState(user="u1", group="g1", last_read_message_id="m1")
    assert rs.mention_unread == 0
    assert isinstance(rs.last_read_at, datetime)


@pytest.mark.asyncio
async def test_mark_read_creates_row_when_missing(beanie_memory_db):
    from pocketpaw_ee.cloud.chat import unread_service
    from pocketpaw_ee.cloud.models.read_state import ReadState

    await unread_service.mark_read("u1", "g1", "m5")

    state = await ReadState.find_one({"user": "u1", "group": "g1"})
    assert state is not None
    assert state.last_read_message_id == "m5"
    assert state.mention_unread == 0


@pytest.mark.asyncio
async def test_mark_read_updates_existing_row_and_zeros_mention_unread(beanie_memory_db):
    from pocketpaw_ee.cloud.chat import unread_service
    from pocketpaw_ee.cloud.models.read_state import ReadState

    # Seed a pre-existing state with mention_unread > 0.
    await ReadState(user="u1", group="g1", last_read_message_id="m1", mention_unread=3).insert()

    await unread_service.mark_read("u1", "g1", "m9")

    state = await ReadState.find_one({"user": "u1", "group": "g1"})
    assert state is not None
    assert state.last_read_message_id == "m9"
    assert state.mention_unread == 0


@pytest.mark.asyncio
async def test_mark_read_is_idempotent_under_repeat(beanie_memory_db):
    """Calling mark_read twice in a row must not produce duplicate rows.
    Regression for the find-then-insert race fix."""
    from pocketpaw_ee.cloud.chat import unread_service
    from pocketpaw_ee.cloud.models.read_state import ReadState

    await unread_service.mark_read("u1", "g1", "m1")
    await unread_service.mark_read("u1", "g1", "m2")

    rows = await ReadState.find({"user": "u1", "group": "g1"}).to_list()
    assert len(rows) == 1
    assert rows[0].last_read_message_id == "m2"


@pytest.mark.asyncio
async def test_bump_mention_creates_row_with_empty_last_read(beanie_memory_db):
    from pocketpaw_ee.cloud.chat import unread_service
    from pocketpaw_ee.cloud.models.read_state import ReadState

    await unread_service.bump_mention("u1", "g1")

    state = await ReadState.find_one({"user": "u1", "group": "g1"})
    assert state is not None
    assert state.mention_unread == 1
    assert state.last_read_message_id == ""


@pytest.mark.asyncio
async def test_bump_mention_increments_existing_counter(beanie_memory_db):
    from pocketpaw_ee.cloud.chat import unread_service
    from pocketpaw_ee.cloud.models.read_state import ReadState

    await unread_service.bump_mention("u1", "g1")
    await unread_service.bump_mention("u1", "g1")
    await unread_service.bump_mention("u1", "g1")

    state = await ReadState.find_one({"user": "u1", "group": "g1"})
    assert state is not None
    assert state.mention_unread == 3


@pytest.mark.asyncio
async def test_bump_mention_does_not_overwrite_last_read(beanie_memory_db):
    """If a user already acked a message, bumping their mention counter
    must not reset last_read_message_id back to empty."""
    from pocketpaw_ee.cloud.chat import unread_service
    from pocketpaw_ee.cloud.models.read_state import ReadState

    await unread_service.mark_read("u1", "g1", "m5")
    await unread_service.bump_mention("u1", "g1")

    state = await ReadState.find_one({"user": "u1", "group": "g1"})
    assert state is not None
    assert state.last_read_message_id == "m5"
    assert state.mention_unread == 1
