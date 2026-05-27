# tests/test_ripple_manifest_action_wiring.py
# 2026-05-23 — Cover the action-verb allowlist and the unwired-live-button
# detector. Companion to the catalog gate: catches a class of LLM
# "loophole" failures the prompt-side rule in PR #1194 didn't catch:
#   - ``action: "fetch"`` and other invented verbs the dispatcher
#     ``console.warn``s and silently no-ops on.
#   - Refresh-labelled buttons whose on_click is empty / inert /
#     pointing at an undeclared source.
# Pure-walker tests; the EE strict + logged wiring lives in
# ``tests/cloud/test_ripple_validator_action_wiring.py``.

from __future__ import annotations

import pytest

from pocketpaw.ripple.manifest import (
    _KNOWN_ACTION_VERBS,
    find_unwired_live_buttons,
    validate_action_verbs,
)

# ---------------------------------------------------------------------------
# Action-verb allowlist
# ---------------------------------------------------------------------------


class TestValidateActionVerbs:
    def test_known_verb_passes(self):
        spec = {
            "ui": {
                "type": "button",
                "props": {"on_click": {"action": "set", "target": "x", "value": 1}},
            }
        }
        assert validate_action_verbs(spec) == []

    def test_unknown_verb_flagged_with_suggestion(self):
        spec = {
            "ui": {
                "type": "button",
                "props": {
                    "on_click": {
                        "action": "fetch",
                        "endpoint": "/pet/1",
                        "target": "pet_rows",
                    }
                },
            }
        }
        issues = validate_action_verbs(spec)
        assert len(issues) == 1
        assert issues[0]["action"] == "fetch"
        assert issues[0]["prop"] == "on_click"
        assert "props.on_click" in issues[0]["path"]
        # difflib should suggest something close-ish from the known set;
        # we don't pin the exact suggestion (depends on close-matches
        # cutoff) but assert it surfaces *some* valid verb when one
        # exists.
        if issues[0]["suggestion"] is not None:
            assert issues[0]["suggestion"] in _KNOWN_ACTION_VERBS

    def test_missing_action_field_flagged(self):
        # A handler dict with no `action` field is functionally
        # identical to an unknown action — the dispatcher's switch
        # falls through to the no-op default.
        spec = {
            "ui": {
                "type": "button",
                "props": {"on_click": {"target": "x"}},
            }
        }
        issues = validate_action_verbs(spec)
        assert len(issues) == 1
        assert issues[0]["action"] == "<missing>"

    def test_action_chain_each_handler_checked(self):
        # on_click can be a list of handlers (action chain). Every
        # handler in the chain is independently validated.
        spec = {
            "ui": {
                "type": "button",
                "props": {
                    "on_click": [
                        {"action": "set", "target": "draft", "value": ""},
                        {"action": "frobnicate", "target": "x"},
                        {"action": "toast", "message": "ok"},
                    ]
                },
            }
        }
        issues = validate_action_verbs(spec)
        assert len(issues) == 1
        assert issues[0]["action"] == "frobnicate"

    def test_walks_nested_children(self):
        spec = {
            "ui": {
                "type": "flex",
                "children": [
                    {
                        "type": "flex",
                        "children": [
                            {
                                "type": "button",
                                "props": {"on_click": {"action": "bogus"}},
                            }
                        ],
                    }
                ],
            }
        }
        issues = validate_action_verbs(spec)
        assert len(issues) == 1
        assert "ui.children[0].children[0]" in issues[0]["path"]

    def test_else_children_walked(self):
        spec = {
            "ui": {
                "type": "if",
                "condition": "{state.x}",
                "children": [{"type": "text", "props": {"text": "ok"}}],
                "else_children": [
                    {
                        "type": "button",
                        "props": {"on_click": {"action": "nope"}},
                    }
                ],
            }
        }
        issues = validate_action_verbs(spec)
        assert len(issues) == 1
        assert "else_children" in issues[0]["path"]

    def test_event_prop_aliases_handled(self):
        # Both snake_case and camelCase event-prop names are scanned.
        spec = {
            "ui": {
                "type": "input",
                "props": {"onChange": {"action": "set", "target": "v", "value": "{$event}"}},
            }
        }
        assert validate_action_verbs(spec) == []
        spec2 = {
            "ui": {
                "type": "input",
                "props": {"onChange": {"action": "bogus"}},
            }
        }
        assert len(validate_action_verbs(spec2)) == 1

    def test_non_dict_spec_returns_empty(self):
        assert validate_action_verbs(None) == []
        assert validate_action_verbs("string") == []
        assert validate_action_verbs([1, 2, 3]) == []

    def test_all_known_verbs_pass(self):
        # Spot-check the full known set — if a verb is removed from
        # the dispatcher without updating this list, this test catches
        # the drift the next time someone runs it locally.
        for verb in _KNOWN_ACTION_VERBS:
            spec = {
                "ui": {
                    "type": "button",
                    "props": {"on_click": {"action": verb}},
                }
            }
            assert validate_action_verbs(spec) == [], f"verb {verb!r} flagged unexpectedly"


