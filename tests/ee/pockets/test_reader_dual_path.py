# tests/ee/pockets/test_reader_dual_path.py
# Created: 2026-06-19 (feat/typed-ripplespec-phase2) — TDD coverage for the
# Phase 2 executor/reader dual-path + domain-boundary promotion. Phase 1
# (#1503) shipped the typed RippleSpec model and used it INTERNALLY in
# service.update + reconcile; Phase 2 makes the reader layer accept
# ``RippleSpec | dict`` and promotes dict -> RippleSpec at the domain
# boundary (``_pocket_to_domain``).
#
# THE MIGRATION INVARIANT under test (non-negotiable — the hot read path):
#   * Dual-path / back-compat: EVERY touched reader works given EITHER a raw
#     legacy dict OR a typed RippleSpec. A reader that breaks on a legacy dict
#     is a FAILURE.
#   * No migration: ``to_flat_dict`` is byte-equivalent; nothing rewrites
#     stored documents — promotion happens on read only.
#   * No data loss: unknown/passthrough keys survive a read->write round-trip
#     (RippleSpec has ``extra="allow"``).
#   * A corrupt/unpromotable spec must NOT break a reader (falls back to the
#     dict path).
#
# These tests are unit-level (no Mongo): they call each reader directly with
# both shapes. The route-level suites (test_sources_run_endpoint,
# test_template_reconcile_route, ...) remain the integration gate.
"""Phase-2 dual-path reader tests for the typed RippleSpec."""

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw.bundled_templates import RippleSpec  # noqa: E402

# A realistic seeded-shape rippleSpec: a UISpec tree + instance state +
# read sources + write actions + a passthrough key the readers never touch
# but which MUST survive a round-trip.
_SEEDED_FLAT: dict = {
    "ui": {
        "type": "stack",
        "id": "n_root",
        "children": [{"type": "text", "id": "n_t", "props": {"text": "{state.title}"}}],
    },
    "state": {"title": "Hello", "rows": [{"id": "r1"}, {"id": "r2"}]},
    "selections": {"row": "r1"},
    "sources": {
        "feed": {"method": "GET", "path": "/feed", "bind": "state.rows", "refresh": ["pocket_open"]}
    },
    "actions": {
        "save": {
            "kind": "write_binding",
            "method": "POST",
            "path": "/save",
            "params": {},
            "instinct_exempt": True,
            "requires_instinct": False,
        }
    },
    # Passthrough / unknown keys — must round-trip untouched.
    "kb_scope": "pocket",
    "schema_version": "2",
    "triggers": [{"type": "manual"}],
    "some_future_key": {"nested": [1, 2, 3]},
}


def _typed() -> RippleSpec:
    """The seeded spec promoted to a typed RippleSpec."""
    spec = RippleSpec.from_flat_dict(_SEEDED_FLAT)
    assert spec is not None
    return spec


def _strip_ids(node):
    """Drop minted ``id`` keys recursively.

    ``normalize_ripple_spec`` re-mints a random ``n_<hex>`` id on every node on
    every call (pre-existing ``ensure_ids`` behavior, orthogonal to Phase 2).
    Comparing two separate normalize calls therefore needs the ids stripped —
    the dual-path contract is about the STRUCTURE being identical, not the
    randomized ids.
    """
    if isinstance(node, dict):
        return {k: _strip_ids(v) for k, v in node.items() if k != "id"}
    if isinstance(node, list):
        return [_strip_ids(c) for c in node]
    return node


# ---------------------------------------------------------------------------
# source_executor._parse_bindings — dual-path
# ---------------------------------------------------------------------------


def test_parse_bindings_legacy_dict() -> None:
    from pocketpaw_ee.cloud.pockets.source_executor import _parse_bindings

    bindings, errors = _parse_bindings(_SEEDED_FLAT)
    assert errors == []
    assert "feed" in bindings
    assert bindings["feed"].path == "/feed"


def test_parse_bindings_typed_spec() -> None:
    from pocketpaw_ee.cloud.pockets.source_executor import _parse_bindings

    bindings, errors = _parse_bindings(_typed())
    assert errors == []
    assert "feed" in bindings
    assert bindings["feed"].path == "/feed"


def test_parse_bindings_none_and_empty() -> None:
    from pocketpaw_ee.cloud.pockets.source_executor import _parse_bindings

    assert _parse_bindings(None) == ({}, [])
    assert _parse_bindings(RippleSpec()) == ({}, [])


