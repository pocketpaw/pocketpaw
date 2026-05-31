# pocketpaw/ripple/_flows.py — Deterministic Chain Flow builders (RFC 13 §7.1, M3).
#
# Created: 2026-05-31 (RFC 13 M3, feat/m3-flow-authoring-tool).
#
# What this is:
#   The DETERMINISTIC half of the `start_flow` authoring tool. The LLM emits a
#   tiny descriptor — `flow_type` (an enum of known templates), an optional
#   `domain` hint, optional `config` overrides — and one of the builders below
#   expands a HARDCODED template into the full nested Chain Flow tree, then
#   returns it as a `{version, ui}` inline-Ripple doc.
#
# Why a builder owns the tree (not the model):
#   The genesis prototype's `preprocessChainStrings` repair code (RFC 13 §2.2)
#   is the evidence: hand-authoring a recursively-nested chain/chain_map tree
#   is fragile — weak models stringify the chain or wrap it in a
#   `{action:'chain', next:{…}}` shape. So the model never sees the steps; it
#   picks a template and supplies a few values, and Python materializes the
#   whole decision tree up front. That tree then walks entirely client-side
#   with zero LLM calls between steps (ripple's ChainExecutor, RFC 13 M1).
#
# The schema each step rides (ripple's UniversalSpec, RFC 13 M1):
#   - each step is a UniversalSpec node;
#   - `chain` is the linear next step; `chain_map` (Record<selectionId, step>)
#     branches on the user's selection id;
#   - `flowId` namespaces this step's accumulated data
#     (`<flowId>_selection` / `<flowId>_formData`);
#   - the terminal step carries `onComplete` — a FlowAction:
#       {kind:'emit', event, payload?} | {kind:'navigate', url} | {kind:'chat', message};
#   - a later step pre-fills from an earlier pick with
#     `{state.<flowId>_selection.field}` / `{state.<flowId>_formData.field}`.
#
# Scope (captain's priority — the primitive serving MANY use cases):
#   NON-COMMERCE templates only. An onboarding wizard (goal → details/invite
#   branch → confirm) and a due-diligence intake (a survey/intake: stage →
#   financials/team branch → review). Checkout is deliberately NOT built here —
#   it is one exemplar of the primitive, not the headline (RFC 13 §7, M4).
#
# This module emits plain Python dicts (JSON-serializable). It does NOT import
# ripple TypeScript — the tree is a data contract, validated by pocketpaw's own
# catalog / action-verb walkers (`pocketpaw.ripple.manifest`).

from __future__ import annotations

from typing import Any

# The inline-Ripple envelope version (the `{version, ui}` doc the chat
# extractor recognizes — RFC 13 M0, run_core._looks_like_ripple_spec). The
# nested steps themselves are UniversalSpec v2.0 nodes; this is the OUTER
# envelope version, matching what `_inline.py` mandates for a chat reply.
INLINE_SPEC_VERSION = "1.0"

# The known flow templates the `start_flow` tool exposes as its `flow_type`
# enum. The model picks one of these; the matching builder owns the tree.
# Kept NON-COMMERCE-first per RFC 13's primitive-over-exemplar framing.
FLOW_TYPES: tuple[str, ...] = (
    "onboarding_wizard",
    "due_diligence_intake",
)


# ---------------------------------------------------------------------------
# Small node helpers — keep the builders readable and the emitted shapes
# consistent. Each returns a plain UINode dict (the shape ripple's NodeRenderer
# and pocketpaw's catalog walker both understand).
# ---------------------------------------------------------------------------


def _heading(text: str) -> dict[str, Any]:
    return {"type": "heading", "props": {"text": text}}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "props": {"text": text}}


def _input(bind: str, label: str, placeholder: str = "") -> dict[str, Any]:
    props: dict[str, Any] = {"label": label}
    if placeholder:
        props["placeholder"] = placeholder
    return {"type": "input", "bind": bind, "props": props}


