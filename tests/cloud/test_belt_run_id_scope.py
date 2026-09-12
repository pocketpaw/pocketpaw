# tests/cloud/test_belt_run_id_scope.py — structural pin for the belt join key.
# Created 2026-09-12 (integration/belt-factory).
#
# WHY A SOURCE-STRUCTURE TEST: the belt entity/stage events are joined by a
# ``run_id`` read from the ``_active_stream_run_id`` ContextVar. The value is
# bound in ``run_core._drive_agent_loop`` and read much later, inside an MCP tool
# the agent calls. Every behavioural test in the suite injects the run id
# directly, so if a refactor moved the bind into a different scope than the
# readers — or spawned the agent's tasks BEFORE the bind, so their copied context
# never sees it — the join would silently return None and every one of those
# tests would still pass. The e2e test cannot catch it either: it does not drive
# ``run_core`` (that would need the LLM it deliberately refuses to fake).
#
# So this pins the two properties that make the ContextVar reachable at all:
#   1. bind, unbind and the entity bridge all live in ONE function.
#   2. the bind happens BEFORE the bridge call and before any task is spawned in
#      that function (``asyncio.create_task`` copies the context AT creation, so
#      a task created before the bind is blind to it forever).
from __future__ import annotations

import ast
import re
from pathlib import Path

RUN_CORE = (
    Path(__file__).resolve().parents[2]
    / "ee"
    / "pocketpaw_ee"
    / "cloud"
    / "chat"
    / "runs"
    / "run_core.py"
)


def _tree() -> tuple[ast.Module, str]:
    src = RUN_CORE.read_text()
    return ast.parse(src), src


def _enclosing_function(tree: ast.Module, line: int) -> ast.FunctionDef | ast.AsyncFunctionDef:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.lineno <= line <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    assert best is not None, f"no enclosing function for line {line}"
    return best


def _lines_calling(src: str, name: str) -> list[int]:
    """Lines calling ``name``. Word-boundary matched: a plain substring test for
    ``bind_stream_run_id(`` also fires on ``unbind_stream_run_id(``."""
    pattern = re.compile(rf"(?<![\w.]){re.escape(name)}\s*\(")
    return [
        i
        for i, line in enumerate(src.splitlines(), 1)
        if pattern.search(line) and not line.strip().startswith("#")
    ]


def test_bind_unbind_and_bridge_share_one_scope() -> None:
    tree, src = _tree()
    bind = _lines_calling(src, "bind_stream_run_id")
    unbind = _lines_calling(src, "unbind_stream_run_id")
    bridge = _lines_calling(src, "maybe_emit_belt_entity_changed")

    assert len(bind) == 1, f"expected exactly one bind site, found {bind}"
    assert len(unbind) == 1, f"expected exactly one unbind site, found {unbind}"
    assert bridge, "the belt entity bridge call vanished from run_core"

    owners = {_enclosing_function(tree, line).name for line in (bind + unbind + bridge)}
    assert len(owners) == 1, (
        "the run-id bind, its reset and the belt entity bridge must live in one "
        f"function or the ContextVar is not reachable at emit time; found {owners}"
    )


def test_bind_precedes_the_bridge_and_every_spawned_task() -> None:
    tree, src = _tree()
    bind_line = _lines_calling(src, "bind_stream_run_id")[0]
    bridge_line = min(_lines_calling(src, "maybe_emit_belt_entity_changed"))
    fn = _enclosing_function(tree, bind_line)

    assert bind_line < bridge_line, (
        "the run id must be bound before the belt entity bridge reads it "
        f"(bind at {bind_line}, bridge at {bridge_line})"
    )

    spawns = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create_task", "ensure_future"}
    ]
    early = [line for line in spawns if line < bind_line]
    assert not early, (
        "a task spawned before the run-id bind copies a context without it and "
        f"can never see the join key; offending lines: {early}"
    )
