# tests/test_flow_descriptor.py
# Created: 2026-06-15 (feat/chain-flow-v2).
# Updated: 2026-06-15 (feat/chain-flow-v2 — genesis-style forgiveness):
#   - Three REJECT tests became REPAIR tests, matching the new forgiveness
#     passes: `test_repairs_no_terminal_by_making_last_step_terminal`,
#     `test_repairs_dead_end_select_as_last_step`,
#     `test_repairs_terminal_with_no_complete_and_no_default` — these sloppy
#     descriptors now BUILD (repaired) instead of erroring. Added
#     `test_rejects_dead_end_select_in_the_middle` so the genuine-bug guard (a
#     select dead-end that ISN'T the last step) stays covered.
#   - Added a §2.0 forgiveness section: alias coercion (terminal `type:`/`kind:`
#     → `action:`, action `type:` → `verb:`), explicit-action-wins, no-caller-
#     mutation, and friendly-parse-error tests (named fix, no Pydantic URL).
#   - Added skeleton guards: every copy-paste skeleton in the inline prompt
#     builds clean, and the three action skeletons wire a `call_binding` button.
#   - The tool-level dead-end test became `test_tool_surfaces_flow_build_error_
#     for_real_bug` (dangling transition — un-repairable) plus
#     `test_tool_repairs_dead_end_last_select` (repaired through the tool).
# Updated: 2026-06-15 (feat/chain-flow-v2 — continuation-not-a-button fix):
#   - De-papered the two tests that had added dummy `id`/`label` to their
#     on_success/on_error continuations: `_example_b()` (now verbatim §1.7 with
#     BARE continuations) and `test_step_action_lowers_to_button_with_verb_on_click`.
#     These now build with the natural `{verb, …payload}` continuation shape.
#   - Strengthened `test_worked_example_b_builds_clean` to assert the lowered
#     invoke_tool button carries its bare on_success/on_error handlers and the
#     terminal onComplete is the create_pocket-with-then.
#   - Added `test_continuation_actions_need_no_button_id_or_label` — the explicit
#     regression guard that the §1.3/§1.7 bare-continuation shape builds without
#     FlowBuildError.
#
# Tests for the CHAIN FLOW v2 GENERAL builder — `build_flow_from_descriptor`
# (`pocketpaw.ripple._flows`). Where `test_flow_authoring.py` proves the two
# PRESETS still emit their exact pre-v2 tree (back-compat), this file proves the
# generalization: an ARBITRARY flat step-graph descriptor materializes into a
# valid nested tree, and every graph defect is REJECTED with a precise,
# agent-readable message.
#
# Test philosophy (TDD): the REJECT cases come first — they are the bug-class
# the primitive exists to prevent ("renders step 1 and silently dead-ends").
# Each §2.3 graph invariant, the §2.5 prefill-ref rules, the §2.4 action
# invariants, the §1.5/§2.2 suffix rewrite, the §7 D4 byte-identical button
# shapes, and the two worked examples (A + B) are exercised.
#
# No EE imports — runs in the OSS-only CI scope.

from __future__ import annotations

import json
from typing import Any

import pytest

from pocketpaw.ripple._flows import (
    FlowBuildError,
    build_flow_from_descriptor,
)
from pocketpaw.ripple.manifest import validate_action_verbs, validate_against_catalog
from pocketpaw.tools.builtin.flow_tool import StartFlowTool

ALLOWED_TYPES = ["container", "heading", "text", "input", "button"]


# ---------------------------------------------------------------------------
# Tree-walk helpers (mirror test_flow_authoring's, kept local so the two files
# stay independent).
# ---------------------------------------------------------------------------


def _iter_steps(step: dict[str, Any]) -> list[dict[str, Any]]:
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


