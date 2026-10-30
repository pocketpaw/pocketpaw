# tests/test_mcp_arg_coercion.py
# Unit tests for the shared coerce_json_object_args helper — the fix for
# object/array MCP tool args the model sometimes sends as JSON *strings*,
# forcing a wasted retry ("the tool needs an actual object, not a
# stringified JSON").
# Added: 2026-07-03 (fix/mcp-tool-json-string-args).

from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args


def test_json_string_object_is_decoded():
    out = coerce_json_object_args({"hints": '{"name": "x"}'}, ["hints"])
    assert out["hints"] == {"name": "x"}


def test_json_string_array_is_decoded():
    out = coerce_json_object_args({"ops": '[{"op": "set_state"}]'}, ["ops"])
    assert out["ops"] == [{"op": "set_state"}]


def test_already_a_dict_passes_through_untouched():
    src = {"spec": {"type": "chart"}}
    out = coerce_json_object_args(src, ["spec"])
    assert out["spec"] == {"type": "chart"}


def test_non_json_string_is_left_untouched():
    # A malformed JSON string is left as-is so the handler's own type check
    # surfaces its normal, precise error rather than a helper-invented one.
    out = coerce_json_object_args({"spec": "{not json"}, ["spec"])
    assert out["spec"] == "{not json"


def test_scalar_json_string_is_left_untouched():
    # "5" parses as JSON but is a scalar, not an object/array — leave it so we
    # never turn a stray numeric-looking string into an int under an
    # object-typed param.
    out = coerce_json_object_args({"spec": "5"}, ["spec"])
    assert out["spec"] == "5"


def test_plain_string_is_left_untouched():
    # A value that does not even look like a JSON literal is never touched.
    out = coerce_json_object_args({"name": "todo-tracker"}, ["name"])
    assert out["name"] == "todo-tracker"


def test_only_named_keys_are_coerced():
    src = {"hints": '{"a": 1}', "other": '{"b": 2}'}
    out = coerce_json_object_args(src, ["hints"])
    assert out["hints"] == {"a": 1}
    assert out["other"] == '{"b": 2}'  # not in keys → untouched


def test_missing_key_is_a_noop():
    out = coerce_json_object_args({"brief": "hi"}, ["hints", "spec"])
    assert out == {"brief": "hi"}


def test_none_value_is_a_noop():
    out = coerce_json_object_args({"hints": None}, ["hints"])
    assert out["hints"] is None


def test_input_mapping_is_not_mutated():
    src = {"hints": '{"name": "x"}'}
    coerce_json_object_args(src, ["hints"])
    assert src["hints"] == '{"name": "x"}'  # original untouched


def test_whitespace_padded_json_string_is_decoded():
    out = coerce_json_object_args({"spec": '  {"type": "chart"}  '}, ["spec"])
    assert out["spec"] == {"type": "chart"}
