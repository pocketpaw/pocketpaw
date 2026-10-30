"""Tests for init_realtime wiring."""

from __future__ import annotations

import pytest


def test_init_realtime_uses_inprocess_by_default(monkeypatch):
    from pocketpaw_ee.cloud import init_realtime
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus

    bus_mod._bus = None  # type: ignore[attr-defined]
    monkeypatch.delenv("POCKETPAW_REALTIME_BUS", raising=False)

    init_realtime()

    assert isinstance(bus_mod._bus, InProcessBus)  # type: ignore[attr-defined]


def test_init_realtime_exposes_resolver(monkeypatch):
    from pocketpaw_ee.cloud import init_realtime
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.bus import get_resolver

    bus_mod._bus = None  # type: ignore[attr-defined]
    bus_mod._resolver = None  # type: ignore[attr-defined]
    monkeypatch.delenv("POCKETPAW_REALTIME_BUS", raising=False)

    init_realtime()

    assert isinstance(get_resolver(), AudienceResolver)


def test_init_realtime_falls_back_to_inprocess_for_unsupported_bus(monkeypatch, caplog):
    from pocketpaw_ee.cloud import init_realtime
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus

    bus_mod._bus = None  # type: ignore[attr-defined]
    monkeypatch.setenv("POCKETPAW_REALTIME_BUS", "redis")

    with caplog.at_level("WARNING"):
        init_realtime()

    assert isinstance(bus_mod._bus, InProcessBus)  # type: ignore[attr-defined]
    assert any("redis" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_group_list_member_ids_returns_members_field(mongo_db):
    """``list_member_ids`` is the realtime audience lookup — must read
    members from the persisted Group doc."""
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.models.group import Group as _GroupDoc

    doc = _GroupDoc(
        workspace="w1",
        name="G",
        slug="g",
        type="private",
        members=["u1", "u2", "u3"],
        owner="u1",
    )
    await doc.insert()

    ids = await group_service.list_member_ids(str(doc.id))
    assert ids == ["u1", "u2", "u3"]


@pytest.mark.asyncio
async def test_group_list_member_ids_returns_empty_for_missing_group(mongo_db):
    from pocketpaw_ee.cloud.chat import group_service

    ids = await group_service.list_member_ids("507f1f77bcf86cd799439011")
    assert ids == []