def _walk_nodes(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        out.append(node)
        for child in node.get("children") or []:
            out.extend(_walk_nodes(child))
    return out


def _is_terminal(step: dict[str, Any]) -> bool:
    return "chain" not in step and "chain_map" not in step


def _on_clicks(step: dict[str, Any], target: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in _walk_nodes(step["ui"]):
        handler = node.get("on_click")
        if isinstance(handler, dict) and handler.get("target") == target:
            out.append(handler)
    return out


def _all_text_blobs(node: Any) -> list[str]:
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


# A minimal valid 3-step branching flow — the fixture most reject tests mutate.
def _good_descriptor() -> dict[str, Any]:
    return {
        "flow": "demo",
        "entry": "pick",
        "title": "Demo",
        "steps": [
            {
                "id": "pick",
                "kind": "select",
                "title": "Pick one",
                "options": [
                    {"id": "a", "label": "Option A"},
                    {"id": "b", "label": "Option B"},
                ],
                "branch": {"a": "form_a", "b": "form_b"},
            },
            {
                "id": "form_a",
                "kind": "form",
                "slot": "details",
                "title": "Details A",
                "fields": [{"id": "name", "label": "Name", "type": "text", "required": True}],
                "next": "done",
            },
            {
                "id": "form_b",
                "kind": "form",
                "slot": "details",
                "title": "Details B",
                "fields": [{"id": "name", "label": "Name", "type": "text", "required": True}],
                "next": "done",
            },
            {
                "id": "done",
                "kind": "confirm",
                "title": "Confirm",
                "review": [
                    {"label": "Choice", "value": "{pick.label}"},
                    {"label": "Name", "value": "{details.name}"},
                ],
                "complete": {"action": "chat", "message": "All set, please proceed."},
            },
        ],
    }


# ===========================================================================
# REJECT CASES (TDD — the bug-class). Each §2.3 invariant rejects with the
# right message.
# ===========================================================================


def test_rejects_missing_entry() -> None:
    d = _good_descriptor()
    d["entry"] = "nonexistent"
    with pytest.raises(FlowBuildError, match=r'entry "nonexistent" is not a step id'):
        build_flow_from_descriptor(d)


def test_rejects_dangling_next() -> None:
    d = _good_descriptor()
    d["steps"][1]["next"] = "ghost"
    with pytest.raises(
        FlowBuildError, match=r'\.next → "ghost" but "ghost" is not a declared step'
    ):
        build_flow_from_descriptor(d)


def test_rejects_dangling_branch_target() -> None:
    d = _good_descriptor()
    d["steps"][0]["branch"]["a"] = "ghost"
    with pytest.raises(
        FlowBuildError, match=r'\.branch\["a"\] → "ghost" but "ghost" is not declared'
    ):
        build_flow_from_descriptor(d)


def test_rejects_branch_key_not_an_option_id() -> None:
    d = _good_descriptor()
    # add a branch key that is not one of pick's option ids
    d["steps"][0]["branch"]["c"] = "form_a"
    with pytest.raises(FlowBuildError, match=r'branch key "c" is not one of pick\'s option ids'):
        build_flow_from_descriptor(d)


def test_repairs_no_terminal_by_making_last_step_terminal() -> None:
    # GENESIS FORGIVENESS: every step transitions onward (no terminal). The
    # repair pass makes the LAST-declared step terminal (drops its transition)
    # and gives it a default complete, so the flow BUILDS instead of erroring.
    # (Dropping the last transition also breaks the a→b→a cycle.)
    d = {
        "flow": "loopy",
        "entry": "a",
        "steps": [
            {"id": "a", "kind": "info", "title": "A", "next": "b"},
            {"id": "b", "kind": "info", "title": "B", "next": "a"},
        ],
    }
    doc = build_flow_from_descriptor(d)  # must NOT raise
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["flowId"] == "b"  # the last step became the terminal
    assert term["onComplete"]["kind"] == "chat"  # default complete injected


def test_rejects_terminal_with_transition() -> None:
    # a step carrying BOTH a transition and a complete action.
    d = _good_descriptor()
    d["steps"][1]["complete"] = {"action": "chat", "message": "early?"}
    with pytest.raises(
        FlowBuildError,
        match=r"has both a transition and a complete action; complete is terminal-only",
    ):
        build_flow_from_descriptor(d)


def test_rejects_orphan_unreachable_step() -> None:
    d = _good_descriptor()
    d["steps"].append(
        {
            "id": "orphan",
            "kind": "info",
            "title": "Orphan",
            "complete": {"action": "chat", "message": "x"},
        }
    )
    with pytest.raises(FlowBuildError, match=r'step "orphan" is unreachable from entry "pick"'):
        build_flow_from_descriptor(d)


def test_rejects_cycle() -> None:
    # a -> b -> c -> b is a cycle; c is also a select dead-end guard? no, use forms.
    d = {
        "flow": "cyc",
        "entry": "a",
        "steps": [
            {"id": "a", "kind": "info", "title": "A", "next": "b"},
            {"id": "b", "kind": "info", "title": "B", "next": "c"},
            {"id": "c", "kind": "info", "title": "C", "next": "b"},
            {
                "id": "end",
                "kind": "confirm",
                "title": "End",
                "complete": {"action": "chat", "message": "done"},
            },
        ],
    }
    # 'end' is unreachable; make it reachable so the cycle check is what fires.
    d["steps"][0]["next"] = "b"
    d["steps"][2]["next"] = "b"  # c -> b back-edge
    # add a path to end so 'no terminal' doesn't fire first: branch a
    d["steps"][0] = {
        "id": "a",
        "kind": "select",
        "title": "A",
        "options": [{"id": "go", "label": "Go"}, {"id": "fin", "label": "Fin"}],
        "branch": {"go": "b", "fin": "end"},
    }
    with pytest.raises(FlowBuildError, match=r"cycle detected:.*flows must be a DAG"):
        build_flow_from_descriptor(d)


def test_repairs_dead_end_select_as_last_step() -> None:
    # GENESIS FORGIVENESS: a select that is the LAST step and goes nowhere is no
    # longer a hard reject — the repair pass converts it to a terminal step with
    # a default complete (and assemble gives it a Finish button), so it BUILDS.
    d = {
        "flow": "deadend",
        "entry": "pick",
        "steps": [
            {
                "id": "pick",
                "kind": "select",
                "title": "Pick (goes nowhere)",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                # NO branch, NO next — the repair pass ends the flow here.
            },
        ],
    }
    doc = build_flow_from_descriptor(d)  # must NOT raise
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["flowId"] == "pick"
    assert term["onComplete"]["kind"] == "chat"
    # the terminal select carries a Finish (flow.submit) so the pick can complete
    assert len(_on_clicks(term, "flow.submit")) == 1


def test_rejects_dead_end_select_in_the_middle() -> None:
    # The guard STILL fires for a genuine bug: a select that dead-ends in the
    # MIDDLE of a flow (not the last step) is unreachable-onward — repair only
    # rescues a LAST dead-end select, so this is a precise reject.
    d = {
        "flow": "middead",
        "entry": "pick",
        "steps": [
            {
                "id": "pick",
                "kind": "select",
                "title": "Pick (goes nowhere)",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                # NO branch, NO next — but NOT the last step.
            },
            {
                "id": "tail",
                "kind": "confirm",
                "title": "Tail",
                "complete": {"action": "chat", "message": "ok"},
            },
        ],
    }
    with pytest.raises(
        FlowBuildError,
        match=r'select step "pick" has no branch/next; it would dead-end after a pick',
    ):
        build_flow_from_descriptor(d)


def test_rejects_both_next_and_branch() -> None:
    d = _good_descriptor()
    d["steps"][0]["next"] = "done"  # already has branch
    with pytest.raises(FlowBuildError, match=r"has both `next` and `branch`"):
        build_flow_from_descriptor(d)


def test_rejects_duplicate_step_id() -> None:
    d = _good_descriptor()
    d["steps"].append(dict(d["steps"][1]))  # duplicate form_a id
    with pytest.raises(FlowBuildError, match=r'duplicate step id "form_a"'):
        build_flow_from_descriptor(d)


def test_rejects_over_step_cap() -> None:
    steps = [
        {"id": f"s{i}", "kind": "info", "title": f"S{i}", "next": f"s{i + 1}"} for i in range(31)
    ]
    steps.append(
        {
            "id": "s31",
            "kind": "confirm",
            "title": "End",
            "complete": {"action": "chat", "message": "done"},
        }
    )
    d = {"flow": "huge", "entry": "s0", "steps": steps}
    with pytest.raises(FlowBuildError, match=r"over the 30-step cap; split the flow"):
        build_flow_from_descriptor(d)


def test_rejects_empty_steps() -> None:
    with pytest.raises(FlowBuildError, match=r"flow has no steps"):
        build_flow_from_descriptor({"flow": "x", "entry": "a", "steps": []})


# ===========================================================================
# §2.0 — GENESIS FORGIVENESS: alias normalization + friendly parse errors.
# A model's instinctive key slips (type:/kind:) are coerced; a shape defect
# that survives names the fix instead of leaking a Pydantic URL.
# ===========================================================================


def test_terminal_complete_type_alias_coerces_to_action() -> None:
    # Models write `type:` out of node-authoring habit; the builder coerces a
    # terminal `complete` written with `type:` → `action:` so it BUILDS.
    d = {
        "flow": "f",
        "entry": "a",
        "steps": [
            {
                "id": "a",
                "kind": "form",
                "title": "F",
                "fields": [{"id": "x", "label": "X", "type": "text"}],
                "next": "b",
            },
            {
                "id": "b",
                "kind": "confirm",
                "title": "Done",
                "review": [{"label": "X", "value": "{a.x}"}],
                "complete": {"type": "chat", "message": "done via type slip"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)  # must NOT raise on the type: slip
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["onComplete"]["kind"] == "chat"
    assert term["onComplete"]["message"] == "done via type slip"


def test_terminal_complete_kind_alias_coerces_to_action() -> None:
    d = {
        "flow": "f",
        "entry": "b",
        "steps": [
            {
                "id": "b",
                "kind": "confirm",
                "title": "Done",
                "complete": {"kind": "navigate", "url": "/somewhere"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["onComplete"]["kind"] == "navigate"
    assert term["onComplete"]["url"] == "/somewhere"


def test_step_action_type_alias_coerces_to_verb() -> None:
    # A mid-flow action written with `type:` instead of `verb:` is coerced.
    d = {
        "flow": "f",
        "entry": "a",
        "steps": [
            {
                "id": "a",
                "kind": "form",
                "title": "F",
                "fields": [{"id": "d", "label": "Domain", "type": "url", "required": True}],
                "actions": [
                    {
                        "id": "verify",
                        "label": "Verify",
                        "type": "call_binding",  # should coerce to verb
                        "binding": "dns",
                        "path": "/check",
                    }
                ],
                "next": "b",
            },
            {
                "id": "b",
                "kind": "confirm",
                "title": "Done",
                "complete": {"action": "chat", "message": "ok"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)  # must NOT raise on the action type: slip
    details = next(s for s in _iter_steps(doc["ui"]) if s["flowId"] == "a")
    verify = next(
        n for n in _walk_nodes(details["ui"]) if n.get("props", {}).get("label") == "Verify"
    )
    assert verify["on_click"]["action"] == "call_binding"
    assert verify["on_click"]["binding"] == "dns"


def test_explicit_action_wins_over_alias() -> None:
    # If BOTH `action` and a `type:`/`kind:` alias are present, the explicit
    # `action` is authoritative — the alias is not allowed to clobber it.
    d = {
        "flow": "f",
        "entry": "b",
        "steps": [
            {
                "id": "b",
                "kind": "confirm",
                "title": "Done",
                "complete": {"action": "chat", "type": "navigate", "message": "stay chat"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["onComplete"]["kind"] == "chat"


def test_does_not_mutate_caller_descriptor() -> None:
    # Normalization deep-copies — the caller's dict is untouched after a build
    # (so a retry sees the original input).
    d = {
        "flow": "f",
        "entry": "b",
        "steps": [
            {
                "id": "b",
                "kind": "confirm",
                "title": "Done",
                "complete": {"type": "chat", "message": "x"},
            },
        ],
    }
    build_flow_from_descriptor(d)
    # the original still carries `type`, not the coerced `action`
    assert d["steps"][0]["complete"] == {"type": "chat", "message": "x"}


def test_friendly_error_for_complete_missing_action() -> None:
    # A `complete` that is present but missing `action` (and no type:/kind: to
    # coerce) raises a FRIENDLY error naming the fix — never a Pydantic URL.
    d = {
        "flow": "f",
        "entry": "b",
        "steps": [
            {
                "id": "b",
                "kind": "confirm",
                "title": "Done",
                "complete": {"message": "no action key"},
            },
        ],
    }
    with pytest.raises(FlowBuildError) as exc:
        build_flow_from_descriptor(d)
    msg = str(exc.value)
    assert "action" in msg
    assert "chat" in msg and "call_binding" in msg  # names the allowed actions
    assert "errors.pydantic.dev" not in msg  # no leaked Pydantic URL


def test_friendly_error_for_bad_kind() -> None:
    d = {
        "flow": "f",
        "entry": "b",
        "steps": [
            {
                "id": "b",
                "kind": "wizard",  # not a valid kind
                "title": "Done",
                "complete": {"action": "chat", "message": "x"},
            },
        ],
    }
    with pytest.raises(FlowBuildError) as exc:
        build_flow_from_descriptor(d)
    msg = str(exc.value)
    assert "kind" in msg
    assert "errors.pydantic.dev" not in msg


# ===========================================================================
# The inline-prompt skeletons (the copy-paste examples the model is taught)
# all BUILD clean — a guard that the prompt never ships an un-buildable example.
# ===========================================================================


def test_inline_prompt_skeletons_build_clean() -> None:
    import re

    from pocketpaw.ripple._inline import _MULTI_STEP_FLOW_RULE

    blocks = re.findall(r"```\n(\{.*?\})\n```", _MULTI_STEP_FLOW_RULE, re.DOTALL)
    # Skeletons A + B (existing) + C/D/E (approve, fulfill, act-on-data) = 5.
    assert len(blocks) == 5, f"expected 5 skeleton blocks in the rule, found {len(blocks)}"
    for i, raw in enumerate(blocks):
        descriptor = json.loads(raw)  # must be valid JSON
        doc = build_flow_from_descriptor(descriptor)  # must BUILD
        assert doc["version"] == "1.0", f"skeleton {i} ({descriptor.get('flow')}) bad envelope"
        assert validate_against_catalog(doc, ALLOWED_TYPES) == [], (
            f"skeleton {i} ({descriptor.get('flow')}) has catalog violations"
        )
        assert validate_action_verbs(doc) == [], (
            f"skeleton {i} ({descriptor.get('flow')}) has verb violations"
        )


def test_action_flow_skeletons_use_call_binding_not_select() -> None:
    # The genesis lever: an APPROVE/FULFILL/ACT flow is a call_binding ACTION
    # BUTTON, never a yes/no select. Assert the three action skeletons carry a
    # call_binding action button (proving the prompt teaches the right pattern).
    import re

    from pocketpaw.ripple._inline import _MULTI_STEP_FLOW_RULE

    blocks = re.findall(r"```\n(\{.*?\})\n```", _MULTI_STEP_FLOW_RULE, re.DOTALL)
    by_flow = {json.loads(b)["flow"]: json.loads(b) for b in blocks}
    for flow_name in ("approve_request", "fulfill_order", "act_on_item"):
        assert flow_name in by_flow, f"missing action skeleton {flow_name!r}"
        descriptor = by_flow[flow_name]
        verbs = {a.get("verb") for step in descriptor["steps"] for a in (step.get("actions") or [])}
        assert "call_binding" in verbs, (
            f"action skeleton {flow_name!r} must wire a call_binding action button"
        )


# ===========================================================================
# §2.5 prefill-ref validation.
# ===========================================================================


def test_rejects_prefill_ref_to_unknown_step() -> None:
    d = _good_descriptor()
    d["steps"][3]["review"][0]["value"] = "{nosuchstep.label}"
    with pytest.raises(
        FlowBuildError, match=r"prefill ref \{nosuchstep\.label\} points at unknown step/slot"
    ):
        build_flow_from_descriptor(d)


def test_rejects_prefill_ref_to_undeclared_field() -> None:
    d = _good_descriptor()
    # `details` slot form declares `name`, not `salary`.
    d["steps"][3]["review"][1]["value"] = "{details.salary}"
    with pytest.raises(
        FlowBuildError,
        match=r'prefill ref \{details\.salary\} — step "details" declares no field "salary"',
    ):
        build_flow_from_descriptor(d)


def test_single_branch_reachability_is_a_warning_not_error() -> None:
    # A review ref to a per-step-id field reachable on only ONE branch is a
    # WARNING (renders empty on the other branch), not a hard error. Use
    # distinct step ids (no shared slot) so the ref is single-branch.
    d = {
        "flow": "warn",
        "entry": "pick",
        "steps": [
            {
                "id": "pick",
                "kind": "select",
                "title": "Pick",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "branch": {"a": "fa", "b": "fb"},
            },
            {
                "id": "fa",
                "kind": "form",
                "title": "FA",
                "fields": [{"id": "x", "label": "X", "type": "text"}],
                "next": "rev",
            },
            {
                "id": "fb",
                "kind": "form",
                "title": "FB",
                "fields": [{"id": "y", "label": "Y", "type": "text"}],
                "next": "rev",
            },
            {
                "id": "rev",
                "kind": "confirm",
                "title": "Rev",
                "review": [{"label": "X", "value": "{fa.x}"}],  # fa only on branch 'a'
                "complete": {"action": "chat", "message": "ok"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)  # must NOT raise
    warnings = doc.get("_warnings") or []
    assert any("only reachable on" in w for w in warnings), (
        f"expected a single-branch-reachability warning, got: {warnings}"
    )


# ===========================================================================
# §1.5 / §2.2 — the suffix rewrite is correct (the #1 hand-author bug).
# ===========================================================================


def test_suffix_rewrite_select_becomes_selection() -> None:
    doc = build_flow_from_descriptor(_good_descriptor())
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    rows = {r["label"]: r["value"] for r in term["review_rows"]}
    # {pick.label} (a select step) → {state.pick_selection.label}
    assert rows["Choice"] == "{state.pick_selection.label}"


def test_suffix_rewrite_form_becomes_formData_via_slot() -> None:
    doc = build_flow_from_descriptor(_good_descriptor())
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    rows = {r["label"]: r["value"] for r in term["review_rows"]}
    # {details.name} (a form slot) → {state.details_formData.name}
    assert rows["Name"] == "{state.details_formData.name}"


def test_passthrough_tokens_not_rewritten() -> None:
    # {flow.payload} / {state.x} / {result.y} must pass through untouched.
    d = _good_descriptor()
    d["steps"][3]["review"][0]["value"] = "{flow.payload}"
    d["steps"][3]["review"][1]["value"] = "{state.custom_key}"
    doc = build_flow_from_descriptor(d)
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    values = {r["value"] for r in term["review_rows"]}
    assert "{flow.payload}" in values
    assert "{state.custom_key}" in values


# ===========================================================================
# §1.3 / §7 D4 — action lowering emits the right button on_click.
# ===========================================================================


def test_step_action_lowers_to_button_with_verb_on_click() -> None:
    d = {
        "flow": "actrich",
        "entry": "f",
        "steps": [
            {
                "id": "f",
                "kind": "form",
                "title": "Form",
                "fields": [{"id": "domain", "label": "Domain", "type": "url", "required": True}],
                "actions": [
                    {
                        "id": "verify",
                        "label": "Verify domain",
                        "verb": "call_binding",
                        "binding": "dns_check",
                        "path": "/dns/check",
                        "params": {"domain": "{f.domain}"},
                        # Bare continuations (no id/label) — the §1.3 shape the
                        # builder accepts after the continuation-not-a-button fix.
                        "on_success": [
                            {
                                "verb": "toast",
                                "message": "Reachable",
                                "variant": "success",
                            }
                        ],
                        "on_error": [
                            {
                                "verb": "toast",
                                "message": "Unreachable",
                                "variant": "warning",
                            }
                        ],
                    }
                ],
                "next": "done",
            },
            {
                "id": "done",
                "kind": "confirm",
                "title": "Done",
                "complete": {"action": "chat", "message": "ok"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)
    form = doc["ui"]
    # find the action button
    btns = [
        n
        for n in _walk_nodes(form["ui"])
        if n.get("type") == "button" and n.get("props", {}).get("label") == "Verify domain"
    ]
    assert len(btns) == 1
    oc = btns[0]["on_click"]
    assert oc["action"] == "call_binding"
    assert oc["binding"] == "dns_check"
    assert oc["path"] == "/dns/check"
    # the {f.domain} ref rewrote to {state.f_formData.domain}
    assert oc["params"]["domain"] == "{state.f_formData.domain}"
    # continuations lowered to bare handler dicts
    assert oc["on_success"][0]["action"] == "toast"
    assert oc["on_success"][0]["variant"] == "success"
    assert oc["on_error"][0]["action"] == "toast"


def test_rejects_unknown_step_action_verb() -> None:
    d = {
        "flow": "bad",
        "entry": "f",
        "steps": [
            {
                "id": "f",
                "kind": "form",
                "title": "F",
                "fields": [{"id": "a", "label": "A", "type": "text"}],
                "actions": [{"id": "x", "label": "X", "verb": "teleport"}],
                "next": "done",
            },
            {
                "id": "done",
                "kind": "confirm",
                "title": "Done",
                "complete": {"action": "chat", "message": "ok"},
            },
        ],
    }
    with pytest.raises(FlowBuildError, match=r'uses unknown verb "teleport"'):
        build_flow_from_descriptor(d)


def test_rejects_step_action_missing_required_key() -> None:
    d = {
        "flow": "bad",
        "entry": "f",
        "steps": [
            {
                "id": "f",
                "kind": "form",
                "title": "F",
                "fields": [{"id": "a", "label": "A", "type": "text"}],
                # invoke_tool requires `tool`
                "actions": [{"id": "x", "label": "X", "verb": "invoke_tool"}],
                "next": "done",
            },
            {
                "id": "done",
                "kind": "confirm",
                "title": "Done",
                "complete": {"action": "chat", "message": "ok"},
            },
        ],
    }
    with pytest.raises(FlowBuildError, match=r'is missing required key "tool"'):
        build_flow_from_descriptor(d)


# ===========================================================================
# §1.4 / §7 D1 — terminal lowering: new kinds + `then`.
# ===========================================================================


def test_terminal_create_pocket_lowers_with_then() -> None:
    d = {
        "flow": "setup",
        "entry": "details",
        "steps": [
            {
                "id": "details",
                "kind": "form",
                "title": "Details",
                "fields": [{"id": "company", "label": "Company", "type": "text", "required": True}],
                "next": "review",
            },
            {
                "id": "review",
                "kind": "confirm",
                "title": "Confirm",
                "review": [{"label": "Company", "value": "{details.company}"}],
                "complete": {
                    "action": "create_pocket",
                    "name": "{details.company} — Client",
                    "template": "tracker",
                    "seed_from_flow": True,
                    "then": {"action": "navigate", "url": "/pockets/{result.id}"},
                },
            },
        ],
    }
    doc = build_flow_from_descriptor(d)
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    oc = term["onComplete"]
    assert oc["kind"] == "create_pocket"
    assert oc["name"] == "{state.details_formData.company} — Client"
    assert oc["template"] == "tracker"
    assert oc["seed_from_flow"] is True
    assert oc["then"]["kind"] == "navigate"
    # {result.id} is a passthrough — not rewritten
    assert oc["then"]["url"] == "/pockets/{result.id}"


def test_terminal_invoke_tool_lowers() -> None:
    d = {
        "flow": "tooly",
        "entry": "f",
        "steps": [
            {
                "id": "f",
                "kind": "form",
                "title": "F",
                "fields": [{"id": "vat", "label": "VAT", "type": "text", "required": True}],
                "next": "done",
            },
            {
                "id": "done",
                "kind": "confirm",
                "title": "Done",
                "complete": {
                    "action": "invoke_tool",
                    "tool": "file_vendor",
                    "args": {"vat": "{f.vat}"},
                },
            },
        ],
    }
    doc = build_flow_from_descriptor(d)
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    oc = term["onComplete"]
    assert oc["kind"] == "invoke_tool"
    assert oc["tool"] == "file_vendor"
    assert oc["args"]["vat"] == "{state.f_formData.vat}"


def test_rejects_terminal_unknown_action() -> None:
    d = _good_descriptor()
    d["steps"][3]["complete"] = {"action": "self_destruct"}
    with pytest.raises(FlowBuildError, match=r'uses unknown action "self_destruct"'):
        build_flow_from_descriptor(d)


def test_rejects_terminal_then_too_deep() -> None:
    d = _good_descriptor()
    d["steps"][3]["complete"] = {
        "action": "chat",
        "message": "a",
        "then": {
            "action": "chat",
            "message": "b",
            "then": {"action": "chat", "message": "c"},
        },  # depth 3 > cap 2
    }
    with pytest.raises(FlowBuildError, match=r"chains `then` deeper than 2"):
        build_flow_from_descriptor(d)


def test_flow_level_default_complete_applies_to_terminal() -> None:
    # a terminal without its own complete inherits the flow-level default.
    d = {
        "flow": "def",
        "entry": "f",
        "title": "Def",
        "steps": [
            {
                "id": "f",
                "kind": "form",
                "title": "F",
                "fields": [{"id": "a", "label": "A", "type": "text"}],
                "next": "end",
            },
            {
                "id": "end",
                "kind": "confirm",
                "title": "End",
                "review": [{"label": "A", "value": "{f.a}"}],
            },  # no own complete
        ],
        "complete": {"action": "chat", "message": "flow default fired"},
    }
    doc = build_flow_from_descriptor(d)
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["onComplete"]["kind"] == "chat"
    assert term["onComplete"]["message"] == "flow default fired"


# ===========================================================================
# §7 D3 — the GENERAL path allows MULTIPLE terminals, each with its own
# complete (the presets keep exactly one; this is the general allowance).
# ===========================================================================


def test_general_path_allows_multiple_terminals() -> None:
    d = {
        "flow": "multi",
        "entry": "pick",
        "steps": [
            {
                "id": "pick",
                "kind": "select",
                "title": "Branch",
                "options": [{"id": "x", "label": "X"}, {"id": "y", "label": "Y"}],
                "branch": {"x": "end_x", "y": "end_y"},
            },
            {
                "id": "end_x",
                "kind": "confirm",
                "title": "End X",
                "complete": {"action": "chat", "message": "ended on X"},
            },
            {
                "id": "end_y",
                "kind": "confirm",
                "title": "End Y",
                "complete": {"action": "navigate", "url": "/y"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)  # must NOT raise on 2 terminals
    terminals = [s for s in _iter_steps(doc["ui"]) if _is_terminal(s)]
    assert len(terminals) == 2
    kinds = {t["onComplete"]["kind"] for t in terminals}
    assert kinds == {"chat", "navigate"}


def test_repairs_terminal_with_no_complete_and_no_default() -> None:
    # GENESIS FORGIVENESS: a terminal step with no `complete` AND no flow-level
    # default no longer errors — the repair pass injects a default `chat`
    # complete so the flow BUILDS with a sensible hand-off.
    d = {
        "flow": "nocomplete",
        "entry": "f",
        "steps": [
            {
                "id": "f",
                "kind": "form",
                "title": "F",
                "fields": [{"id": "a", "label": "A", "type": "text"}],
                "next": "end",
            },
            {"id": "end", "kind": "confirm", "title": "End"},  # no complete, no default
        ],
    }
    doc = build_flow_from_descriptor(d)  # must NOT raise
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["flowId"] == "end"
    assert term["onComplete"]["kind"] == "chat"
    assert term["onComplete"]["message"]  # a non-empty default message


# ===========================================================================
# Worked examples A + B build clean (§1.6, §1.7).
# ===========================================================================


def _example_a() -> dict[str, Any]:
    """The branching due-diligence-style intake from §1.6 (uses a shared slot so
    the review ref is branch-agnostic — the spec note explains the preset does
    this; we mirror it here so example A builds without a single-branch warning)."""
    return {
        "flow": "diligence_intake",
        "entry": "stage",
        "title": "Due-diligence intake",
        "steps": [
            {
                "id": "stage",
                "kind": "select",
                "title": "Pick the deal stage",
                "subtitle": "The next steps adapt to it.",
                "options": [
                    {"id": "early", "label": "Early stage (pre-seed / seed)"},
                    {"id": "growth", "label": "Growth stage (Series A+)"},
                ],
                "branch": {"early": "fin_early", "growth": "fin_growth"},
            },
            {
                "id": "fin_early",
                "kind": "form",
                "slot": "financials",
                "title": "Early-stage traction",
                "fields": [
                    {
                        "id": "headline",
                        "label": "Headline traction metric",
                        "type": "text",
                        "placeholder": "1.2k weekly active",
                        "required": True,
                    },
                    {
                        "id": "raise",
                        "label": "Round size sought",
                        "type": "text",
                        "placeholder": "$2M seed",
                    },
                ],
                "next": "risk",
            },
            {
                "id": "fin_growth",
                "kind": "form",
                "slot": "financials",
                "title": "Growth metrics",
                "fields": [
                    {
                        "id": "headline",
                        "label": "Headline revenue metric",
                        "type": "text",
                        "placeholder": "$4M ARR",
                        "required": True,
                    },
                    {"id": "growth", "label": "YoY growth", "type": "text", "placeholder": "180%"},
                ],
                "next": "risk",
            },
            {
                "id": "risk",
                "kind": "form",
                "slot": "risk",
                "title": "Risk & open flags",
                "fields": [
                    {
                        "id": "key_risk",
                        "label": "Biggest open risk",
                        "type": "text",
                        "placeholder": "Customer concentration",
                        "required": True,
                    },
                    {
                        "id": "mitigation",
                        "label": "Mitigation (optional)",
                        "type": "textarea",
                        "required": False,
                    },
                ],
                "next": "review",
            },
            {
                "id": "review",
                "kind": "confirm",
                "title": "Review the intake",
                "review": [
                    {"label": "Deal stage", "value": "{stage.label}"},
                    {"label": "Headline metric", "value": "{financials.headline}"},
                    {"label": "Key risk", "value": "{risk.key_risk}"},
                ],
                "complete": {
                    "action": "chat",
                    "message": "Diligence intake complete — please summarize and flag risks.",
                },
            },
        ],
    }


def _example_b() -> dict[str, Any]:
    """The action-rich 'stand up a client workspace' mini-app from §1.7."""
    return {
        "flow": "client_setup",
        "entry": "plan",
        "title": "Set up a client workspace",
        "steps": [
            {
                "id": "plan",
                "kind": "select",
                "title": "Plan",
                "options": [{"id": "starter", "label": "Starter"}, {"id": "pro", "label": "Pro"}],
                "ui": {"layout": "cards", "columns": 2},
                "next": "details",
            },
            {
                "id": "details",
                "kind": "form",
                "title": "Company details",
                "fields": [
                    {"id": "company", "label": "Company name", "type": "text", "required": True},
                    {
                        "id": "domain",
                        "label": "Primary domain",
                        "type": "url",
                        "required": True,
                        "placeholder": "acme.com",
                    },
                    {"id": "seats", "label": "Seats", "type": "number", "required": True},
                ],
                "actions": [
                    # Verbatim from design §1.7: the continuations are BARE
                    # (verb + payload, NO id/label) — they are NOT buttons. This
                    # is the regression guard for the continuation-not-a-button
                    # fix; before it, the required id/label on StepAction made
                    # the build raise FlowBuildError on this naturally-authored
                    # shape.
                    {
                        "id": "verify_domain",
                        "label": "Verify domain",
                        "verb": "invoke_tool",
                        "tool": "dns_domain_check",
                        "args": {"domain": "{details.domain}"},
                        "on_success": [
                            {
                                "verb": "toast",
                                "message": "Domain reachable",
                                "variant": "success",
                            },
                            {
                                "verb": "set",
                                "key": "details.domain_ok",
                                "value": True,
                            },
                        ],
                        "on_error": [
                            {
                                "verb": "toast",
                                "message": "Domain not reachable",
                                "variant": "warning",
                            },
                        ],
                    }
                ],
                "next": "review",
            },
            {
                "id": "review",
                "kind": "confirm",
                "title": "Confirm and create the workspace",
                "review": [
                    {"label": "Plan", "value": "{plan.label}"},
                    {"label": "Company", "value": "{details.company}"},
                    {"label": "Domain", "value": "{details.domain}"},
                    {"label": "Seats", "value": "{details.seats}"},
                ],
                "complete": {
                    "action": "create_pocket",
                    "name": "{details.company} — Client",
                    "template": "tracker",
                    "seed_from_flow": True,
                    "then": {"action": "navigate", "url": "/pockets/{result.id}"},
                },
            },
        ],
    }


def test_worked_example_a_builds_clean() -> None:
    doc = build_flow_from_descriptor(_example_a())
    assert doc["version"] == "1.0"
    assert isinstance(doc["ui"], dict)
    # no single-branch warning (shared slot makes the read branch-agnostic)
    assert not doc.get("_warnings"), f"example A should build warning-free: {doc.get('_warnings')}"
    # passes the deep validators
    assert validate_against_catalog(doc, ALLOWED_TYPES) == []
    assert validate_action_verbs(doc) == []
    # 5 logical steps; financials is shared so 6 step objects collapse appropriately
    flow_ids = {s["flowId"] for s in _iter_steps(doc["ui"])}
    assert flow_ids == {"stage", "financials", "risk", "review"}


def test_worked_example_b_builds_clean() -> None:
    # Builds clean — no FlowBuildError — even though the verify_domain action's
    # on_success/on_error continuations are bare (no id/label). This is the
    # literal §1.7 example B copied verbatim from the design doc; it is the
    # regression guard for the continuation-not-a-button fix.
    doc = build_flow_from_descriptor(_example_b())
    assert isinstance(doc["ui"], dict)
    assert validate_against_catalog(doc, ALLOWED_TYPES) == []
    assert validate_action_verbs(doc) == []

    # The form step carries the lowered invoke_tool button with its handlers.
    details = next(s for s in _iter_steps(doc["ui"]) if s["flowId"] == "details")
    verify = [
        n for n in _walk_nodes(details["ui"]) if n.get("props", {}).get("label") == "Verify domain"
    ]
    assert len(verify) == 1
    oc = verify[0]["on_click"]
    assert oc["action"] == "invoke_tool"
    assert oc["tool"] == "dns_domain_check"
    # the {details.domain} arg ref rewrote to the namespaced formData key
    assert oc["args"]["domain"] == "{state.details_formData.domain}"
    # on_success / on_error lowered to bare handler dicts (no id/label, no button
    # wrapper) — exactly two success continuations, one error continuation.
    assert [h["action"] for h in oc["on_success"]] == ["toast", "set"]
    assert oc["on_success"][0]["variant"] == "success"
    assert oc["on_success"][0]["message"] == "Domain reachable"
    # no id/label leaked into the lowered handler
    assert "id" not in oc["on_success"][0] and "label" not in oc["on_success"][0]
    # the set continuation preserves its falsy-safe boolean value
    set_conts = [h for h in oc["on_success"] if h["action"] == "set"]
    assert set_conts[0]["value"] is True
    assert set_conts[0]["key"] == "details.domain_ok"
    assert [h["action"] for h in oc["on_error"]] == ["toast"]
    assert oc["on_error"][0]["variant"] == "warning"

    # The terminal onComplete is the create_pocket-with-then.
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["onComplete"]["kind"] == "create_pocket"
    assert term["onComplete"]["name"] == "{state.details_formData.company} — Client"
    assert term["onComplete"]["seed_from_flow"] is True
    assert term["onComplete"]["then"]["kind"] == "navigate"
    assert term["onComplete"]["then"]["url"] == "/pockets/{result.id}"


def test_continuation_actions_need_no_button_id_or_label() -> None:
    """Regression: the §1.3/§1.7 continuation shape (bare `{verb, …payload}`
    with NO id/label) must BUILD, not raise.

    Continuations are NOT buttons. Before the fix, `StepAction.on_success` /
    `.on_error` were typed `list[StepAction]`, and `StepAction` requires
    `id` + `label`; the real chat agent — following the prompt + design — authors
    continuations WITHOUT id/label, so every naturally-authored chain hit
    `FlowBuildError`. This builds the LITERAL §1.7 example B (verbatim, bare
    continuations) and proves it materializes a clean tree.
    """
    # The descriptor's verify_domain continuations carry no id/label at all.
    desc = _example_b()
    verify = desc["steps"][1]["actions"][0]
    for cont in verify["on_success"] + verify["on_error"]:
        assert "id" not in cont, f"continuation should be authored bare: {cont}"
        assert "label" not in cont, f"continuation should be authored bare: {cont}"

    # Must build with no FlowBuildError (the bug raised here).
    doc = build_flow_from_descriptor(desc)
    assert doc["version"] == "1.0"
    assert validate_action_verbs(doc) == []

    # The lowered invoke_tool button carries its on_success/on_error handlers,
    # each a bare action dict (verb + payload, no id/label survived lowering).
    details = next(s for s in _iter_steps(doc["ui"]) if s["flowId"] == "details")
    btn = next(
        n for n in _walk_nodes(details["ui"]) if n.get("props", {}).get("label") == "Verify domain"
    )
    handlers = btn["on_click"]["on_success"] + btn["on_click"]["on_error"]
    assert len(handlers) == 3
    for h in handlers:
        assert "action" in h and "id" not in h and "label" not in h

    # And the terminal is the create_pocket-with-then hand-off.
    term = next(s for s in _iter_steps(doc["ui"]) if _is_terminal(s))
    assert term["onComplete"]["kind"] == "create_pocket"
    assert term["onComplete"]["then"]["kind"] == "navigate"


# ===========================================================================
# An arbitrary 6-step branching flow round-trips.
# ===========================================================================


def test_arbitrary_six_step_flow_round_trips() -> None:
    d = {
        "flow": "sixstep",
        "entry": "s1",
        "title": "Six-step",
        "steps": [
            {
                "id": "s1",
                "kind": "select",
                "title": "Start",
                "options": [{"id": "left", "label": "Left"}, {"id": "right", "label": "Right"}],
                "branch": {"left": "s2", "right": "s3"},
            },
            {
                "id": "s2",
                "kind": "form",
                "slot": "data",
                "title": "Left form",
                "fields": [{"id": "v", "label": "Value", "type": "text", "required": True}],
                "next": "s4",
            },
            {
                "id": "s3",
                "kind": "form",
                "slot": "data",
                "title": "Right form",
                "fields": [{"id": "v", "label": "Value", "type": "text", "required": True}],
                "next": "s4",
            },
            {
                "id": "s4",
                "kind": "info",
                "title": "Interstitial",
                "body": "You entered {data.v}.",
                "next": "s5",
            },
            {
                "id": "s5",
                "kind": "form",
                "slot": "extra",
                "title": "More",
                "fields": [{"id": "note", "label": "Note", "type": "textarea", "required": False}],
                "next": "s6",
            },
            {
                "id": "s6",
                "kind": "confirm",
                "title": "Done",
                "review": [
                    {"label": "Value", "value": "{data.v}"},
                    {"label": "Note", "value": "{extra.note}"},
                ],
                "complete": {"action": "chat", "message": "six steps complete"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)
    steps = _iter_steps(doc["ui"])
    flow_ids = {s["flowId"] for s in steps}
    # s2/s3 share slot "data"; s4 info, s5 extra, s6 review
    assert flow_ids == {"s1", "data", "s4", "extra", "s6"}
    # diamond: both branches converge on the same s4 object
    s4_objs = [s for s in steps if s["flowId"] == "s4"]
    assert len(s4_objs) == 1
    # the info body ref rewrote
    info = s4_objs[0]
    body_blobs = _all_text_blobs(info["ui"])
    assert any("{state.data_formData.v}" in b for b in body_blobs)
    # passes deep validators
    assert validate_against_catalog(doc, ALLOWED_TYPES) == []
    assert validate_action_verbs(doc) == []


# ===========================================================================
# Diamond-join sharing: a branch-converging step is materialized ONCE.
# ===========================================================================


def test_diamond_join_shares_one_node_instance() -> None:
    doc = build_flow_from_descriptor(_good_descriptor())
    root = doc["ui"]
    a_term = root["chain_map"]["a"]["chain"]
    b_term = root["chain_map"]["b"]["chain"]
    # both branches' "done" step must be the SAME object (a materialized diamond)
    assert a_term is b_term


# ===========================================================================
# Linear (no-branch) flow also builds.
# ===========================================================================


def test_linear_flow_builds() -> None:
    d = {
        "flow": "linear",
        "entry": "a",
        "steps": [
            {"id": "a", "kind": "info", "title": "Intro", "body": "Welcome.", "next": "b"},
            {
                "id": "b",
                "kind": "form",
                "slot": "f",
                "title": "Fill",
                "fields": [{"id": "x", "label": "X", "type": "text", "required": True}],
                "next": "c",
            },
            {
                "id": "c",
                "kind": "confirm",
                "title": "Confirm",
                "review": [{"label": "X", "value": "{f.x}"}],
                "complete": {"action": "chat", "message": "linear done"},
            },
        ],
    }
    doc = build_flow_from_descriptor(d)
    steps = _iter_steps(doc["ui"])
    assert [s["flowId"] for s in steps] == ["a", "f", "c"]
    # intermediate form 'b' gets a Back button (not the entry, not terminal)
    form = steps[1]
    assert len(_on_clicks(form, "flow.back")) == 1
    # entry 'a' (info) gets no Back
    assert len(_on_clicks(steps[0], "flow.back")) == 0


# ===========================================================================
# The `start_flow` tool routes a flat `steps` graph through the general builder
# and surfaces FlowBuildError verbatim so the model can retry.
# ===========================================================================


@pytest.fixture
def tool() -> StartFlowTool:
    return StartFlowTool()


async def test_tool_builds_from_flat_steps_graph(tool: StartFlowTool) -> None:
    d = _good_descriptor()
    result = await tool.execute(
        flow=d["flow"], entry=d["entry"], title=d["title"], steps=d["steps"]
    )
    assert not result.startswith("Error:")
    doc = json.loads(result)
    assert doc["version"] == "1.0"
    assert isinstance(doc["ui"], dict)
    # the doc matches the builder's output (minus the internal _warnings key)
    built = build_flow_from_descriptor(d)
    built.pop("_warnings", None)
    assert doc == built


async def test_tool_surfaces_flow_build_error_for_real_bug(tool: StartFlowTool) -> None:
    # A GENUINE structural bug (a dangling `next` → an undeclared id) is NOT
    # repairable, so the tool must return the precise reject message verbatim so
    # the model can fix the graph and retry. (A dead-end LAST select is repaired
    # now — see test_repairs_dead_end_select_as_last_step — so the un-repairable
    # dangling-transition case is the right thing to assert the error path on.)
    result = await tool.execute(
        flow="bad",
        entry="pick",
        steps=[
            {
                "id": "pick",
                "kind": "select",
                "title": "Pick",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "branch": {"a": "ghost", "b": "ghost"},  # → undeclared step
            }
        ],
    )
    assert result.startswith("Error:")
    assert "ghost" in result and "not declared" in result


async def test_tool_repairs_dead_end_last_select(tool: StartFlowTool) -> None:
    # The flip side: a dead-end LAST select is REPAIRED by the builder, so the
    # tool returns a valid doc, not an Error (genesis forgiveness through the
    # tool boundary).
    result = await tool.execute(
        flow="ok",
        entry="pick",
        steps=[
            {
                "id": "pick",
                "kind": "select",
                "title": "Pick",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            }
        ],
    )
    assert not result.startswith("Error:")
    doc = json.loads(result)
    assert doc["version"] == "1.0"
    assert doc["ui"]["onComplete"]["kind"] == "chat"


async def test_tool_accepts_steps_as_json_string(tool: StartFlowTool) -> None:
    """Subprocess / CLI callers pass `steps` as a JSON string through the flat
    signature — the tool coerces it."""
    d = _good_descriptor()
    result = await tool.execute(flow=d["flow"], entry=d["entry"], steps=json.dumps(d["steps"]))
    assert not result.startswith("Error:")
    doc = json.loads(result)
    assert isinstance(doc["ui"], dict)


async def test_tool_preset_shorthand_still_works(tool: StartFlowTool) -> None:
    """`flow_type` remains an OPTIONAL preset path that returns a valid doc."""
    result = await tool.execute(flow_type="onboarding_wizard")
    assert not result.startswith("Error:")
    doc = json.loads(result)
    assert isinstance(doc["ui"], dict)


async def test_tool_no_steps_no_flow_type_is_agent_readable(tool: StartFlowTool) -> None:
    result = await tool.execute()
    assert result.startswith("Error:")
    assert "steps" in result and "flow_type" in result


async def test_tool_does_not_leak_warnings_into_doc(tool: StartFlowTool) -> None:
    """Single-branch-reachability warnings are build-time guidance, not part of
    the rendered spec — the tool strips `_warnings` from its output."""
    # distinct step ids (no shared slot) so a single-branch warning is raised
    result = await tool.execute(
        flow="warn",
        entry="pick",
        steps=[
            {
                "id": "pick",
                "kind": "select",
                "title": "Pick",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "branch": {"a": "fa", "b": "fb"},
            },
            {
                "id": "fa",
                "kind": "form",
                "title": "FA",
                "fields": [{"id": "x", "label": "X", "type": "text"}],
                "next": "rev",
            },
            {
                "id": "fb",
                "kind": "form",
                "title": "FB",
                "fields": [{"id": "y", "label": "Y", "type": "text"}],
                "next": "rev",
            },
            {
                "id": "rev",
                "kind": "confirm",
                "title": "Rev",
                "review": [{"label": "X", "value": "{fa.x}"}],
                "complete": {"action": "chat", "message": "ok"},
            },
        ],
    )
    doc = json.loads(result)
    assert "_warnings" not in doc
