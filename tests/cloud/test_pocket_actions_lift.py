# tests/cloud/test_pocket_actions_lift.py — RFC 05 M2a.
# Created: 2026-05-22 — pins the deterministic translation the spec
# normalizer applies to the pocket-authoring agent's hallucinated WRITE
# output. The agent emits a write the same way it emits a read: an inline
# `{action: "api", method: "POST", url: "/x", body: {...}}` handler instead
# of a `rippleSpec.actions` write binding triggered by `call_binding`.
# `normalize_ripple_spec` lifts a RELATIVE-url inline write into the
# `actions` block and rewrites the handler to `call_binding`.
#
# What this pins:
#   - a relative-url inline `api` POST handler -> rippleSpec.actions entry
#     + a call_binding handler.
#   - an ABSOLUTE-url `api` handler is left as `api` (third-party intent —
#     never redirected onto the credentialed backend).
#   - a GET `api` handler is left alone (not a write).
#   - a correctly-authored `actions` block passes through unchanged.
#   - a call_binding handler wired with the wrong field name is repaired.

from __future__ import annotations

from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec


def test_relative_inline_write_api_is_lifted_into_actions():
    """An inline `{action:"api", method:"POST", url:<relative>}` handler is
    lifted into `rippleSpec.actions` and the handler becomes a
    `call_binding`."""
    spec = {
        "title": "Lease Tracker",
        "version": "1.0",
        "state": {"form": {"rent": ""}},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Renew"},
                    "on_click": {
                        "action": "api",
                        "method": "POST",
                        "url": "/leases/42/renew",
                        "body": {"proposed_rent": "{state.form.rent}"},
                    },
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None

    actions = normalized.get("actions")
    assert isinstance(actions, dict)
    assert len(actions) == 1
    entry = next(iter(actions.values()))
    assert entry["kind"] == "write_binding"
    assert entry["method"] == "POST"
    assert entry["path"] == "/leases/42/renew"
    assert entry["params"] == {"proposed_rent": "{state.form.rent}"}

    # The handler is rewritten to call_binding pointing at the lifted name.
    handler = normalized["ui"]["children"][0]["on_click"]
    assert handler["action"] == "call_binding"
    assert handler["binding"] == next(iter(actions.keys()))


def test_absolute_url_inline_api_is_left_untouched():
    """An ABSOLUTE-url `api` call is a different (third-party) intent and
    must NEVER be redirected onto the pocket's credentialed backend."""
    spec = {
        "title": "Third Party",
        "version": "1.0",
        "state": {},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Notify"},
                    "on_click": {
                        "action": "api",
                        "method": "POST",
                        "url": "https://hooks.thirdparty.example/notify",
                        "body": {"msg": "hi"},
                    },
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    # No actions block created; the handler is unchanged.
    assert "actions" not in normalized
    handler = normalized["ui"]["children"][0]["on_click"]
    assert handler["action"] == "api"
    assert handler["url"] == "https://hooks.thirdparty.example/notify"


def test_get_inline_api_is_not_lifted_as_a_write():
    """A GET `api` handler is a read, not a write — not lifted into
    `actions`."""
    spec = {
        "title": "Reader",
        "version": "1.0",
        "state": {},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Fetch"},
                    "on_click": {"action": "api", "method": "GET", "url": "/leases"},
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    assert "actions" not in normalized
    assert normalized["ui"]["children"][0]["on_click"]["action"] == "api"


def test_correctly_authored_actions_block_passes_through():
    """A spec already using `rippleSpec.actions` + `call_binding` is not
    mutated."""
    good = {
        "title": "Good",
        "version": "1.0",
        "actions": {
            "mark_renewed": {
                "kind": "write_binding",
                "method": "POST",
                "path": "/leases/{item.id}/renew",
                "params": {},
                "confirm": False,
            }
        },
        "state": {},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Renew"},
                    "on_click": {"action": "call_binding", "binding": "mark_renewed"},
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(good)
    assert normalized is not None
    assert normalized["actions"] == good["actions"]
    handler = normalized["ui"]["children"][0]["on_click"]
    assert handler == {"action": "call_binding", "binding": "mark_renewed"}


def test_call_binding_wrong_field_name_is_repaired():
    """A call_binding handler that names its target with `action_id` /
    `name` is repaired to `binding`."""
    spec = {
        "title": "Misnamed",
        "version": "1.0",
        "actions": {"mark_renewed": {"kind": "write_binding", "method": "POST", "path": "/x"}},
        "state": {},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Renew"},
                    "on_click": {"action": "call_binding", "action_id": "mark_renewed"},
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    handler = normalized["ui"]["children"][0]["on_click"]
    assert handler["binding"] == "mark_renewed"
    assert "action_id" not in handler


def test_lifted_name_skips_existing_action_keys():
    """A lifted entry gets a fresh `act_N` name that does not clobber an
    already-authored action."""
    spec = {
        "title": "Mixed",
        "version": "1.0",
        "actions": {"act_1": {"kind": "write_binding", "method": "POST", "path": "/existing"}},
        "state": {},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Do"},
                    "on_click": {"action": "api", "method": "DELETE", "url": "/rows/9"},
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    actions = normalized["actions"]
    # The pre-existing act_1 survives; the lifted entry took a fresh name.
    assert actions["act_1"]["path"] == "/existing"
    assert len(actions) == 2
    lifted_name = normalized["ui"]["children"][0]["on_click"]["binding"]
    assert lifted_name != "act_1"
    assert actions[lifted_name]["method"] == "DELETE"