def _select_button(label: str, selection_id: str) -> dict[str, Any]:
    """A card/option button that ADVANCES the flow, branching on its id.

    Emits the standard `flow.next` verb whose `value` carries the
    `{selection}` the ChainExecutor namespaces and keys `chain_map` on. The
    selection's `id` is the `chain_map` key; `label` is what a later
    `{state.<flowId>_selection.label}` expression reads back.
    """
    return {
        "type": "button",
        "props": {"label": label},
        "on_click": {
            "action": "emit",
            "target": "flow.next",
            "value": {"selection": {"id": selection_id, "label": label}},
        },
    }


def _continue_button(label: str, form_binds: list[str]) -> dict[str, Any]:
    """A form step's primary button — carries the entered fields forward.

    `value.formData` mirrors the step's input binds via `{state.<bind>}`
    placeholders; the action dispatcher resolves them before the
    ChainExecutor namespaces them as `<flowId>_formData`.
    """
    form_data = {bind: f"{{state.{bind}}}" for bind in form_binds}
    return {
        "type": "button",
        "props": {"label": label},
        "on_click": {
            "action": "emit",
            "target": "flow.next",
            "value": {"formData": form_data},
        },
    }


def _submit_button(label: str) -> dict[str, Any]:
    """A terminal step's primary button — fires `flow.submit`, which runs the
    step's `onComplete` FlowAction with the full accumulated payload."""
    return {
        "type": "button",
        "props": {"label": label},
        "on_click": {"action": "emit", "target": "flow.submit", "value": {}},
    }


def _container(children: list[dict[str, Any]], cls: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "container", "children": children}
    if cls:
        node["props"] = {"class": cls}
    return node


# ---------------------------------------------------------------------------
# Template 1 — Onboarding wizard (NON-COMMERCE proof, RFC 13 M1/M3).
#
#   Step 1 (root):  pick a goal           — branches via chain_map
#   Step 2a:        focus → workspace name (form)        ┐
#   Step 2b:        collaborate → team workspace (form)  ┘ both chain → step 3
#   Step 3 (term):  confirm — pre-filled from steps 1+2 via {state.x}, emits
#
# Mirrors the M1 fixture (onboarding-wizard.ts) so the same tree this builder
# emits is the one M1's ChainExecutor tests already prove walks correctly.
# ---------------------------------------------------------------------------


