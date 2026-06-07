# tests/test_flow_authoring.py
# Created: 2026-05-31 (RFC 13 M3, feat/m3-flow-authoring-tool).
#
# Tests for the `start_flow` Chain Flow authoring tool and its deterministic
# builders (`pocketpaw.ripple._flows` + `pocketpaw.tools.builtin.flow_tool`).
#
# What's asserted:
#   1. The builder, given a descriptor, emits a STRUCTURALLY-VALID nested flow:
#      correct chain / chain_map nesting, flowId namespacing, a terminal
#      onComplete FlowAction, and {state.x} cross-step pre-fill expressions.
#   2. Every step's emitted UI passes pocketpaw's own ingest walkers —
#      `validate_against_catalog` (type allow-list) and `validate_action_verbs`
#      (event-handler verb allow-list) — so the flow the model ships renders.
#   3. The `start_flow` tool wraps the builder: tiny descriptor in, a
#      `{version, ui}` JSON doc out; unknown flow_type returns an agent-readable
#      error. No LLM / agent call is made (the tool is pure-deterministic).
#
# No EE imports — runs in the OSS-only CI scope. Both validators live in the
# OSS core (`pocketpaw.ripple.manifest`).

from __future__ import annotations

import json
from typing import Any

import pytest

from pocketpaw.ripple._flows import (
    FLOW_TYPES,
    INLINE_SPEC_VERSION,
    build_due_diligence_intake,
    build_flow,
    build_onboarding_wizard,
)
from pocketpaw.ripple.manifest import validate_action_verbs, validate_against_catalog
from pocketpaw.tools.builtin.flow_tool import StartFlowTool

# The catalog allow-list every emitted step UI must stay inside. These are the
# widget types the builders use; in production the list comes from the live
# widget manifest (`allowed_types_from_manifest`). Listing them explicitly here
# keeps the test hermetic (no network fetch) and documents the builders' palette.
ALLOWED_TYPES = ["container", "heading", "text", "input", "button"]


# ---------------------------------------------------------------------------
# Helpers — walk the materialized flow tree.
# ---------------------------------------------------------------------------


def _iter_steps(step: dict[str, Any]) -> list[dict[str, Any]]:
    """Depth-first collect every DISTINCT UniversalSpec step reachable from
    `step` via `chain` (linear) and `chain_map` (branch).

    Deduped by object identity: branches legitimately CONVERGE on a shared
    later step (both onboarding branches reuse the same confirm step, like the
    M1 fixture), and that shared step must be counted once, not once per branch.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    def _visit(node: dict[str, Any]) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        out.append(node)
        nxt = node.get("chain")
        if isinstance(nxt, dict):
            _visit(nxt)
        cmap = node.get("chain_map")
        if isinstance(cmap, dict):
            for branch in cmap.values():
                if isinstance(branch, dict):
                    _visit(branch)

    _visit(step)
    return out


def _all_flow_text(root_step: dict[str, Any]) -> str:
    """All visible text/label copy across EVERY step's UI in the flow tree."""
    blobs: list[str] = []
    for step in _iter_steps(root_step):
        blobs.extend(_all_text_blobs(step.get("ui")))
    return " ".join(blobs)


def _is_terminal(step: dict[str, Any]) -> bool:
    return "chain" not in step and "chain_map" not in step


def _all_text_blobs(node: Any) -> list[str]:
    """Collect every `props.text` / `props.label` string in a UI node tree —
    used to assert {state.x} pre-fill expressions are present."""
    blobs: list[str] = []
    if isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict):
            for key in ("text", "label"):
                v = props.get(key)
                if isinstance(v, str):
                    blobs.append(v)
        for child in node.get("children") or []:
            blobs.extend(_all_text_blobs(child))
    return blobs


# Builders under test, parametrized so the structural contract is enforced for
# every template uniformly.
_BUILDERS = pytest.mark.parametrize(
    "builder",
    [build_onboarding_wizard, build_due_diligence_intake],
    ids=["onboarding_wizard", "due_diligence_intake"],
)


# ---------------------------------------------------------------------------
# Envelope shape.
# ---------------------------------------------------------------------------


@_BUILDERS
def test_emits_version_ui_envelope(builder) -> None:
    """The doc is the canonical inline-Ripple `{version, ui}` envelope — a
    top-level `ui` dict, which is exactly what the cloud extractor recognizes
    (`run_core._looks_like_ripple_spec`)."""
    doc = builder()
    assert doc["version"] == INLINE_SPEC_VERSION
    assert isinstance(doc["ui"], dict)
    # JSON-serializable end to end (the tool returns json.dumps of this).
    assert json.loads(json.dumps(doc)) == doc


# ---------------------------------------------------------------------------
# Structural validity of the nested tree.
# ---------------------------------------------------------------------------