def test_selected_source_keys_dual_path() -> None:
    from pocketpaw_ee.cloud.pockets.source_executor import selected_source_keys

    assert selected_source_keys(_SEEDED_FLAT, trigger="pocket_open") == ["feed"]
    assert selected_source_keys(_typed(), trigger="pocket_open") == ["feed"]


# ---------------------------------------------------------------------------
# bulk_dispatch — actions read via from_flat_dict
# ---------------------------------------------------------------------------


def test_bulk_dispatch_reads_actions_from_legacy_dict() -> None:
    # The helper extracts the named action binding off the (legacy) wire dict.
    from pocketpaw_ee.cloud.pockets.bulk_dispatch import _action_binding_for

    raw = _action_binding_for(_SEEDED_FLAT, "save")
    assert isinstance(raw, dict)
    assert raw["method"] == "POST"


def test_bulk_dispatch_reads_actions_from_typed_spec() -> None:
    from pocketpaw_ee.cloud.pockets.bulk_dispatch import _action_binding_for

    raw = _action_binding_for(_typed(), "save")
    assert isinstance(raw, dict)
    assert raw["method"] == "POST"


def test_bulk_dispatch_missing_action_returns_none() -> None:
    from pocketpaw_ee.cloud.pockets.bulk_dispatch import _action_binding_for

    assert _action_binding_for(_SEEDED_FLAT, "nope") is None
    assert _action_binding_for(_typed(), "nope") is None
    assert _action_binding_for(None, "save") is None
    assert _action_binding_for("corrupt", "save") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ripple_normalizer.normalize_ripple_spec — dual-path; returns the flat wire
# dict in BOTH cases (the wire stays Record<string, any>) and preserves the
# passthrough/unknown keys.
# ---------------------------------------------------------------------------


def test_normalize_legacy_dict_unchanged_wire_shape() -> None:
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    out = normalize_ripple_spec(_SEEDED_FLAT)
    assert isinstance(out, dict)
    # State + passthrough survive.
    assert out["state"]["rows"] == [{"id": "r1"}, {"id": "r2"}]
    assert out["kb_scope"] == "pocket"
    assert out["some_future_key"] == {"nested": [1, 2, 3]}


def test_normalize_typed_spec_returns_equivalent_wire_dict() -> None:
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    from_dict = normalize_ripple_spec(_SEEDED_FLAT)
    from_typed = normalize_ripple_spec(_typed())
    assert isinstance(from_typed, dict)
    # Same logical wire shape regardless of input type (byte-equivalent
    # invariant: a promoted spec normalizes to the same stored document).
    # Node ids are randomized per-call, so compare structure id-stripped.
    assert _strip_ids(from_typed["ui"]) == _strip_ids(from_dict["ui"])
    assert from_typed["state"] == from_dict["state"]
    assert from_typed["kb_scope"] == "pocket"
    assert from_typed["some_future_key"] == {"nested": [1, 2, 3]}


def test_normalize_none_returns_none() -> None:
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    assert normalize_ripple_spec(None) is None


# ---------------------------------------------------------------------------
# ripple_validator — dual-path on every public spec reader.
# ---------------------------------------------------------------------------


def test_validate_ripple_spec_dual_path() -> None:
    from pocketpaw_ee.cloud.ripple_validator import validate_ripple_spec

    # A clean spec yields no warnings either way.
    assert validate_ripple_spec(_SEEDED_FLAT) == []
    assert validate_ripple_spec(_typed()) == []


def test_validate_ripple_spec_flags_bad_expression_dual_path() -> None:
    from pocketpaw_ee.cloud.ripple_validator import validate_ripple_spec

    bad_flat = {
        "ui": {"type": "text", "id": "n", "props": {"text": "{state.x.map(=> y)}"}},
        "state": {},
    }
    dict_warnings = validate_ripple_spec(bad_flat)
    typed_warnings = validate_ripple_spec(RippleSpec.from_flat_dict(bad_flat))
    assert dict_warnings, "expected a grammar warning on the dict path"
    # The typed path must surface the SAME finding (same ui tree walk).
    assert {w.code for w in typed_warnings} == {w.code for w in dict_warnings}


