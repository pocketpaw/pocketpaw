# tests/cloud/test_pocket_sources_lift.py — RFC 04 alpha.
# Created: 2026-05-22 (PR #1177) — pins the deterministic translation that
# the spec normalizer applies to the pocket-authoring agent's hallucinated
# data-source output. The agent reliably emits a `rippleSpec.tool_specs`
# REST schema (invented `kind`/`url`/`auto_fetch`/`into` fields) instead of
# the RFC 04 `rippleSpec.sources` block, and wires refresh buttons with the
# wrong `source_id` field. `normalize_ripple_spec` now lifts that output
# into a working `sources` block and repairs the buttons.
#
# What this pins:
#   - tool_specs REST entry -> rippleSpec.sources entry, button repaired.
#   - a correctly-authored `sources` spec passes through UNCHANGED.
#   - auto_fetch:false -> refresh ["manual"].
#   - an absolute `url` is reduced to a relative `path`.
#   - a non-GET method entry is skipped (alpha is GET-only).

from __future__ import annotations

from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec


def _hallucinated_spec() -> dict:
    """The exact bad shape the authoring agent emits, from MongoDB ground
    truth: a `tool_specs` REST list nested in the rippleSpec plus a refresh
    button wired with `source_id` instead of `source`."""
    return {
        "title": "Todo Tracker",
        "version": "1.0",
        "tool_specs": [
            {
                "id": "src_todos",
                "kind": "rest",
                "method": "GET",
                "url": "/todos",
                "auto_fetch": True,
                "into": "todos",
            }
        ],
        "state": {"todos": []},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Refresh"},
                    "on_click": {"action": "run_source", "source_id": "src_todos"},
                }
            ],
        },
    }


def test_tool_specs_rest_entry_is_lifted_into_sources():
    """The hallucinated tool_specs REST entry becomes a sources entry."""
    normalized = normalize_ripple_spec(_hallucinated_spec())
    assert normalized is not None

    sources = normalized.get("sources")
    assert sources == {
        "src_todos": {
            "method": "GET",
            "path": "/todos",
            "bind": "state.todos",
            "refresh": ["pocket_open", "manual"],
        }
    }


def test_lifted_tool_specs_key_is_removed():
    """`rippleSpec.tool_specs` is not a real field — drop it once empty."""
    normalized = normalize_ripple_spec(_hallucinated_spec())
    assert normalized is not None
    assert "tool_specs" not in normalized


def test_run_source_button_handler_is_repaired():
    """`source_id` on a run_source handler is renamed to `source`."""
    normalized = normalize_ripple_spec(_hallucinated_spec())
    assert normalized is not None

    button = normalized["ui"]["children"][0]
    handler = button["on_click"]
    assert handler["action"] == "run_source"
    assert handler["source"] == "src_todos"
    assert "source_id" not in handler


def test_correctly_authored_sources_spec_passes_through_unchanged():
    """A spec already using rippleSpec.sources must not be mutated."""
    good = {
        "title": "PR Tracker",
        "version": "1.0",
        "sources": {
            "prs": {
                "method": "GET",
                "path": "/pulls?state=open",
                "bind": "state.prs",
                "refresh": ["pocket_open", "manual"],
            }
        },
        "state": {"prs": []},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Refresh"},
                    "on_click": {"action": "run_source", "source": "prs"},
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(good)
    assert normalized is not None
    assert normalized["sources"] == good["sources"]
    assert "tool_specs" not in normalized
    handler = normalized["ui"]["children"][0]["on_click"]
    assert handler == {"action": "run_source", "source": "prs"}


def test_auto_fetch_false_gives_manual_only_refresh():
    """auto_fetch:false -> refresh ["manual"] (no pocket_open trigger)."""
    spec = {
        "title": "Manual Todo",
        "version": "1.0",
        "tool_specs": [
            {
                "id": "src_todos",
                "kind": "rest",
                "method": "GET",
                "url": "/todos",
                "auto_fetch": False,
                "into": "todos",
            }
        ],
        "state": {"todos": []},
        "ui": {"type": "flex", "children": []},
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    assert normalized["sources"]["src_todos"]["refresh"] == ["manual"]


def test_absolute_url_is_reduced_to_relative_path():
    """An absolute URL in `url` keeps only its path+query portion."""
    spec = {
        "title": "Absolute URL",
        "version": "1.0",
        "tool_specs": [
            {
                "id": "src_todos",
                "kind": "rest",
                "method": "GET",
                "url": "https://api.example.com/todos?state=open",
                "auto_fetch": True,
                "into": "todos",
            }
        ],
        "state": {"todos": []},
        "ui": {"type": "flex", "children": []},
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    assert normalized["sources"]["src_todos"]["path"] == "/todos?state=open"


def test_non_get_method_entry_is_skipped():
    """Alpha is GET-only — a non-GET tool_specs entry is dropped, not lifted."""
    spec = {
        "title": "Write Source",
        "version": "1.0",
        "tool_specs": [
            {
                "id": "src_create",
                "kind": "rest",
                "method": "POST",
                "url": "/todos",
                "auto_fetch": True,
                "into": "todos",
            }
        ],
        "state": {"todos": []},
        "ui": {"type": "flex", "children": []},
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    assert "sources" not in normalized or "src_create" not in normalized.get("sources", {})
    assert "tool_specs" not in normalized


def test_lift_heuristic_without_kind_field():
    """An entry with url+into but no `kind:"rest"` still looks like a REST
    data source and is lifted."""
    spec = {
        "title": "No Kind",
        "version": "1.0",
        "tool_specs": [{"id": "src_todos", "method": "GET", "url": "/todos", "into": "todos"}],
        "state": {"todos": []},
        "ui": {"type": "flex", "children": []},
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    assert "src_todos" in normalized["sources"]


def test_run_source_button_bound_to_sole_source_when_source_missing():
    """A run_source handler with no source at all binds to the only source."""
    spec = {
        "title": "Sole Source",
        "version": "1.0",
        "sources": {
            "todos": {
                "method": "GET",
                "path": "/todos",
                "bind": "state.todos",
                "refresh": ["manual"],
            }
        },
        "state": {"todos": []},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Refresh"},
                    "on_click": {"action": "run_source"},
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    handler = normalized["ui"]["children"][0]["on_click"]
    assert handler["source"] == "todos"


def test_handler_arrays_are_walked():
    """A run_source handler inside an on_click array is repaired too."""
    spec = {
        "title": "Array Handler",
        "version": "1.0",
        "tool_specs": [
            {
                "id": "src_todos",
                "kind": "rest",
                "method": "GET",
                "url": "/todos",
                "auto_fetch": True,
                "into": "todos",
            }
        ],
        "state": {"todos": []},
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {"label": "Refresh"},
                    "on_click": [
                        {"action": "run_source", "source_id": "src_todos"},
                        {"action": "set_state", "path": "todos", "value": []},
                    ],
                }
            ],
        },
    }
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    handlers = normalized["ui"]["children"][0]["on_click"]
    assert handlers[0] == {"action": "run_source", "source": "src_todos"}
    assert handlers[1] == {"action": "set_state", "path": "todos", "value": []}
