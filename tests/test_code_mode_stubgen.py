# Tests for the Code Mode stub generator.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# Proves: a read-safe tool gets a stub; a write/gated tool is ABSENT from the
# generated module; the generated module is syntactically valid Python.

from __future__ import annotations

import ast
from typing import Any

from pocketpaw.tools.code_mode.stubgen import generate_stub_module, read_safe_tools
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry


class _Tool(BaseTool):
    def __init__(self, name: str, trust: str, params: dict | None = None) -> None:
        self._name = name
        self._trust = trust
        self._params = params or {"type": "object", "properties": {}, "required": []}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"do {self._name}"

    @property
    def trust_level(self) -> str:
        return self._trust

    @property
    def parameters(self) -> dict[str, Any]:
        return self._params

    async def execute(self, **params: Any) -> str:
        return "ok"


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        _Tool(
            "read_file",
            "standard",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "encoding": {"type": "string"},
                },
                "required": ["path"],
            },
        )
    )
    reg.register(_Tool("web_search", "standard", {"type": "object", "properties": {}}))
    reg.register(_Tool("write_file", "standard"))  # mutating — must be excluded
    reg.register(_Tool("fabric_query", "high"))  # gated — must be excluded
    return reg


def test_read_safe_tools_filters_writes_and_gated():
    names = {t.name for t in read_safe_tools(_registry())}
    assert "read_file" in names
    assert "web_search" in names
    assert "write_file" not in names
    assert "fabric_query" not in names


def test_generated_module_is_valid_python():
    src = generate_stub_module(_registry())
    # Must parse — a syntax error here means a broken sandbox script.
    ast.parse(src)


def test_generated_module_contains_only_read_safe_stubs():
    src = generate_stub_module(_registry())
    assert "def read_file(" in src
    assert "def web_search(" in src
    assert "def write_file(" not in src
    assert "def fabric_query(" not in src
    # The transport helper is present.
    assert "def _call(" in src


def test_stub_signature_carries_params():
    src = generate_stub_module(_registry())
    # read_file has a required 'path' and optional 'encoding'.
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "read_file" in funcs
    arg_names = [a.arg for a in funcs["read_file"].args.args]
    assert "path" in arg_names
    assert "encoding" in arg_names