def build_onboarding_wizard(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the onboarding-wizard flow tree as a `{version, ui}` doc.

    `config` is the optional descriptor override. Recognized keys (all
    optional — every one has a sensible default so a bare `flow_type` works):
      - `product_name` (str): branded into the welcome / confirm copy.
      - `goals` (list[{id, label}]): override the goal options at step 1.
        Each goal id becomes a `chain_map` branch. A goal whose id is not
        `focus`/`collaborate` falls through to the shared details step.
    """
    config = config or {}
    product_name = str(config.get("product_name") or "your workspace")

    # --- Step 3 (terminal): confirm, pre-filled from the earlier answers -----
    # Cross-step pre-fill: reads back step 1's selection label and step 2's
    # entered workspace name via `{state.<flowId>_*}` — the ChainExecutor
    # namespaces each step's data under its flowId.
    confirm_step: dict[str, Any] = {
        "version": "2.0",
        "id": "onboard-confirm",
        "flowId": "confirm",
        "intent": "confirm",
        "title": "You are all set",
        # Terminal action: hand the whole accumulated payload back to the host.
        "onComplete": {"kind": "emit", "event": "onboarding.complete"},
        "ui": _container(
            [
                _heading("Review your setup"),
                _text("Goal: {state.pick_goal_selection.label}"),
                _text("Workspace: {state.enter_details_formData.workspace}"),
                _submit_button("Finish setup"),
            ],
            cls="flow-confirm",
        ),
    }

    def _details_step(step_id: str, heading: str, placeholder: str) -> dict[str, Any]:
        # A step-2 form. flowId is shared across both branches ("enter_details")
        # so the confirm step's `{state.enter_details_formData.workspace}`
        # resolves regardless of which branch the user took.
        return {
            "version": "2.0",
            "id": step_id,
            "flowId": "enter_details",
            "intent": "form",
            "title": "Set up your workspace",
            "chain": confirm_step,
            "ui": _container(
                [
                    _heading(heading),
                    _input("workspace", "Workspace name", placeholder),
                    _continue_button("Continue", ["workspace"]),
                ]
            ),
        }

    focus_step = _details_step("onboard-details", "Name your workspace", "Acme HQ")
    invite_step = _details_step("onboard-invite", "Name your shared workspace", "Acme Team")

    # --- Step 1 (root): pick a goal, branch on the selection -----------------
    default_goals = [
        {"id": "focus", "label": "Focus on my own work"},
        {"id": "collaborate", "label": "Collaborate with a team"},
    ]
    goals = config.get("goals") or default_goals

    # The chain_map: each goal id -> the step 2 it routes to. `collaborate`
    # gets the invite-flavored step; everything else shares the focus step.
    chain_map: dict[str, dict[str, Any]] = {}
    goal_buttons: list[dict[str, Any]] = []
    for goal in goals:
        gid = str(goal.get("id"))
        glabel = str(goal.get("label") or gid)
        chain_map[gid] = invite_step if gid == "collaborate" else focus_step
        goal_buttons.append(_select_button(glabel, gid))

    root: dict[str, Any] = {
        "version": "2.0",
        "id": "onboard-goal",
        "flowId": "pick_goal",
        "intent": "select",
        "title": "What brings you here?",
        "selection": "single",
        "chain_map": chain_map,
        "ui": _container(
            [
                _heading(f"Welcome to {product_name}"),
                _text("Pick your primary goal to get started."),
                *goal_buttons,
            ]
        ),
    }

    return {"version": INLINE_SPEC_VERSION, "ui": root}


# ---------------------------------------------------------------------------
# Template 2 — Due-diligence intake (NON-COMMERCE vertical, RFC 13 §7.1, M3).
#
#   Step 1 (root):  pick the deal stage     — branches via chain_map
#   Step 2a:        early-stage → traction + raise (form)  ┐
#   Step 2b:        growth → revenue + metrics (form)      ┘ both chain → step 3
#   Step 3:         risk & flags (form)                    → step 4
#   Step 4 (term):  review — pre-filled from every prior step via {state.x},
#                   emits `diligence.intake.submit` carrying the full packet.
#
# This is the "vertical workflow → flow template" case from RFC 13 §7.1: a
# multi-step intake split into digestible steps, branching on the deal stage,
# accumulating a structured packet the host acts on at the terminal emit.
# A survey is the same shape with the labels swapped; due-diligence is the
# richer (4-step, branch + linear) proof.
# ---------------------------------------------------------------------------


def build_due_diligence_intake(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the due-diligence intake flow tree as a `{version, ui}` doc.

    `config` overrides (all optional):
      - `company_name` (str): branded into the intro / review copy.
      - `submit_event` (str): the terminal `onComplete` event name
        (default `diligence.intake.submit`).
    """
    config = config or {}
    company_name = str(config.get("company_name") or "the company")
    submit_event = str(config.get("submit_event") or "diligence.intake.submit")

    # --- Step 4 (terminal): review the assembled packet, then submit ---------
    # Pre-fills from EVERY prior step (the deal stage, the stage-specific
    # financials, the risk flags) via `{state.<flowId>_*}`.
    review_step: dict[str, Any] = {
        "version": "2.0",
        "id": "dd-review",
        "flowId": "review",
        "intent": "confirm",
        "title": "Review the intake",
        "onComplete": {"kind": "emit", "event": submit_event},
        "ui": _container(
            [
                _heading("Confirm the diligence packet"),
                _text("Deal stage: {state.deal_stage_selection.label}"),
                _text("Headline metric: {state.financials_formData.headline}"),
                _text("Key risk: {state.risk_review_formData.key_risk}"),
                _submit_button("Submit intake"),
            ],
            cls="flow-dd-review",
        ),
    }

    # --- Step 3 (shared): risk & flags -> review -----------------------------
    risk_step: dict[str, Any] = {
        "version": "2.0",
        "id": "dd-risk",
        "flowId": "risk_review",
        "intent": "form",
        "title": "Risk & open flags",
        "chain": review_step,
        "ui": _container(
            [
                _heading("Note the top risk"),
                _input("key_risk", "Biggest open risk", "Customer concentration"),
                _input("mitigation", "Mitigation (optional)", "Diversifying pipeline"),
                _continue_button("Continue to review", ["key_risk", "mitigation"]),
            ]
        ),
    }

    def _financials_step(
        step_id: str, heading: str, fields: list[tuple[str, str, str]]
    ) -> dict[str, Any]:
        # A step-2 financials form. Shared flowId ("financials") so the review
        # step's `{state.financials_formData.headline}` resolves on both
        # branches. Every branch exposes a `headline` field so the read-back
        # is branch-agnostic.
        children: list[dict[str, Any]] = [_heading(heading)]
        binds: list[str] = []
        for bind, label, placeholder in fields:
            children.append(_input(bind, label, placeholder))
            binds.append(bind)
        children.append(_continue_button("Continue", binds))
        return {
            "version": "2.0",
            "id": step_id,
            "flowId": "financials",
            "intent": "form",
            "title": "Financial snapshot",
            "chain": risk_step,
            "ui": _container(children),
        }

    early_step = _financials_step(
        "dd-financials-early",
        "Early-stage traction",
        [
            ("headline", "Headline traction metric", "1.2k weekly active"),
            ("raise", "Round size sought", "$2M seed"),
        ],
    )
    growth_step = _financials_step(
        "dd-financials-growth",
        "Growth metrics",
        [
            ("headline", "Headline revenue metric", "$4M ARR"),
            ("growth", "YoY growth", "180%"),
        ],
    )

    # --- Step 1 (root): pick the deal stage, branch on it --------------------
    stages = [
        {"id": "early", "label": "Early stage (pre-seed / seed)"},
        {"id": "growth", "label": "Growth stage (Series A+)"},
    ]
    chain_map: dict[str, dict[str, Any]] = {
        "early": early_step,
        "growth": growth_step,
    }
    stage_buttons = [_select_button(s["label"], s["id"]) for s in stages]

    root: dict[str, Any] = {
        "version": "2.0",
        "id": "dd-stage",
        "flowId": "deal_stage",
        "intent": "select",
        "title": "Due-diligence intake",
        "selection": "single",
        "chain_map": chain_map,
        "ui": _container(
            [
                _heading(f"Diligence intake for {company_name}"),
                _text("Pick the deal stage — the next steps adapt to it."),
                *stage_buttons,
            ]
        ),
    }

    return {"version": INLINE_SPEC_VERSION, "ui": root}


# ---------------------------------------------------------------------------
# Dispatch — `flow_type` -> builder. The tool layer (start_flow) validates the
# `flow_type` against FLOW_TYPES and calls `build_flow`; the builder owns the
# rest. `domain` is accepted as a forward-compatible hint (RFC 13 §7.1 notes a
# later data-fresh materialization path) but does not change the tree today.
# ---------------------------------------------------------------------------

_BUILDERS = {
    "onboarding_wizard": build_onboarding_wizard,
    "due_diligence_intake": build_due_diligence_intake,
}


def build_flow(
    flow_type: str,
    domain: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expand a descriptor into a full `{version, ui}` Chain Flow doc.

    Raises ``ValueError`` for an unknown ``flow_type`` (the tool layer turns
    that into an agent-readable error listing the valid templates).
    """
    builder = _BUILDERS.get(flow_type)
    if builder is None:
        valid = ", ".join(sorted(_BUILDERS))
        raise ValueError(f"Unknown flow_type {flow_type!r}. Known templates: {valid}.")
    return builder(config)


__all__ = [
    "FLOW_TYPES",
    "INLINE_SPEC_VERSION",
    "build_due_diligence_intake",
    "build_flow",
    "build_onboarding_wizard",
]