def test_find_unreferenced_state_keys_dual_path() -> None:
    from pocketpaw_ee.cloud.ripple_validator import find_unreferenced_state_keys

    flat = {
        "ui": {"type": "text", "id": "n", "props": {"text": "{state.title}"}},
        "state": {"title": "x", "orphan": "y"},
    }
    assert find_unreferenced_state_keys(flat) == ["orphan"]
    assert find_unreferenced_state_keys(RippleSpec.from_flat_dict(flat)) == ["orphan"]


# ---------------------------------------------------------------------------
# Domain-boundary promotion — _pocket_to_domain promotes dict -> RippleSpec,
# but a corrupt spec falls back to the raw value and never breaks the load.
# ---------------------------------------------------------------------------


class _FakeDoc:
    """Minimal stand-in for the Beanie Pocket doc the converter reads."""

    def __init__(self, ripple_spec):
        self.id = "pid"
        self.workspace = "ws"
        self.name = "n"
        self.description = "d"
        self.type = "custom"
        self.icon = ""
        self.color = ""
        self.owner = "owner"
        self.visibility = "private"
        self.team = []
        self.agents = []
        self.widgets = []
        self.rippleSpec = ripple_spec
        self.share_link_token = None
        self.share_link_access = "view"
        self.shared_with = []
        self.tool_specs = []


def test_pocket_to_domain_promotes_dict_to_ripplespec() -> None:
    from pocketpaw_ee.cloud.pockets import service as svc

    pocket = svc._pocket_to_domain(_FakeDoc(_SEEDED_FLAT))
    assert isinstance(pocket.ripple_spec, RippleSpec)
    assert pocket.ripple_spec.instance_layer.state["title"] == "Hello"
    # Passthrough keys survive promotion + round-trip.
    assert pocket.ripple_spec.to_flat_dict()["some_future_key"] == {"nested": [1, 2, 3]}


def test_pocket_to_domain_none_stays_none() -> None:
    from pocketpaw_ee.cloud.pockets import service as svc

    pocket = svc._pocket_to_domain(_FakeDoc(None))
    assert pocket.ripple_spec is None


def test_pocket_to_domain_empty_dict_stays_empty_dict() -> None:
    # Byte-equivalence: a stored ``rippleSpec: {}`` must stay ``{}`` (NOT
    # promote to a truthy RippleSpec that would later normalize away to None
    # on the wire). The wire serializer keys off truthiness.
    from pocketpaw_ee.cloud.pockets import service as svc

    pocket = svc._pocket_to_domain(_FakeDoc({}))
    assert pocket.ripple_spec == {}
    assert not isinstance(pocket.ripple_spec, RippleSpec)


def test_pocket_to_domain_corrupt_spec_falls_back_to_raw() -> None:
    # A non-dict / unpromotable rippleSpec must NOT break pocket load: the
    # converter falls back to the raw stored value.
    from pocketpaw_ee.cloud.pockets import service as svc

    corrupt = "this is not a spec"
    pocket = svc._pocket_to_domain(_FakeDoc(corrupt))
    assert pocket.ripple_spec == corrupt


# ---------------------------------------------------------------------------
# Wire byte-equivalence — a promoted Pocket serializes to the SAME wire dict
# a legacy (dict) Pocket would. This is the no-document-migration invariant
# expressed at the serializer boundary.
# ---------------------------------------------------------------------------


def test_wire_serialization_is_equivalent_for_dict_and_typed() -> None:
    import dataclasses

    from pocketpaw_ee.cloud.pockets import service as svc
    from pocketpaw_ee.cloud.pockets.dto import pocket_to_wire_dict

    legacy = svc._pocket_to_domain(_FakeDoc(_SEEDED_FLAT))
    # Force the dict path for the comparison baseline.
    legacy_dict = dataclasses.replace(legacy, ripple_spec=dict(_SEEDED_FLAT))

    wire_typed = pocket_to_wire_dict(legacy)
    wire_dict = pocket_to_wire_dict(legacy_dict)

    assert _strip_ids(wire_typed["rippleSpec"]["ui"]) == _strip_ids(wire_dict["rippleSpec"]["ui"])
    assert wire_typed["rippleSpec"]["state"] == wire_dict["rippleSpec"]["state"]
    assert wire_typed["rippleSpec"]["some_future_key"] == {"nested": [1, 2, 3]}
    # The wire rippleSpec is a plain dict, never a RippleSpec object.
    assert isinstance(wire_typed["rippleSpec"], dict)
    assert not isinstance(wire_typed["rippleSpec"], RippleSpec)
