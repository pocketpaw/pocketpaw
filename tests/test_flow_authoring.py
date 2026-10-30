# tests/test_flow_authoring.py
# Created: 2026-05-31 (RFC 13 M3, feat/m3-flow-authoring-tool).
#
# Changes:
#   - 2026-06-15 (feat/chain-flow-v2): this file is now the BACK-COMPAT proof
#     for CHAIN FLOW v2. The two builders (`build_onboarding_wizard` /
#     `build_due_diligence_intake`) now emit a flat descriptor and delegate to
#     the generalized `build_flow_from_descriptor`, so every assertion here (one
#     terminal with onComplete.kind=="chat", flowId on every step, chain_map
#     keyed on option ids, intermediate Back buttons, deep-validator descent)
#     proves the preset path still produces the exact pre-v2 tree. The one tool
#     test that snapshotted the OLD `{flow_type}` schema was updated to assert
#     the NEW flat-graph descriptor schema (flow/entry/steps + optional
#     flow_type shorthand) — the only intentional spec-driven change here. The
#     GENERAL builder's new behavior lives in tests/test_flow_descriptor.py.
#   - 2026-06-07 (polish/rfc13-flow-nav-validation): added (a) Back-navigation
#     tests asserting both templates' intermediate (non-first, non-terminal)
#     steps render a `flow.back` button while root/terminal steps do not, and
#     (b) deep-validation tests asserting validate_against_catalog /
#     validate_action_verbs now descend chain / chain_map branches — rejecting a
#     bad widget or verb buried in a later flow step, accepting a clean one.
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
    # Both shipped templates loop the collected answers back to the AGENT: the
    # terminal onComplete is a `chat` FlowAction carrying a human-readable prompt
    # (NOT a dead host `emit` event). The runtime appends the accumulated payload
    # to `message` downstream.
    assert action["kind"] == "chat"
    assert isinstance(action.get("message"), str) and action["message"].strip()
    # The retired `emit` shape must be gone — no stray `event` key.
    assert "event" not in action

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


@_BUILDERS
def test_form_steps_carry_structured_field_data(builder) -> None:
    """Each form step ALSO carries genesis-style `form_fields` so ripple's
    FormLayout renders a designed form. Each field has the documented shape
    `{id, label, type, placeholder?, required, options?}`, and every field id
    maps to a real bound input in the raw `ui` fallback (so both render paths
    agree on the keys)."""
    for step in _iter_steps(builder()["ui"]):
        if step.get("intent") != "form":
            continue
        fields = step.get("form_fields")
        assert isinstance(fields, list) and fields, (
            f"form step {step.get('id')!r} carries no form_fields"
        )
        bound_inputs = {
            n["bind"]
            for n in _walk_nodes(step["ui"])
            if n.get("type") == "input" and isinstance(n.get("bind"), str)
        }
        for field in fields:
            assert set(field) >= {"id", "label", "type", "required"}, (
                f"field {field!r} in {step.get('id')!r} missing required keys"
            )
            assert isinstance(field["id"], str) and field["id"]
            assert isinstance(field["label"], str) and field["label"]
            assert isinstance(field["type"], str) and field["type"]
            assert isinstance(field["required"], bool)
            if "options" in field:
                assert isinstance(field["options"], list)
            # The structured field's id must match a raw-ui bind: both paths key
            # the same accumulated formData.
            assert field["id"] in bound_inputs, (
                f"form_field id {field['id']!r} in {step.get('id')!r} has no "
                f"matching bound input (binds={bound_inputs})"
            )


@_BUILDERS
def test_terminal_step_carries_structured_review_rows(builder) -> None:
    """The terminal confirm/summary step carries structured `review_rows`
    (label/value pairs) for a designed summary render; each value reuses the
    same `{state.x}` pre-fill expression as the raw `ui` fallback."""
    terminal = next(s for s in _iter_steps(builder()["ui"]) if _is_terminal(s))
    rows = terminal.get("review_rows")
    assert isinstance(rows, list) and rows
    for row in rows:
        assert set(row) == {"label", "value"}
        assert isinstance(row["label"], str) and row["label"]
        assert isinstance(row["value"], str) and row["value"]
    # At least one row reads back a prior selection and one a prior formData.
    values = " ".join(r["value"] for r in rows)
    assert "_selection." in values
    assert "_formData." in values


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
# Back navigation (Task 1) — intermediate steps render a flow.back control.
# ---------------------------------------------------------------------------


