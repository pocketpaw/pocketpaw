# tests/ee/pockets/test_ripple_spec_model.py
# Created: 2026-06-19 (feat/typed-ripplespec-phase1) — TDD unit tests for the
# typed RippleSpec / TemplateLayer / InstanceLayer models (RFC-03-v2 runtime
# layer split). These are PURE model tests — no Mongo, no FastAPI. They pin
# the five adversarial-review must-fix contracts:
#   #1 to_flat_dict uses exclude_unset → never injects null keys absent from
#      the original, and round-trips extra/passthrough keys.
#   #2 every compile_template passthrough key is an explicit typed field
#      (no silent fall-through to __pydantic_extra__).
#   #3/#4 the layer split is the type boundary the clobber-fix is built on:
#      with_template_layer preserves state, with_instance_layer preserves ui.
# The model lives in pocketpaw.bundled_templates.schema (OSS core) so a future
# non-EE consumer (CLI lint, registry validator) can use it without an EE dep.
"""Unit tests for the typed, layer-split RippleSpec."""

from __future__ import annotations

from pocketpaw.bundled_templates import InstanceLayer, RippleSpec, TemplateLayer

# ---------------------------------------------------------------------------
# from_flat_dict — promotion entry point
# ---------------------------------------------------------------------------


def test_from_flat_dict_promotes_legacy_spec() -> None:
    spec = RippleSpec.from_flat_dict(
        {"ui": {"type": "stack", "id": "n_abc"}, "state": {"items": []}, "sources": {}}
    )
    assert isinstance(spec, RippleSpec)
    assert spec.template_layer.ui == {"type": "stack", "id": "n_abc"}
    assert spec.instance_layer.state == {"items": []}


def test_from_flat_dict_returns_none_on_none_input() -> None:
    assert RippleSpec.from_flat_dict(None) is None


def test_from_flat_dict_returns_none_on_non_dict() -> None:
    # Must NOT raise — a corrupted spec value must not break pocket load.
    assert RippleSpec.from_flat_dict("not a dict") is None  # type: ignore[arg-type]
    assert RippleSpec.from_flat_dict(42) is None  # type: ignore[arg-type]


def test_from_flat_dict_idempotent_on_ripplespec() -> None:
    original = RippleSpec(ui={"type": "stack"})
    assert RippleSpec.from_flat_dict(original) is original


# ---------------------------------------------------------------------------
# to_flat_dict — BSON-equivalence + must-fix #1 (no null injection)
# ---------------------------------------------------------------------------


def test_to_flat_dict_round_trips_extra_keys() -> None:
    flat = {
        "ui": {"type": "stack"},
        "state": {"rows": [1, 2]},
        "kb_scope": "pocket",
        "schema_version": "2",
    }
    spec = RippleSpec.from_flat_dict(flat)
    assert spec is not None
    out = spec.to_flat_dict()
    assert out["kb_scope"] == "pocket"
    assert out["schema_version"] == "2"


def test_to_flat_dict_round_trips_unknown_extra_keys() -> None:
    # A key NOT declared as a typed field must survive a round-trip via
    # __pydantic_extra__ (extra="allow"), so an unknown future field on a
    # stored doc is never silently dropped on the next write.
    flat = {"ui": {"type": "stack"}, "some_future_field": {"nested": True}}
    spec = RippleSpec.from_flat_dict(flat)
    assert spec is not None
    out = spec.to_flat_dict()
    assert out["some_future_field"] == {"nested": True}


def test_to_flat_dict_does_not_inject_null_keys() -> None:
    # "actions" was absent in the input — it must NOT appear as a null key
    # in the serialized dict (must-fix #1). Injecting nulls would pollute
    # documents that originally omitted the key and break .get() readers
    # that distinguish "absent" from "null".
    spec = RippleSpec.from_flat_dict({"ui": {"type": "stack"}, "state": {}})
    assert spec is not None
    out = spec.to_flat_dict()
    assert "actions" not in out
    assert "sources" not in out
    assert "agents" not in out