# ---------------------------------------------------------------------------
# Unwired live buttons
# ---------------------------------------------------------------------------


class TestFindUnwiredLiveButtons:
    def test_refresh_button_with_run_source_passes(self):
        spec = {
            "sources": {"pets": {"method": "GET", "path": "/pets", "bind": "state.pets"}},
            "ui": {
                "type": "button",
                "props": {
                    "label": "Refresh",
                    "on_click": {"action": "run_source", "source": "pets"},
                },
            },
        }
        assert find_unwired_live_buttons(spec) == []

    def test_refresh_button_with_api_passes(self):
        spec = {
            "ui": {
                "type": "button",
                "props": {
                    "label": "Refresh pets",
                    "on_click": {"action": "api", "url": "/pets", "target": "pets"},
                },
            }
        }
        assert find_unwired_live_buttons(spec) == []

    def test_refresh_button_with_empty_on_click_flagged(self):
        spec = {
            "ui": {
                "type": "button",
                "props": {"label": "Refresh"},
            }
        }
        issues = find_unwired_live_buttons(spec)
        assert len(issues) == 1
        assert "no on_click" in issues[0]["reason"]

    def test_refresh_button_with_invented_verb_flagged(self):
        # Captain's test-D failure shape — the agent invented
        # ``action: "fetch"``. Catch is two-layered: the verb check
        # already flags it, but the live-button walk catches the
        # semantic too.
        spec = {
            "ui": {
                "type": "button",
                "props": {
                    "label": "Refresh",
                    "on_click": {
                        "action": "fetch",
                        "endpoint": "/pet/1",
                        "target": "pet_rows",
                    },
                },
            }
        }
        issues = find_unwired_live_buttons(spec)
        assert len(issues) == 1
        assert "no fetching action" in issues[0]["reason"]

    def test_refresh_button_run_source_with_missing_key_flagged(self):
        spec = {
            "sources": {},
            "ui": {
                "type": "button",
                "props": {
                    "label": "Refresh",
                    "on_click": {"action": "run_source", "source": "ghost"},
                },
            },
        }
        issues = find_unwired_live_buttons(spec)
        assert len(issues) == 1
        assert "'ghost'" in issues[0]["reason"]

    def test_refresh_button_api_without_url_flagged(self):
        spec = {
            "ui": {
                "type": "button",
                "props": {
                    "label": "Refresh",
                    "on_click": {"action": "api", "target": "x"},
                },
            }
        }
        issues = find_unwired_live_buttons(spec)
        assert len(issues) == 1
        assert "no url" in issues[0]["reason"]

    def test_non_live_button_is_not_flagged(self):
        # A button labelled "Save draft" doesn't promise live behaviour
        # — its on_click can be a plain set. Don't false-positive on
        # those.
        spec = {
            "ui": {
                "type": "button",
                "props": {
                    "label": "Save draft",
                    "on_click": {"action": "set", "target": "draft.saved", "value": True},
                },
            }
        }
        assert find_unwired_live_buttons(spec) == []

    def test_aria_label_also_matched(self):
        # Sometimes the visible label is an icon and the live-claim is
        # in aria-label only.
        spec = {
            "ui": {
                "type": "button",
                "props": {
                    "aria_label": "Refresh data",
                    "on_click": {"action": "frobnicate"},
                },
            }
        }
        assert len(find_unwired_live_buttons(spec)) == 1

    def test_non_button_with_refresh_label_ignored(self):
        # The walk only inspects ``type: "button"`` nodes. A heading
        # that says "Refresh" is not a button and not in scope.
        spec = {"ui": {"type": "heading", "props": {"text": "Refresh history"}}}
        assert find_unwired_live_buttons(spec) == []

    def test_non_dict_spec_returns_empty(self):
        assert find_unwired_live_buttons(None) == []
        assert find_unwired_live_buttons("string") == []


# Silence ruff unused-import nudge.
_ = pytest
