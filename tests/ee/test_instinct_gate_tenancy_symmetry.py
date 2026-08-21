# tests/ee/test_instinct_gate_tenancy_symmetry.py — the structural guard for the
# Instinct approval gate's cross-workspace asserts.
#
# WHY THIS EXISTS. The gate carries one ``_assert_<kind>_workspace`` helper per
# gated proposal kind, and each one must be called in ALL FOUR handlers:
# approve_action, reject_action, bulk_approve_actions, bulk_reject_actions. That
# is eleven kinds x four call sites, maintained by hand — and SHIP-4 wired
# ``_assert_ship_action_workspace`` into only the two BULK handlers while the
# single-approve handler dispatched ``execute_approved_ship_action`` anyway.
#
# The consequence was a real cross-tenant hole: an approver holding
# ``instinct.approve`` in workspace B could approve workspace A's parked box
# teardown. The ship executor re-checks the PROPOSER's ``ship.manage`` in the
# blob's own workspace and never the APPROVER's, so every executor guard passed
# and A's box, plus every app on it, was destroyed. The reject side had the
# mirror hole: B could kill A's pending proposal, and because the row keeps
# pointing at that dead ``pending_destroy_proposal_id``, A could no longer tear
# the resource down through the product at all.
#
# The router's own header states the rule ("asymmetric tenant scope is no tenant
# scope"), but nothing enforced it. This test does, by reading the source: every
# assert helper that exists must be invoked in every one of the four handlers.
# It fails loudly for the NEXT gated kind that forgets one, which is the failure
# mode that actually recurs here.
#
# Created 2026-07-29 (fix/ship-review-p0): new module.

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

_HANDLERS = (
    "approve_action",
    "reject_action",
    "bulk_approve_actions",
    "bulk_reject_actions",
)


def _router_source() -> str:
    from pocketpaw_ee.instinct import router as instinct_router

    return Path(inspect.getfile(instinct_router)).read_text()


def _tree() -> ast.Module:
    return ast.parse(_router_source())


def _assert_helpers(tree: ast.Module) -> set[str]:
    """Every ``_assert_<kind>_workspace`` helper the module defines."""
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name.startswith("_assert_")
        and n.name.endswith("_workspace")
    }


def _calls_in(tree: ast.Module, func_name: str) -> set[str]:
    """Every function called (by bare name) inside ``func_name``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func_name:
            return {
                c.func.id
                for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
    raise AssertionError(f"handler {func_name!r} not found in instinct/router.py")


def test_the_four_handlers_exist() -> None:
    """Guard on the guard — a rename must not make the rest vacuous."""
    tree = _tree()
    for handler in _HANDLERS:
        assert _calls_in(tree, handler), f"{handler} has no calls — did it move?"


def test_at_least_the_known_kinds_are_gated() -> None:
    """Sanity: the helpers exist at all, including ship's."""
    helpers = _assert_helpers(_tree())
    assert "_assert_ship_action_workspace" in helpers
    assert len(helpers) >= 11, f"expected the full gated-kind set, got {sorted(helpers)}"


def _calls_transitive(tree: ast.Module, func_name: str, _seen: set[str] | None = None) -> set[str]:
    """Everything ``func_name`` calls, following module-local calls one level deep.

    Needed because the gate now has TWO shapes: some handlers call each
    ``_assert_<kind>_workspace`` directly, and some call an aggregate
    (``_assert_gated_workspaces``) that calls them all. Both satisfy the rule, so
    the guard resolves through the aggregate instead of demanding one shape.
    """
    seen = _seen if _seen is not None else set()
    if func_name in seen:
        return set()
    seen.add(func_name)
    direct = _calls_in(tree, func_name)
    out = set(direct)
    for callee in direct:
        if callee.startswith("_assert") and callee.endswith("_workspace"):
            continue  # a leaf assert, nothing further to resolve
        try:
            out |= _calls_transitive(tree, callee, seen)
        except AssertionError:
            continue  # not a module-local function (imported / builtin)
    return out


@pytest.mark.parametrize("handler", _HANDLERS)
def test_every_tenancy_assert_runs_in_every_handler(handler: str) -> None:
    """The rule the router's header states, mechanically enforced.

    Approve and reject, single and bulk, must all gate the SAME set of kinds. A
    kind gated on only some of them is the exact shape of the SHIP-4 hole.

    Resolved TRANSITIVELY: calling the ``_assert_gated_workspaces`` aggregate
    counts, since it runs every per-kind assert. That aggregate is the structural
    fix this guard was written to demand — one registration point instead of
    eleven kinds x four call sites — so the guard has to recognise it rather than
    insist on the old hand-copied shape.
    """
    tree = _tree()
    expected = _assert_helpers(tree)
    called = _calls_transitive(tree, handler)
    missing = sorted(expected - called)
    assert not missing, (
        f"{handler} does not run {missing} — a gated kind must be tenancy-checked "
        "in ALL FOUR handlers (approve/reject x single/bulk), either directly or "
        "through the aggregate. Asymmetric tenant scope is no tenant scope: the "
        "unguarded handler lets a caller in another workspace approve or reject "
        "this tenant's parked action."
    )
