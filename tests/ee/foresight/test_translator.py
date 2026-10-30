# tests/ee/foresight/test_translator.py
# Created: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
#
# Pin the CAMEL FunctionTool → Claude Code SDK Permissions translator
# (RFC §6.4 follow-up flagged in PR 2).
#
# Tests cover:
#   - Empty / None tool lists return {} (no override).
#   - CAMEL FunctionTool with func.__name__ ∈ SDK built-ins is allowed.
#   - OpenAI-shaped dict with {"function": {"name": ...}} is allowed.
#   - OpenAI-shaped dict with {"name": ...} is allowed (alternate shape).
#   - Unknown tool names are dropped (not in SDK built-in set).
#   - Mixed lists (knowns + unknowns) emit a clean allow-list.
#   - Custom allowed_sdk_tools narrows the whitelist.
#   - Tools without an extractable name are dropped.
#   - Duplicates in the input list are deduplicated in the output.

from __future__ import annotations

from dataclasses import dataclass

from pocketpaw_ee.foresight.llm.adapter import (
    _SDK_BUILTIN_TOOLS,
    translate_camel_tools_to_sdk_overrides,
)

# --- Fakes ----------------------------------------------------------


@dataclass
class _FakeFunctionTool:
    """Quacks like ``camel.toolkits.FunctionTool`` — has ``.func`` with a
    ``__name__`` attribute.
    """

    func: object

    @staticmethod
    def with_name(name: str) -> _FakeFunctionTool:
        func = type("Fn", (), {"__name__": name})()
        return _FakeFunctionTool(func=func)


# --- Empty / None inputs --------------------------------------------


def test_empty_list_returns_empty_dict():
    assert translate_camel_tools_to_sdk_overrides([]) == {}


def test_none_returns_empty_dict():
    assert translate_camel_tools_to_sdk_overrides(None) == {}


# --- CAMEL FunctionTool path ----------------------------------------


def test_camel_function_tool_with_known_name_is_allowed():
    tool = _FakeFunctionTool.with_name("Read")
    result = translate_camel_tools_to_sdk_overrides([tool])
    assert result == {"allow": ["Read"]}


def test_camel_function_tool_with_unknown_name_is_dropped():
    tool = _FakeFunctionTool.with_name("twitter_post")
    result = translate_camel_tools_to_sdk_overrides([tool])
    assert result == {}


# --- OpenAI-shaped dict path ----------------------------------------


def test_openai_function_dict_with_function_block():
    tool = {"type": "function", "function": {"name": "Bash"}}
    result = translate_camel_tools_to_sdk_overrides([tool])
    assert result == {"allow": ["Bash"]}


def test_openai_dict_with_top_level_name_key():
    tool = {"name": "Write"}
    result = translate_camel_tools_to_sdk_overrides([tool])
    assert result == {"allow": ["Write"]}


# --- Mixed lists ----------------------------------------------------


def test_mixed_known_and_unknown_emits_known_only():
    tools = [
        _FakeFunctionTool.with_name("Read"),
        _FakeFunctionTool.with_name("twitter_post"),  # unknown
        {"function": {"name": "Glob"}},
        {"function": {"name": "make_post"}},  # unknown
    ]
    result = translate_camel_tools_to_sdk_overrides(tools)
    assert set(result["allow"]) == {"Read", "Glob"}


def test_duplicates_in_input_are_deduplicated_in_output():
    tools = [
        _FakeFunctionTool.with_name("Read"),
        _FakeFunctionTool.with_name("Read"),
        _FakeFunctionTool.with_name("Read"),
    ]
    result = translate_camel_tools_to_sdk_overrides(tools)
    assert result == {"allow": ["Read"]}


# --- Custom whitelist -----------------------------------------------


def test_custom_allowed_sdk_tools_narrows_whitelist():
    """Caller can restrict the SDK-side allow-list further."""
    tools = [
        _FakeFunctionTool.with_name("Bash"),
        _FakeFunctionTool.with_name("Read"),
    ]
    # Only allow Read (Bash is too dangerous for this scenario).
    result = translate_camel_tools_to_sdk_overrides(tools, allowed_sdk_tools={"Read"})
    assert result == {"allow": ["Read"]}


def test_empty_whitelist_returns_empty_dict():
    tools = [_FakeFunctionTool.with_name("Bash"), _FakeFunctionTool.with_name("Read")]
    result = translate_camel_tools_to_sdk_overrides(tools, allowed_sdk_tools=set())
    assert result == {}


# --- Pathological inputs --------------------------------------------


def test_tool_without_extractable_name_is_dropped():
    """A tool that has neither .func.__name__ nor a name key is dropped."""

    class _NoName:
        pass

    result = translate_camel_tools_to_sdk_overrides([_NoName()])
    assert result == {}


def test_dict_with_non_string_name_is_dropped():
    tools = [{"function": {"name": 123}}, {"function": {"name": None}}]
    assert translate_camel_tools_to_sdk_overrides(tools) == {}


def test_empty_name_string_is_dropped():
    tools = [_FakeFunctionTool.with_name("")]
    assert translate_camel_tools_to_sdk_overrides(tools) == {}


# --- Lock the SDK built-in set --------------------------------------


def test_sdk_builtin_tools_includes_core_set():
    """Sanity check — the constant has not silently been emptied."""
    must_have = {"Bash", "Read", "Write", "Edit", "Glob", "Grep"}
    assert must_have.issubset(_SDK_BUILTIN_TOOLS)