@_BUILDERS
def test_root_branches_via_chain_map(builder) -> None:
    """The first step branches on the user's selection — it carries a
    `chain_map` (not a linear `chain`)."""
    root = builder()["ui"]
    assert isinstance(root.get("chain_map"), dict)
    assert root.get("chain") is None
    assert len(root["chain_map"]) >= 2  # a real branch, not a degenerate one


@_BUILDERS
def test_every_step_has_a_flow_id(builder) -> None:
    """flowId namespaces each step's accumulated data — every step must carry
    one or cross-step `{state.<flowId>_*}` pre-fill can't resolve."""
    for step in _iter_steps(builder()["ui"]):
        assert isinstance(step.get("flowId"), str) and step["flowId"], (
            f"step {step.get('id')!r} is missing a flowId"
        )


@_BUILDERS
def test_exactly_one_terminal_step_with_on_complete(builder) -> None:
    """The tree converges on a single terminal step (no chain/chain_map) that
    carries an `onComplete` FlowAction; non-terminal steps must NOT."""
    steps = _iter_steps(builder()["ui"])
    terminals = [s for s in steps if _is_terminal(s)]
    assert len(terminals) == 1, "flow must converge on exactly one terminal step"

    terminal = terminals[0]
    action = terminal.get("onComplete")
    assert isinstance(action, dict)
    assert action.get("kind") in {"emit", "navigate", "chat"}
    assert action["kind"] == "emit"  # both non-commerce templates emit
    assert isinstance(action.get("event"), str) and action["event"]

    for step in steps:
        if not _is_terminal(step):
            assert "onComplete" not in step, (
                f"non-terminal step {step.get('id')!r} should not carry onComplete"
            )


@_BUILDERS
def test_chain_map_keys_match_selection_ids(builder) -> None:
    """Every `chain_map` key must be emitted as a selectable option's id in the
    same step's UI — otherwise a branch is unreachable (the ChainExecutor keys
    the map on the selected item's id)."""
    for step in _iter_steps(builder()["ui"]):
        cmap = step.get("chain_map")
        if not isinstance(cmap, dict):
            continue
        emitted_ids: set[str] = set()
        for node in _walk_nodes(step["ui"]):
            handler = node.get("on_click")
            if isinstance(handler, dict) and handler.get("target") == "flow.next":
                sel = (handler.get("value") or {}).get("selection")
                if isinstance(sel, dict) and isinstance(sel.get("id"), str):
                    emitted_ids.add(sel["id"])
        for key in cmap:
            assert key in emitted_ids, (
                f"chain_map branch {key!r} in step {step.get('id')!r} has no "
                f"matching selectable option (emitted ids: {sorted(emitted_ids)})"
            )


