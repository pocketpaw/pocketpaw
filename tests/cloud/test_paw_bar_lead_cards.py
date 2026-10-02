# tests/cloud/test_paw_bar_lead_cards.py — the lead card, server side.
#
# The v2 concierge may offer a ``send_to_team`` form (the lead card) prefilled
# from the conversation. Pinned here:
#   * ``card_spec``: a ``form`` with verb ``send_to_team`` passes only when the
#     site's ``concierge_lead_capture`` is on, needs an email or phone field and
#     takes only name / email / phone / message fields; any form field's
#     prefill ``value`` is a string of at most 500 characters. The legacy
#     ``{"kind": "form"}`` card follows the same lead rules;
#   * ``book_slot`` (in the vendored manifest for the booking work) is not
#     offered or accepted yet;
#   * the prompt: with lead capture on the frame carries the lead rule and the
#     cards paragraph teaches the lead form, even on a site with no catalog or
#     actions; off, the frame is the plain one;
#   * ``concierge_lead_capture`` on the settings GET/PATCH (default on).

# Fixtures are imported from a sibling suite; naming one as a test parameter is
# how pytest injects it.
# ruff: noqa: F811

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from pocketpaw.paw_bar.models import PawBarActionSpec
from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    _site,
    client,
)


def _form(verb: str = "send_to_team", fields: list[dict] | None = None) -> dict:
    if fields is None:
        fields = [
            {"name": "name", "label": "Name", "type": "text", "value": "Priya"},
            {"name": "email", "label": "Email", "type": "email", "value": "priya@x.com"},
            {"name": "message", "label": "Message", "type": "textarea", "value": "20 jackets"},
        ]
    return {
        "ui": {"type": "form", "props": {"verb": verb, "submit_label": "Send", "fields": fields}}
    }


def _vh(spec: dict, **kw: Any):
    from pocketpaw_ee.paw_bar.card_spec import validate_and_hydrate

    return validate_and_hydrate(spec, [], **kw)


# --------------------------------------------------------------------------- #
# card_spec
# --------------------------------------------------------------------------- #


def test_a_lead_form_passes_when_lead_capture_is_on():
    out = _vh(_form(), lead_capture=True)
    assert out is not None
    assert out["ui"]["props"]["fields"][0]["value"] == "Priya"


def test_a_lead_form_is_dropped_when_lead_capture_is_off():
    assert _vh(_form(), lead_capture=False) is None
    # The default is off: a caller that doesn't say gets no lead card.
    assert _vh(_form()) is None


def test_a_lead_form_needs_a_reply_channel():
    fields = [{"name": "name", "label": "Name", "type": "text"}]
    assert _vh(_form(fields=fields), lead_capture=True) is None
    phone = [{"name": "phone", "label": "Phone", "type": "tel"}]
    assert _vh(_form(fields=phone), lead_capture=True) is not None


def test_a_lead_form_takes_only_the_lead_fields():
    fields = [
        {"name": "email", "label": "Email", "type": "email"},
        {"name": "company", "label": "Company", "type": "text"},
    ]
    assert _vh(_form(fields=fields), lead_capture=True) is None


@pytest.mark.parametrize("value", ["x" * 501, 42, None, ["a"]])
def test_a_bad_prefill_value_drops_the_card(value):
    fields = [{"name": "email", "label": "Email", "type": "email", "value": value}]
    assert _vh(_form(fields=fields), lead_capture=True) is None
    # The rule is the form's, not the lead's: a gated form is held to it too.
    gated = [{"name": "date", "label": "Date", "type": "text", "value": value}]
    assert _vh(_form("book_visit", gated)) is None


def test_a_prefill_at_the_cap_passes():
    fields = [{"name": "email", "label": "Email", "type": "email", "value": "x" * 500}]
    assert _vh(_form(fields=fields), lead_capture=True) is not None
    gated = [{"name": "date", "label": "Date", "type": "text", "value": "next Tuesday"}]
    assert _vh(_form("book_visit", gated)) is not None