def test_to_flat_dict_preserves_explicit_null() -> None:
    # A key explicitly set to None in the original (a deliberate clear) IS
    # part of model_fields_set, so exclude_unset keeps it.
    spec = RippleSpec.from_flat_dict({"ui": None, "state": {"x": 1}})
    assert spec is not None
    out = spec.to_flat_dict()
    assert "ui" in out
    assert out["ui"] is None


# ---------------------------------------------------------------------------
# layer split — must-fix #3/#4 (the clobber-fix type boundary)
# ---------------------------------------------------------------------------


def test_with_template_layer_preserves_state() -> None:
    spec = RippleSpec.from_flat_dict({"ui": {"type": "old"}, "state": {"items": ["a", "b"]}})
    assert spec is not None
    new_layer = TemplateLayer(ui={"type": "new"}, actions={"act_1": {"kind": "write_binding"}})
    merged = spec.with_template_layer(new_layer)
    # Instance-owned state is preserved verbatim; template UI is refreshed.
    assert merged.instance_layer.state == {"items": ["a", "b"]}
    assert merged.template_layer.ui == {"type": "new"}
    assert merged.template_layer.actions == {"act_1": {"kind": "write_binding"}}
    # Original is not mutated (with_* returns a NEW object).
    assert spec.template_layer.ui == {"type": "old"}


def test_with_instance_layer_preserves_ui() -> None:
    spec = RippleSpec.from_flat_dict({"ui": {"type": "stack"}, "state": {"old": True}})
    assert spec is not None
    merged = spec.with_instance_layer(InstanceLayer(state={"items": []}))
    assert merged.template_layer.ui == {"type": "stack"}
    assert merged.instance_layer.state == {"items": []}
    # Original is not mutated.
    assert spec.instance_layer.state == {"old": True}


def test_template_layer_property_excludes_instance_keys() -> None:
    spec = RippleSpec.from_flat_dict(
        {"ui": {"type": "stack"}, "state": {"rows": []}, "selections": {"sel": 1}}
    )
    assert spec is not None
    layer = spec.template_layer
    dumped = layer.model_dump(exclude_unset=True)
    assert "state" not in dumped
    assert "selections" not in dumped


# ---------------------------------------------------------------------------
# passthrough fields — must-fix #2 (no silent __pydantic_extra__ fall-through)
# ---------------------------------------------------------------------------


def test_compile_template_passthrough_fields_accessible() -> None:
    spec = RippleSpec.from_flat_dict(
        {"agents": [{"name": "helper"}], "triggers": [], "kb_scope": "global"}
    )
    assert spec is not None
    # These would FAIL (return None / KeyError) if the keys landed in
    # __pydantic_extra__ instead of being explicit typed fields.
    assert spec.agents == [{"name": "helper"}]
    assert spec.kb_scope == "global"
    assert spec.triggers == []


def test_all_compile_passthrough_keys_are_declared_fields() -> None:
    # Pin must-fix #2: every key compile_template can emit that a downstream
    # `.get("<key>")` reader depends on must be a declared field, NOT an extra.
    declared = set(RippleSpec.model_fields.keys())
    passthrough = {
        "schema_version",
        "name",
        "version",
        "agents",
        "triggers",
        "outcomes",
        "kb_scope",
        "skill_refs",
        "permissions",
        "instinct_rules",
    }
    missing = passthrough - declared
    assert not missing, f"compile_template passthrough keys not declared as fields: {missing}"


def test_template_layer_fields_are_template_owned_set() -> None:
    # The TemplateLayer field set is the single source of truth reconcile
    # derives _TEMPLATE_OWNED_REGIONS from. Pin the contract so a field add
    # there is a deliberate, test-visible change.
    assert set(TemplateLayer.model_fields.keys()) == {"ui", "actions", "sources", "shape"}


def test_instance_layer_fields_are_instance_owned_set() -> None:
    assert set(InstanceLayer.model_fields.keys()) == {"state", "selections"}
