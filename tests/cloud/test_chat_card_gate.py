# tests/cloud/test_chat_card_gate.py: Paw OS chat cards go through the Paw Bar
# card gate on CHAT_PROFILE (run_core._gate_chat_card / _repair_fence_json).
#
# A card the gate passes ships with its repairs (flat props lifted, aliases
# renamed, a broken fence's JSON repaired); a refused card ships unchanged with a
# ripple_chat_card.refused line; a gate error falls back to the normalized card.
# The ripple and pawbar profiles keep their verdicts after the chat-profile edits.

from __future__ import annotations

import copy
import json
import logging
from unittest.mock import patch

from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.paw_bar import card_spec
from pocketpaw_ee.paw_bar.card_spec import (
    HOST_EVENTS,
    PAWBAR_PROFILE,
    RIPPLE_PROFILE,
    rejection_reason,
    render_card,
)
from pocketpaw_ee.paw_bar.chat_profile import CHAT_PROFILE

LOGGER = "pocketpaw_ee.cloud.chat.runs.run_core"


def _fence(spec: dict | str) -> str:
    body = spec if isinstance(spec, str) else json.dumps(spec)
    return f"Here you go:\n\n```ui-spec\n{body}\n```\n\nAnything else?"


def _button(action: dict) -> dict:
    return {
        "version": "1.0",
        "ui": {"type": "button", "props": {"label": "Go"}, "on_click": action},
    }


def _logged(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == name]


# --------------------------------------------------------------------------- #
# CHAT_PROFILE verdicts
# --------------------------------------------------------------------------- #


def test_chat_profile_accepts_emit_chat_send():
    body = json.dumps(_button({"action": "emit", "target": "chat.send", "value": {"text": "more"}}))
    assert render_card(body, None, verbs=CHAT_PROFILE.host_events, profile=CHAT_PROFILE)


def test_chat_profile_refuses_paw_bar_checkout_emit():
    body = json.dumps(_button({"action": "emit", "target": "checkout"}))
    verbs = CHAT_PROFILE.host_events
    assert render_card(body, None, verbs=verbs, profile=CHAT_PROFILE) is None
    assert "emit names a host event" in rejection_reason(body, verbs=verbs, profile=CHAT_PROFILE)


def test_chat_profile_runs_navigate_and_api():
    for action in (
        {"action": "navigate", "url": "/pockets/abc"},
        {"action": "api", "url": "/api/v1/pockets", "method": "POST"},
    ):
        body = json.dumps(_button(action))
        assert render_card(body, None, verbs=CHAT_PROFILE.host_events, profile=CHAT_PROFILE)


# --------------------------------------------------------------------------- #
# The chat path: gate applied, refusal and error fall back
# --------------------------------------------------------------------------- #


def test_flat_props_card_ships_repaired(caplog):
    flat = {
        "version": "1.0",
        "ui": {"type": "card", "title": "Hi", "children": [{"type": "text", "text": "x"}]},
    }
    caplog.set_level(logging.INFO, logger=LOGGER)

    remaining, spec = run_core._extract_ripple_attachment(_fence(flat))

    assert spec["version"] == "1.0"
    assert spec["ui"]["props"]["title"] == "Hi"
    assert "title" not in spec["ui"]
    assert spec["ui"]["children"][0]["props"]["text"] == "x"
    assert _logged(caplog, "ripple_chat_card.repaired")
    assert "```ui-spec" not in remaining and "Anything else?" in remaining


def test_alias_card_ships_renamed():
    card = {"version": "1.0", "ui": {"type": "pomodoro", "props": {}}}

    _, spec = run_core._extract_ripple_attachment(_fence(card))

    assert spec["ui"]["type"] == "focus-timer"


def test_broken_fence_json_is_repaired_and_gated(caplog):
    # One closing brace short: json.loads refuses it, the gate's repair fixes it.
    body = '{"version": "1.0", "ui": {"type": "card", "title": "Hi", "children": []}'
    caplog.set_level(logging.INFO, logger=LOGGER)

    _, spec = run_core._extract_ripple_attachment(_fence(body))

    assert spec is not None
    assert spec["ui"]["props"]["title"] == "Hi"
    assert any(
        getattr(r, "json_repaired", False) for r in _logged(caplog, "ripple_chat_card.repaired")
    )


def test_refused_card_ships_unchanged_and_logs(caplog):
    card = _button({"action": "emit", "target": "checkout"})
    caplog.set_level(logging.INFO, logger=LOGGER)

    _, spec = run_core._extract_ripple_attachment(_fence(card))

    assert spec["ui"]["on_click"] == card["ui"]["on_click"]
    refused = _logged(caplog, "ripple_chat_card.refused")
    assert refused and "emit names a host event" in refused[0].reason


