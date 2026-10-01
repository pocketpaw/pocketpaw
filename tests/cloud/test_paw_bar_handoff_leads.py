# tests/cloud/test_paw_bar_handoff_leads.py — a handoff with a contact is a lead.
#
# ``raise_handoff`` keeps writing the ``_paw_handoffs`` Fabric object (the inbox
# reads it) and, when the visitor left an email or phone, also writes ONE Lead
# for the conversation: form_type="handoff", source.kind="handoff", the
# visitor's message and ``conversation_ref`` = "<widget_id>:<customer_ref>". A
# second handoff in the same conversation adds no second lead. No contact, or
# one that is neither an email nor a phone, writes no lead. The handoff's own
# owner notification stands; the lead's ``lead.captured`` carries
# source_kind="handoff" so the bridge doesn't notify twice.

# Fixtures are imported from a sibling suite; naming one as a test parameter is
# how pytest injects it.
# ruff: noqa: F811

from __future__ import annotations

import pytest
from pocketpaw_ee.paw_bar.handoff import PAW_HANDOFFS_TYPE, raise_handoff

from tests.cloud.test_paw_bar_escape_hatch import (  # noqa: F401 — fixtures
    _REF,
    _site,
    _widget,
    fabric,
    store,
    ws,
)

_SCRIPT = "6d4a1f2b3c8e9a0f1b2c3d4e"


async def _leads() -> list:
    from pocketpaw_ee.cloud.models.lead import Lead

    return await Lead.find_all().to_list()


async def _raise(store, widget, ws, **kw):
    return await raise_handoff(
        widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store, **kw
    )


@pytest.mark.asyncio
async def test_a_handoff_with_an_email_writes_a_lead(mongo_db, store, fabric, monkeypatch, ws):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    await _site(str(ws.id), script_name=_SCRIPT)
    widget = await store.create_widget(_widget(str(ws.id)))

    outcome = await _raise(
        store, widget, ws, question="Can someone call me about a refund?", contact="v@brewco.com"
    )
    assert outcome.ok and outcome.handoff_id

    [lead] = await _leads()
    assert lead.workspace == str(ws.id)
    assert lead.site_id == _SCRIPT
    assert lead.form_type == "handoff"
    assert lead.source.kind == "handoff"
    assert lead.source.conversation_ref == f"{widget.id}:{_REF}"
    assert lead.properties == {
        "email": "v@brewco.com",
        "message": "Can someone call me about a refund?",
    }

    # The Fabric object the inbox reads is still written.
    from pocketpaw.fabric.models import FabricQuery

    result = await fabric.query(
        FabricQuery(type_name=PAW_HANDOFFS_TYPE, filters={"widget_id": widget.id}, limit=10),
        workspace_id=str(ws.id),
    )
    assert len(result.objects) == 1


@pytest.mark.asyncio
async def test_a_phone_contact_lands_as_phone(mongo_db, store, fabric, monkeypatch, ws):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    await _site(str(ws.id), script_name=_SCRIPT)
    widget = await store.create_widget(_widget(str(ws.id)))
    await _raise(store, widget, ws, question="hi", contact="+44 20 7946 0958")
    [lead] = await _leads()
    assert lead.properties == {"phone": "+44 20 7946 0958", "message": "hi"}


@pytest.mark.asyncio
async def test_one_lead_per_conversation(mongo_db, store, fabric, monkeypatch, ws):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    await _site(str(ws.id), script_name=_SCRIPT)
    widget = await store.create_widget(_widget(str(ws.id)))
    await _raise(store, widget, ws, question="first", contact="v@brewco.com")
    await _raise(store, widget, ws, question="again", contact="v@brewco.com")
    assert len(await _leads()) == 1
    # A different conversation is its own lead.
    await raise_handoff(
        widget=widget,
        workspace_id=str(ws.id),
        customer_ref="cust-0002",
        question="me too",
        contact="w@brewco.com",
        store=store,
    )
    assert len(await _leads()) == 2


@pytest.mark.parametrize("contact", ["", "call me maybe"])
@pytest.mark.asyncio
async def test_no_usable_contact_writes_no_lead(mongo_db, store, fabric, monkeypatch, ws, contact):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    await _site(str(ws.id), script_name=_SCRIPT)
    widget = await store.create_widget(_widget(str(ws.id)))
    outcome = await _raise(store, widget, ws, question="hello", contact=contact)
    assert outcome.ok
    assert await _leads() == []


@pytest.mark.asyncio
async def test_the_handoff_lead_is_flagged_for_the_bridge(mongo_db, store, fabric, monkeypatch, ws):
    from pocketpaw_ee.cloud.shared.events import event_bus

    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    await _site(str(ws.id), script_name=_SCRIPT)
    widget = await store.create_widget(_widget(str(ws.id)))
    seen: list[dict] = []

    async def _rec(data: dict) -> None:
        seen.append(data)

    event_bus.subscribe("lead.captured", _rec)
    try:
        await _raise(store, widget, ws, question="q", contact="v@brewco.com")
    finally:
        event_bus.unsubscribe("lead.captured", _rec)
    assert [e["source_kind"] for e in seen] == ["handoff"]


@pytest.mark.asyncio
async def test_a_lead_failure_never_fails_the_handoff(mongo_db, store, fabric, monkeypatch, ws):
    from pocketpaw_ee.cloud.leads import service as leads_service

    async def _boom(**_kw):
        raise RuntimeError("mongo down")

    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    monkeypatch.setattr(leads_service, "capture_internal", _boom)
    await _site(str(ws.id), script_name=_SCRIPT)
    widget = await store.create_widget(_widget(str(ws.id)))
    outcome = await _raise(store, widget, ws, question="q", contact="v@brewco.com")
    assert outcome.ok
