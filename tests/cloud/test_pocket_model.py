"""Pocket document model tests.

Uses ``Pocket.model_construct(...)`` rather than the normal constructor because
``Pocket`` is a Beanie Document — ``__init__`` requires collection
initialization, which isn't available in a unit test without a live MongoDB.
``model_construct`` bypasses validation, so these tests verify the default and
storage behavior of ``tool_specs``; schema-level validation of the field shape
is exercised at the integration layer in later tasks.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.models.pocket import Pocket


def test_pocket_tool_specs_defaults_to_empty_list():
    """New pockets have no scoped tools by default — must not inherit anything."""
    p = Pocket.model_construct(workspace="w1", name="n", owner="u1")
    assert p.tool_specs == []


def test_pocket_tool_specs_accepts_list_of_dicts():
    """tool_specs is a free-form list of dicts so built-in IDs, MCP refs,
    and inline declarative tools can all be represented."""
    specs = [
        {"kind": "builtin", "id": "web_fetch"},
        {"kind": "mcp", "server": "notion", "name": "search_pages"},
        {"kind": "inline", "name": "echo", "schema": {"type": "object"}},
    ]
    p = Pocket.model_construct(workspace="w1", name="n", owner="u1", tool_specs=specs)
    assert p.tool_specs == specs
