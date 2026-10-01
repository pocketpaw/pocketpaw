# tests/cloud/test_paw_bar_reserved_verb.py — owners can't declare send_to_team.
#
# ``send_to_team`` is the concierge's built-in lead verb. A stored spec that
# declares it still loads (the action is dropped with a warning; see
# test_paw_bar_send_to_team.py), but every owner save path refuses it with
# 422 ``reserved_verb`` and writes nothing: the token-gated spec PATCH, the
# session-authed admin spec PATCH, and widget create.

# Fixtures are imported from a sibling suite; naming one as a test parameter is
# how pytest injects it.
# ruff: noqa: F811

from __future__ import annotations

from typing import Any

import pytest

from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    _site,
    _widget,
    client,
)

_RESERVED = {"verb": "send_to_team", "policy": "gated", "args": {"email": "str"}}
_OK = {"verb": "book_visit", "policy": "gated", "args": {"name": "str"}}


def _spec(widget_id: str, *actions: dict[str, Any]) -> dict[str, Any]:
    return {"widget_id": widget_id, "pocket_id": "pocket-1", "actions": list(actions)}


@pytest.mark.asyncio
async def test_the_token_spec_patch_refuses_it(client):
    c, store = client
    widget = await store.create_widget(_widget())
    res = await c.patch(
        f"/paw-bar/widgets/{widget.id}/spec",
        json=_spec(widget.id, _OK, _RESERVED),
        headers={"X-Paw-Bar-Token": widget.access_token},
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"] == "reserved_verb"
    assert (await store.get_widget(widget.id)).spec.actions == []
    # The same body without it saves.
    ok = await c.patch(
        f"/paw-bar/widgets/{widget.id}/spec",
        json=_spec(widget.id, _OK),
        headers={"X-Paw-Bar-Token": widget.access_token},
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_the_admin_spec_patch_refuses_it(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/widget/spec",
        json={"spec": _spec(widget.id, _RESERVED)},
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"] == "reserved_verb"
    assert (await store.get_widget(widget.id)).spec.actions == []


@pytest.mark.asyncio
async def test_widget_create_refuses_it(client):
    c, store = client
    res = await c.post(
        "/paw-bar/widgets",
        json={"pocket_id": "pocket-1", "owner": "user:maya", "spec": _spec("pending", _RESERVED)},
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"] == "reserved_verb"
    assert await store.list_widgets() == []