def _walk_nodes(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        out.append(node)
        for child in node.get("children") or []:
            out.extend(_walk_nodes(child))
    return out


@_BUILDERS
def test_terminal_step_prefills_via_state_expressions(builder) -> None:
    """The terminal (confirm/review) step reads back earlier answers with
    `{state.<flowId>_selection.field}` / `{state.<flowId>_formData.field}`
    cross-step expressions."""
    terminal = next(s for s in _iter_steps(builder()["ui"]) if _is_terminal(s))
    blobs = _all_text_blobs(terminal["ui"])
    state_exprs = [b for b in blobs if "{state." in b]
    assert state_exprs, "terminal step has no {state.x} pre-fill expression"
    # At least one reads a prior step's *selection* and one a prior *formData*,
    # proving both accumulation paths are exercised.
    assert any("_selection." in b for b in state_exprs)
    assert any("_formData." in b for b in state_exprs)


@_BUILDERS
def test_form_steps_carry_inputs_forward(builder) -> None:
    """Each form step's primary button forwards the bound fields as
    `value.formData` via `{state.<bind>}`, so the entered data accumulates."""
    for step in _iter_steps(builder()["ui"]):
        if step.get("intent") != "form":
            continue
        binds = {
            n["bind"]
            for n in _walk_nodes(step["ui"])
            if n.get("type") == "input" and isinstance(n.get("bind"), str)
        }
        assert binds, f"form step {step.get('id')!r} has no bound inputs"
        # The flow.next handler's formData must reference those binds.
        forwarded: set[str] = set()
        for n in _walk_nodes(step["ui"]):
            handler = n.get("on_click")
            if isinstance(handler, dict) and handler.get("target") == "flow.next":
                fd = (handler.get("value") or {}).get("formData")
                if isinstance(fd, dict):
                    forwarded.update(fd.keys())
        assert binds <= forwarded, (
            f"form step {step.get('id')!r} does not forward all binds "
            f"(binds={binds}, forwarded={forwarded})"
        )


# ---------------------------------------------------------------------------
# The emitted spec passes pocketpaw's ingest validators (per-step).
# ---------------------------------------------------------------------------


@_BUILDERS
def test_every_step_passes_catalog_validator(builder) -> None:
    """Each step's UI tree contains only catalog widgets — the builder never
    emits a node type that would render as an 'Unknown widget' box."""
    for step in _iter_steps(builder()["ui"]):
        issues = validate_against_catalog({"ui": step["ui"]}, ALLOWED_TYPES)
        assert issues == [], f"step {step.get('id')!r} has catalog violations: {issues}"


@_BUILDERS
def test_every_step_passes_action_verb_validator(builder) -> None:
    """Each step's event handlers use a known action verb. The flow verbs ride
    on the `emit` action (target `flow.next` / `flow.submit`), and `emit` is in
    the known-verb set — so a correctly-authored flow has zero verb issues."""
    for step in _iter_steps(builder()["ui"]):
        issues = validate_action_verbs({"ui": step["ui"]})
        assert issues == [], f"step {step.get('id')!r} has action-verb violations: {issues}"


# ---------------------------------------------------------------------------
# Template-specific shape checks.
# ---------------------------------------------------------------------------


def test_onboarding_wizard_has_three_logical_steps() -> None:
    """goal -> details/invite (branch) -> confirm: 4 step objects (the two
    branches share the confirm), 3 logical stages."""
    steps = _iter_steps(build_onboarding_wizard()["ui"])
    flow_ids = {s["flowId"] for s in steps}
    assert flow_ids == {"pick_goal", "enter_details", "confirm"}


def test_due_diligence_intake_has_four_logical_steps() -> None:
    """stage -> financials (branch) -> risk -> review."""
    steps = _iter_steps(build_due_diligence_intake()["ui"])
    flow_ids = {s["flowId"] for s in steps}
    assert flow_ids == {"deal_stage", "financials", "risk_review", "review"}


def test_config_overrides_copy_not_structure() -> None:
    """A `config` override changes visible copy but leaves the tree shape (the
    flowId set, the branch count) identical."""
    base = build_onboarding_wizard()
    themed = build_onboarding_wizard({"product_name": "Foresight"})

    assert {s["flowId"] for s in _iter_steps(base["ui"])} == {
        s["flowId"] for s in _iter_steps(themed["ui"])
    }
    assert "Foresight" in _all_flow_text(themed["ui"])
    assert "Foresight" not in _all_flow_text(base["ui"])


def test_due_diligence_custom_submit_event() -> None:
    doc = build_due_diligence_intake({"submit_event": "deal.review.ready"})
    terminal = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert terminal["onComplete"]["event"] == "deal.review.ready"


# ---------------------------------------------------------------------------
# The `start_flow` tool wrapper.
# ---------------------------------------------------------------------------


@pytest.fixture
def tool() -> StartFlowTool:
    return StartFlowTool()


def test_tool_definition_schema_is_a_tiny_descriptor(tool: StartFlowTool) -> None:
    """The LLM-facing schema is the descriptor — flow_type (enum) plus optional
    domain/config — NOT the tree."""
    defn = tool.definition
    assert defn.name == "start_flow"
    props = defn.parameters["properties"]
    assert set(props) == {"flow_type", "domain", "config"}
    assert defn.parameters["required"] == ["flow_type"]
    assert props["flow_type"]["enum"] == list(FLOW_TYPES)


async def test_tool_returns_version_ui_doc(tool: StartFlowTool) -> None:
    """A bare `flow_type` yields a valid `{version, ui}` doc with the flow
    tree, matching the builder's output exactly."""
    result = await tool.execute(flow_type="onboarding_wizard")
    doc = json.loads(result)
    assert doc["version"] == INLINE_SPEC_VERSION
    assert isinstance(doc["ui"], dict)
    assert doc == build_onboarding_wizard()


async def test_tool_applies_config(tool: StartFlowTool) -> None:
    result = await tool.execute(
        flow_type="due_diligence_intake",
        config={"company_name": "Northwind"},
    )
    doc = json.loads(result)
    assert "Northwind" in _all_flow_text(doc["ui"])


async def test_tool_accepts_config_as_json_string(tool: StartFlowTool) -> None:
    """Subprocess / CLI callers pass `config` as a JSON string through the flat
    signature — the tool coerces it."""
    result = await tool.execute(
        flow_type="onboarding_wizard",
        config='{"product_name": "Atlas"}',
    )
    doc = json.loads(result)
    assert "Atlas" in _all_flow_text(doc["ui"])


async def test_tool_unknown_flow_type_is_agent_readable(tool: StartFlowTool) -> None:
    """An unknown template returns an Error string listing the valid set — the
    model can read it and retry rather than getting a stack trace."""
    result = await tool.execute(flow_type="checkout_flow")
    assert result.startswith("Error:")
    assert "onboarding_wizard" in result
    assert "due_diligence_intake" in result


async def test_tool_missing_flow_type_errors(tool: StartFlowTool) -> None:
    result = await tool.execute()
    assert result.startswith("Error:")
    assert "flow_type" in result


async def test_tool_bad_config_json_errors(tool: StartFlowTool) -> None:
    result = await tool.execute(flow_type="onboarding_wizard", config="{not json")
    assert result.startswith("Error:")


async def test_build_flow_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="Unknown flow_type"):
        build_flow("nope")