def _back_targets(step: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node in a step's UI whose on_click emits the `flow.back` verb."""
    out: list[dict[str, Any]] = []
    for node in _walk_nodes(step["ui"]):
        handler = node.get("on_click")
        if isinstance(handler, dict) and handler.get("target") == "flow.back":
            out.append(node)
    return out


def _next_targets(step: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in _walk_nodes(step["ui"]):
        handler = node.get("on_click")
        if isinstance(handler, dict) and handler.get("target") == "flow.next":
            out.append(node)
    return out


def _is_root(step: dict[str, Any], root: dict[str, Any]) -> bool:
    return step is root


@_BUILDERS
def test_intermediate_steps_have_a_back_button(builder) -> None:
    """Every non-first, non-terminal step renders a `flow.back` button — so the
    runtime's history stack / flow.back verb is finally reachable from the UI.

    A step is intermediate when it is neither the root (first) step nor the
    terminal (no chain/chain_map) step. The root has nothing to go back to and
    the terminal carries Submit, so neither gets a Back control.
    """
    root = builder()["ui"]
    steps = _iter_steps(root)
    intermediate = [s for s in steps if not _is_root(s, root) and not _is_terminal(s)]
    assert intermediate, "template has no intermediate steps to validate"

    for step in intermediate:
        backs = _back_targets(step)
        assert len(backs) == 1, (
            f"intermediate step {step.get('id')!r} should render exactly one "
            f"flow.back button (found {len(backs)})"
        )
        back = backs[0]
        # Mirrors the _continue_button / _submit_button emit shape exactly.
        assert back["type"] == "button"
        assert back["on_click"]["action"] == "emit"
        assert back["on_click"]["value"] == {}


@_BUILDERS
def test_root_and_terminal_steps_have_no_back_button(builder) -> None:
    """The first step (nothing to go back to) and the terminal step (Submit, not
    Back) must NOT render a flow.back control."""
    root = builder()["ui"]
    assert _back_targets(root) == [], "root step should not have a Back button"
    terminal = next(s for s in _iter_steps(root) if _is_terminal(s))
    assert _back_targets(terminal) == [], "terminal step should not have a Back button"


@_BUILDERS
def test_back_button_precedes_continue(builder) -> None:
    """In an intermediate step's button row, Back sits before Continue."""
    root = builder()["ui"]
    intermediate = [s for s in _iter_steps(root) if not _is_root(s, root) and not _is_terminal(s)]
    for step in intermediate:
        ordered = _walk_nodes(step["ui"])
        back_idx = next(
            i
            for i, n in enumerate(ordered)
            if isinstance(n.get("on_click"), dict) and n["on_click"].get("target") == "flow.back"
        )
        next_idx = next(
            i
            for i, n in enumerate(ordered)
            if isinstance(n.get("on_click"), dict) and n["on_click"].get("target") == "flow.next"
        )
        assert back_idx < next_idx, (
            f"step {step.get('id')!r}: Back should precede Continue in the row"
        )


# ---------------------------------------------------------------------------
# Deep validation (Task 2) — validators descend chain / chain_map branches.
# ---------------------------------------------------------------------------


def test_catalog_validator_descends_chain_and_chain_map() -> None:
    """A bad widget buried in a chain / chain_map branch step is caught.

    Two violations are planted: one in a linear `chain` step's UI, one in a
    `chain_map` branch step's UI. The shallow walker (children-only) would miss
    both; the deep walker must report exactly these two.
    """
    flow = {
        "version": "1.0",
        "ui": {
            "version": "2.0",
            "id": "root",
            "flowId": "root",
            "chain_map": {
                "a": {
                    "version": "2.0",
                    "id": "branch-a",
                    "flowId": "a",
                    "chain": {
                        "version": "2.0",
                        "id": "deep",
                        "flowId": "deep",
                        # buried in a linear chain step's UI
                        "ui": {"type": "container", "children": [{"type": "bogus-widget"}]},
                    },
                    # buried in a chain_map branch step's UI
                    "ui": {"type": "container", "children": [{"type": "another-bad-one"}]},
                },
            },
            "ui": {"type": "container", "children": [{"type": "heading"}]},
        },
    }
    issues = validate_against_catalog(flow, ALLOWED_TYPES)
    bad_types = {i["type"] for i in issues}
    assert bad_types == {"bogus-widget", "another-bad-one"}, (
        f"deep walker must flag both buried bad widgets, got: {bad_types}"
    )


def test_action_verb_validator_descends_chain_and_chain_map() -> None:
    """A bad action verb buried in a chain / chain_map branch is caught."""
    flow = {
        "version": "1.0",
        "ui": {
            "version": "2.0",
            "id": "root",
            "flowId": "root",
            "chain_map": {
                "a": {
                    "version": "2.0",
                    "id": "branch-a",
                    "flowId": "a",
                    "chain": {
                        "version": "2.0",
                        "id": "deep",
                        "flowId": "deep",
                        "ui": {
                            "type": "button",
                            "props": {
                                "label": "Go",
                                "on_click": {"action": "teleport", "target": "x"},
                            },
                        },
                    },
                    "ui": {"type": "container", "children": [{"type": "text"}]},
                },
            },
            "ui": {"type": "container", "children": [{"type": "heading"}]},
        },
    }
    issues = validate_action_verbs(flow)
    bad_verbs = {i["action"] for i in issues}
    assert "teleport" in bad_verbs, (
        f"deep walker must flag the buried unknown verb, got: {bad_verbs}"
    )


@_BUILDERS
def test_full_materialized_flow_passes_deep_validators(builder) -> None:
    """A clean, fully-materialized flow tree (root + every nested step) passes
    BOTH deep validators when validated end to end — not just per-step. This is
    the accept case complementing the reject cases above."""
    doc = builder()
    assert validate_against_catalog(doc, ALLOWED_TYPES) == []
    assert validate_action_verbs(doc) == []


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


def test_terminal_onComplete_loops_to_agent_via_chat() -> None:
    """Both shipped templates finish by handing the collected answers back to the
    AGENT: the terminal onComplete is `{kind:"chat", message:<prompt>}`, never the
    old dead-end `{kind:"emit", event:"onboarding.complete"}`."""
    onboarding = next(s for s in _iter_steps(build_onboarding_wizard()["ui"]) if _is_terminal(s))
    assert onboarding["onComplete"]["kind"] == "chat"
    assert "onboard" in onboarding["onComplete"]["message"].lower()
    assert "event" not in onboarding["onComplete"]

    dd = next(s for s in _iter_steps(build_due_diligence_intake()["ui"]) if _is_terminal(s))
    assert dd["onComplete"]["kind"] == "chat"
    assert "diligence" in dd["onComplete"]["message"].lower()
    assert "event" not in dd["onComplete"]


def test_custom_complete_message_overrides_terminal_prompt() -> None:
    """`config.complete_message` overrides the human-readable prompt the terminal
    step hands the agent, on both templates."""
    ob = build_onboarding_wizard({"complete_message": "Onboarding wrapped, go."})
    ob_term = next(s for s in _iter_steps(ob["ui"]) if _is_terminal(s))
    assert ob_term["onComplete"]["message"] == "Onboarding wrapped, go."

    dd = build_due_diligence_intake({"complete_message": "Intake done, summarize."})
    dd_term = next(s for s in _iter_steps(dd["ui"]) if _is_terminal(s))
    assert dd_term["onComplete"]["message"] == "Intake done, summarize."


def test_legacy_submit_event_config_is_ignored_not_error() -> None:
    """The retired `submit_event` key is accepted (back-compat for old callers)
    but no longer changes the terminal action — there is no host event anymore."""
    doc = build_due_diligence_intake({"submit_event": "deal.review.ready"})
    terminal = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert terminal["onComplete"]["kind"] == "chat"
    assert "event" not in terminal["onComplete"]


# ---------------------------------------------------------------------------
# The `start_flow` tool wrapper.
# ---------------------------------------------------------------------------


@pytest.fixture
def tool() -> StartFlowTool:
    return StartFlowTool()


def test_tool_definition_schema_is_a_flat_graph_descriptor(tool: StartFlowTool) -> None:
    """CHAIN FLOW v2: the LLM-facing schema describes a FLAT step-graph —
    `flow` / `entry` / `steps` (+ optional `title` / `complete`) for the general
    path, plus `flow_type` as OPTIONAL preset shorthand. The `flow_type` enum is
    no longer required, and `steps` is the general authoring surface (the agent
    still never emits the nested tree itself)."""
    defn = tool.definition
    assert defn.name == "start_flow"
    props = defn.parameters["properties"]
    # the flat-graph params plus the preset-shorthand params
    assert {"flow", "entry", "steps", "title", "complete"} <= set(props)
    assert {"flow_type", "domain", "config"} <= set(props)
    # nothing is hard-required at the schema level — execute() validates the
    # combination (general needs flow+entry+steps; preset needs flow_type).
    assert defn.parameters["required"] == []
    # flow_type stays an enum of the known presets, but as optional shorthand.
    assert props["flow_type"]["enum"] == list(FLOW_TYPES)
    assert props["steps"]["type"] == "array"


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