def test_gate_error_falls_back_to_normalized_card(caplog):
    card = {"version": "1.0", "ui": {"type": "card", "title": "Hi", "children": []}}
    with patch.object(card_spec, "render_card", side_effect=RuntimeError("boom")):
        _, spec = run_core._extract_ripple_attachment(_fence(card))

    # The normalized card ships: its flat title was not lifted.
    assert spec["ui"]["title"] == "Hi"
    assert "title" not in (spec["ui"].get("props") or {})
    assert _logged(caplog, "ripple_chat_card.gate_failed")


def test_comparison_prices_survive_the_gate():
    # render_card's store fill would strip these with no store; chat never ships it.
    items = [
        {"id": "a", "name": "Aero 14", "price": 999},
        {"id": "b", "name": "Nimbus Pro 15", "price": 1299},
    ]
    card = {"version": "1.0", "ui": {"type": "comparison-layout", "props": {"items": items}}}

    _, spec = run_core._extract_ripple_attachment(_fence(card))

    assert spec["ui"]["props"]["items"] == items


def test_packing_list_each_in_props_runs_end_to_end(caplog):
    """Today's live card: ``each`` with ``props.items``. The normalizer (#2691)
    lifts the loop fields; the gate then passes the card and leaves them be."""
    card = {
        "version": "1.0",
        "state": {"clothes": [{"name": "Socks", "packed": False}]},
        "ui": {
            "type": "card",
            "props": {"title": "Packing"},
            "children": [
                {
                    "type": "flex",
                    "props": {"direction": "column"},
                    "children": [
                        {"type": "text", "props": {"text": "Packed 0 of 30"}},
                        {
                            "type": "each",
                            "props": {"items": "{state.clothes}"},
                            "children": [
                                {
                                    "type": "checkbox",
                                    "props": {"label": "{item.name}"},
                                    "bind": "clothes.{index}.packed",
                                }
                            ],
                        },
                    ],
                }
            ],
        },
    }
    caplog.set_level(logging.INFO, logger=LOGGER)

    _, spec = run_core._extract_ripple_attachment(_fence(copy.deepcopy(card)))

    each = spec["ui"]["children"][0]["children"][1]
    assert each["items"] == "{state.clothes}"
    assert "items" not in (each.get("props") or {})
    assert each["children"][0]["bind"] == "clothes.{index}.packed"
    assert spec["state"] == card["state"]
    assert not _logged(caplog, "ripple_chat_card.refused")


# --------------------------------------------------------------------------- #
# Ripple and pawbar keep their verdicts after the chat-profile edits
# --------------------------------------------------------------------------- #


def test_card_verbs_unchanged_for_ripple_and_pawbar():
    for profile in (None, PAWBAR_PROFILE, RIPPLE_PROFILE):
        assert card_spec._card_verbs(HOST_EVENTS, profile) == list(HOST_EVENTS)
        assert card_spec._card_verbs((), profile) == []
    # ripple's server-wired "book" is never model-emittable, even when declared.
    assert card_spec._card_verbs((*HOST_EVENTS, "book"), RIPPLE_PROFILE) == list(HOST_EVENTS)
    # chat.send is chat's own; other profiles never allow it.
    assert card_spec._card_verbs(("chat.send",), RIPPLE_PROFILE) == []
    assert card_spec._card_verbs(("chat.send",), CHAT_PROFILE) == ["chat.send"]


def test_ripple_and_pawbar_verdicts_and_lifts_unchanged():
    send = json.dumps(_button({"action": "emit", "target": "chat.send"}))
    checkout = json.dumps(_button({"action": "emit", "target": "checkout"}))
    flat = json.dumps({"ui": {"type": "card", "title": "Hi", "children": []}})
    for profile in (RIPPLE_PROFILE, PAWBAR_PROFILE):
        assert render_card(send, None, profile=profile) is None
        assert render_card(send, None, verbs=("chat.send",), profile=profile) is None
    assert render_card(checkout, None, profile=RIPPLE_PROFILE) is not None
    lifted = render_card(flat, None, profile=RIPPLE_PROFILE)
    assert '"props":{"title":"Hi"}' in lifted
    # A profile on another manifest (pawbar) still reads its own lift table only.
    assert card_spec._LIFTABLE_BY_PROFILE[PAWBAR_PROFILE.name] is not card_spec._LIFTABLE