def test_the_legacy_form_card_follows_the_lead_rules():
    from pocketpaw_ee.paw_bar.card_spec import render_card

    body = json.dumps(
        {
            "kind": "form",
            "verb": "send_to_team",
            "fields": [{"name": "email", "label": "Email", "type": "email", "value": "a@b.co"}],
        }
    )
    assert render_card(body, [], lead_capture=False) is None
    assert render_card(body, [], lead_capture=True) is not None
    no_contact = json.dumps(
        {"kind": "form", "verb": "send_to_team", "fields": [{"name": "name", "type": "text"}]}
    )
    assert render_card(no_contact, [], lead_capture=True) is None
    # A legacy gated form still passes through untouched.
    legacy = json.dumps({"kind": "form", "verb": "book_visit", "fields": []})
    assert render_card(legacy, []) is not None


def test_book_slot_is_not_offered_yet():
    from pocketpaw_ee.paw_bar import card_spec

    assert "book_slot" in {w["type"] for w in card_spec.MANIFEST["widgets"]}
    assert "book_slot" not in card_spec.WIDGET_TYPES
    assert "book_slot" not in card_spec.compact_manifest()
    spec = {"ui": {"type": "book_slot", "props": {"session_type": "a", "slot_ids": ["s1"]}}}
    assert _vh(spec, lead_capture=True) is None


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

_RULE = (
    "When the visitor shares contact details or asks to be contacted, offer a "
    "send_to_team form prefilled with what they said. Never claim it was sent."
)


def _site_ns(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(concierge_allow_doc_code=False, **kw)


def test_the_frame_carries_the_lead_rule_only_when_on():
    from pocketpaw_ee.paw_bar import concierge_runtime as rt

    assert _RULE in rt.frame_for(_site_ns(concierge_lead_capture=True))
    assert rt.frame_for(_site_ns(concierge_lead_capture=False)) == rt.FRAME
    assert _RULE not in rt.FRAME
    doc = rt.frame_for(SimpleNamespace(concierge_allow_doc_code=True, concierge_lead_capture=True))
    assert _RULE in doc and "quote it verbatim" in doc
    # A Site-like object without the field reads as the model default: on.
    assert _RULE in rt.frame_for(SimpleNamespace(concierge_allow_doc_code=False))


def _bare_widget(actions: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(id="w1", spec=SimpleNamespace(actions=actions or []))


def test_the_cards_paragraph_teaches_the_lead_form_on_a_bare_site():
    from pocketpaw_ee.paw_bar.concierge_runtime import build_prompt

    on = build_prompt([], _bare_widget(), [], "hi", site=_site_ns(concierge_lead_capture=True))
    assert "send_to_team" in on
    assert "pawbar-card" in on
    off = build_prompt([], _bare_widget(), [], "hi", site=_site_ns(concierge_lead_capture=False))
    assert "pawbar-card" not in off


def test_the_cards_paragraph_says_when_lead_cards_are_off():
    from pocketpaw_ee.paw_bar.concierge_runtime import build_prompt

    gated = [PawBarActionSpec(verb="book_visit", policy="gated", args={"name": "str"})]
    off = build_prompt(
        [], _bare_widget(gated), [], "hi", site=_site_ns(concierge_lead_capture=False)
    )
    assert "Do not offer a send_to_team form" in off


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_expose_lead_capture_default_on(client):
    c, _store = client
    site = await _site()
    res = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert res.status_code == 200
    assert res.json()["concierge_lead_capture"] is True


@pytest.mark.asyncio
async def test_settings_patch_turns_lead_capture_off(client):
    c, _store = client
    site = await _site()
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings", json={"concierge_lead_capture": False}
    )
    assert res.status_code == 200, res.text
    assert res.json()["concierge_lead_capture"] is False
    got = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert got.json()["concierge_lead_capture"] is False
