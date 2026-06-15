# pocketpaw/ripple/_flows.py — Chain Flow builders (RFC 13 §7.1, M3 → CHAIN FLOW v2).
#
# Created: 2026-05-31 (RFC 13 M3, feat/m3-flow-authoring-tool).
# Updated: 2026-06-15 (feat/chain-flow-v2 — GENESIS-STYLE FORGIVENESS / RELIABILITY):
#   - Front-loaded two FORGIVENESS passes before the strict validators so most
#     "sloppy" descriptors BUILD (repaired) instead of erroring — genesis's
#     reliability philosophy (repair imperfect specs, don't reject them):
#       * `_normalize_descriptor` (§2.0): coerces the model's instinctive key
#         slips on the raw dict BEFORE parse — a terminal `complete` written with
#         `type:`/`kind:` → `action:`; a StepAction / continuation written with
#         `type:` → `verb:` (the rest of ripple uses type/kind for NODES, so the
#         model reaches for them here too). Deep-copies first; never mutates the
#         caller's dict.
#       * `_repair_descriptor` (§2.0): patches recoverable GRAPH defects on the
#         parsed model — a terminal step (or the whole flow) with no `complete`
#         gets a default `chat`; a flow where every step transitions has its
#         last step made terminal; a dead-ending `select`/`info` LAST step is
#         converted to terminal (was a hard reject).
#   - `_friendly_parse_error`: any shape defect that survives normalization now
#     raises a FlowBuildError that NAMES the fix (e.g. "terminal `complete` needs
#     an `action` key (chat|navigate|emit|call_binding|create_pocket)") instead
#     of leaking the raw `errors.pydantic.dev` dump.
#   - `_validate_graph` relaxes the select-must-transition rule for a select the
#     repair pass made TERMINAL (it carries a `complete`); `_assemble_node` gives
#     a terminal select a Finish (flow.submit) button so the recorded pick can
#     run its `complete`. HARD rejects are kept ONLY for genuinely unrenderable
#     bugs: a transition to an UNDECLARED id, a duplicate step id, a branch key
#     that is not an option id.
# Updated: 2026-06-15 (feat/chain-flow-v2 — CONTINUATION-NOT-A-BUTTON FIX):
#   - Mid-flow `StepAction.on_success` / `.on_error` continuation chains are no
#     longer typed as `list[StepAction]`. `StepAction` is a BUTTON and requires
#     `id` + `label`; a continuation is NOT a button (design §1.3 / §1.7 write
#     them as bare `{"verb":"toast","message":…}` / `{"verb":"set","key":…,
#     "value":…}` with NO id/label). A new `StepContinuation` model carries the
#     same payload fields + its own nested `on_success`/`on_error` but drops
#     id/label, so a naturally-authored continuation chain builds instead of
#     raising FlowBuildError demanding button fields. `_validate_actions` now
#     recurses the per-verb required-key table INTO continuations (without
#     requiring id/label), and the on_success/on_error lowering accepts a
#     `StepContinuation` — output is unchanged (id/label were already discarded
#     in lowering, so the emitted bare handler dict is byte-identical).
# Updated: 2026-06-15 (feat/chain-flow-v2 — GENERALIZATION):
#   - The headline change: the flow primitive is no longer "pick 1 of 2
#     hardcoded Python templates." A new entry point
#     `build_flow_from_descriptor(descriptor)` lets the agent author an
#     ARBITRARY ephemeral mini-app as a FLAT step-graph (a `FlowDescriptor`:
#     a `flow` id, an `entry` step id, and a `steps[]` array where each step
#     points at the next by id string via `next` / `branch`). The builder owns
#     ALL the fragile nesting the model used to get wrong: it materializes the
#     nested `chain` / `chain_map` `UniversalSpec` tree, deep-validates the
#     graph (every §2.3 invariant), validates prefill refs (§2.5) and action
#     verbs (§2.4), and rewrites `{stepId.field}` sugar into the executor's
#     real `{state.<flowId>_selection|_formData.field}` keys (§1.5 / §2.2) so
#     the author never writes the suffix — the #1 hand-author bug, eliminated.
#   - The two shipped templates become THIN PRESETS: `build_onboarding_wizard`
#     / `build_due_diligence_intake` now emit a flat descriptor and delegate to
#     `build_flow_from_descriptor`. `build_flow(flow_type, …)` stays as the
#     preset dispatcher. Every assertion in `test_flow_authoring.py` still holds
#     because the builder emits the SAME nested structure (flowId on every step,
#     one terminal with onComplete.kind=="chat", chain_map keyed on option ids,
#     shared join node, Back on intermediate steps).
#   - `assemble_node` REUSES the exact `_select_button` / `_continue_button` /
#     `_submit_button` / `_back_button` / `_nav_row` helpers (§7 D4) so the
#     general path emits BYTE-IDENTICAL buttons to the presets — the
#     ChainExecutor and the existing tests rely on those on_click event shapes.
#   - Terminal lowering (§1.4 / §7 D1) extends the FlowAction union with the new
#     `invoke_tool` / `call_binding` / `create_pocket` kinds (plus the existing
#     `chat` / `navigate` / `emit`) and an optional `then` post-action (chain
#     length capped at 2). Per-step `StepAction`s lower into a `button` whose
#     `on_click` is the matching ripple action-VM verb (§1.3).
#   - Caps: ≤30 steps (over → FlowBuildError "split the flow"); `then` chain ≤2.
#   - GENERAL path allows ≥1 terminal (§7 D3 — branches may end differently,
#     each terminal carries its own `complete` or the flow default). The PRESETS
#     keep exactly one terminal, so the preset's one-terminal test stays green.
#
# Updated: 2026-06-07 (feat/flow-terminal-to-agent):
#   - Terminal step now loops the collected answers back to the AGENT instead of
#     firing a dead host event. Both shipped templates' `onComplete` is now
#     `{kind:"chat", message:<human prompt>}` — the runtime appends the
#     accumulated payload to the message before sending it to the agent. The
#     `navigate`/`emit` FlowAction kinds remain available for other flows.
#   - Each form step now ALSO carries genesis-style structured field DATA
#     (`form_fields: [{id, label, type, placeholder, required, options?}]`) so
#     ripple's FormLayout renders a designed form. The hand-built raw `ui` widget
#     tree stays as the backward-compat fallback (ripple falls back to
#     NodeRenderer(ui) when no field data is present). The terminal review/confirm
#     step carries structured `review_rows` for the same designed-render benefit.
#
# Changes:
#   - 2026-06-07 (polish/rfc13-flow-nav-validation): added a `_back_button`
#     helper (emits `flow.back`, mirroring `_continue_button`'s emit shape) and
#     a `_nav_row` container, then wired Back into the intermediate (non-first,
#     non-terminal) steps of BOTH templates — the onboarding details step and
#     the due-diligence financials + risk steps. The runtime already supported
#     `flow.back` via the history stack, but no template rendered the control,
#     so users could not step backward. Root/first steps (nothing to go back
#     to) and terminal confirm/review steps (Submit) deliberately get no Back.
#
# What this is:
#   The DETERMINISTIC half of the `start_flow` authoring tool. v2: the LLM emits
#   a FLAT step-graph descriptor (impossible to mis-nest), and a Python builder
#   materializes + deep-validates the full nested Chain Flow tree, returning it
#   as a `{version, ui}` inline-Ripple doc. The legacy preset path (pick a
#   `flow_type`, supply a few values) still works on top of the same builder.
#
# Why a builder owns the tree (not the model):
#   The genesis prototype's `preprocessChainStrings` repair code (RFC 13 §2.2)
#   is the evidence: hand-authoring a recursively-nested chain/chain_map tree
#   is fragile — weak models stringify the chain or wrap it in a
#   `{action:'chain', next:{…}}` shape. v2 keeps the reliability property — the
#   agent NEVER emits a raw nested tree — but generalizes the authoring surface:
#   the model writes a flat list with `next`/`branch` pointers, Python resolves
#   pointers into nesting and REJECTS any graph that would dead-end with a
#   precise, agent-readable error the tool surfaces for a retry.
#
# The schema each step rides (ripple's UniversalSpec, RFC 13 M1):
#   - each step is a UniversalSpec node;
#   - `chain` is the linear next step; `chain_map` (Record<selectionId, step>)
#     branches on the user's selection id;
#   - `flowId` namespaces this step's accumulated data
#     (`<flowId>_selection` / `<flowId>_formData`); set from the descriptor
#     step's optional `slot` (branch-agnostic shared slot) else its `id`;
#   - a terminal step carries `onComplete` — a FlowAction:
#       chat | navigate | emit (existing) | invoke_tool | call_binding |
#       create_pocket (new in v2), each optionally with a `then` post-action;
#   - form steps additionally carry `form_fields` (genesis-style structured field
#     DATA) so ripple's FormLayout renders a designed form; the raw `ui` tree
#     remains the fallback;
#   - a later step pre-fills from an earlier pick with
#     `{state.<flowId>_selection.field}` / `{state.<flowId>_formData.field}` —
#     the builder owns that rewrite from the author's `{stepId.field}` sugar.
#
# This module emits plain Python dicts (JSON-serializable). It does NOT import
# ripple TypeScript — the tree is a data contract, validated by pocketpaw's own
# catalog / action-verb walkers (`pocketpaw.ripple.manifest`).

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The inline-Ripple envelope version (the `{version, ui}` doc the chat
# extractor recognizes — RFC 13 M0, run_core._looks_like_ripple_spec). The
# nested steps themselves are UniversalSpec v2.0 nodes; this is the OUTER
# envelope version, matching what `_inline.py` mandates for a chat reply.
INLINE_SPEC_VERSION = "1.0"

# The UniversalSpec node version every assembled step carries.
_STEP_VERSION = "2.0"

# Caps (§7 open decisions): a flat graph bigger than this is an authoring smell;
# reject and ask the model to split it. `then` post-action chains are bounded so
# a malformed `then` can't recurse without limit.
_MAX_STEPS = 30
_MAX_THEN_DEPTH = 2

# The known flow templates the `start_flow` tool exposes as `flow_type` preset
# shorthand. The model can still pick one of these; the matching builder emits a
# descriptor and delegates to `build_flow_from_descriptor`. Kept NON-COMMERCE
# first per RFC 13's primitive-over-exemplar framing.
FLOW_TYPES: tuple[str, ...] = (
    "onboarding_wizard",
    "due_diligence_intake",
)

# ---------------------------------------------------------------------------
# Verb / action allow-sets (§2.4). Kept in lock-step with ripple's action VM
# (`event-dispatcher.ts`) and pocketpaw's `manifest._KNOWN_ACTION_VERBS`. A
# mid-flow StepAction rides any of `_STEP_ACTION_VERBS`; a terminal `complete`
# uses one of `_TERMINAL_ACTIONS`.
# ---------------------------------------------------------------------------

_STEP_ACTION_VERBS: frozenset[str] = frozenset(
    {"api", "call_binding", "invoke_tool", "emit", "navigate", "set", "toast"}
)

_TERMINAL_ACTIONS: frozenset[str] = frozenset(
    {"chat", "navigate", "emit", "invoke_tool", "call_binding", "create_pocket"}
)

# Per-verb required payload keys (§2.4). Checked after the Pydantic shape pass so
# the error names the missing key precisely instead of a generic schema gripe.
_STEP_VERB_REQUIRED: dict[str, tuple[str, ...]] = {
    "invoke_tool": ("tool",),
    "call_binding": ("binding", "path"),
    "api": ("url",),
    "navigate": ("url",),
    "set": ("key", "value"),
    "emit": (),  # emit is forgiving — a bare event name is allowed
    "toast": (),
}

_TERMINAL_REQUIRED: dict[str, tuple[str, ...]] = {
    "chat": ("message",),
    "navigate": ("url",),
    "emit": (),
    "invoke_tool": ("tool",),
    "call_binding": ("binding", "path"),
    "create_pocket": ("name",),
}


# ---------------------------------------------------------------------------
# FlowBuildError — every structural / reference / action defect raises this with
# a precise, agent-readable message. The tool layer surfaces it verbatim so the
# model can fix the flat graph and retry (genesis's forgiving-authoring loop).
# ---------------------------------------------------------------------------


class FlowBuildError(ValueError):
    """A flat FlowDescriptor could not be materialized into a valid flow tree.

    Subclasses ``ValueError`` so existing ``except ValueError`` call sites (the
    preset path's unknown-``flow_type`` handler) keep working. The message is
    written for the model: it names the offending step / ref / verb and what is
    wrong, so a retry can be surgical.
    """


# ---------------------------------------------------------------------------
# Descriptor Pydantic models (§1). The flat, agent-friendly shape. `extra=allow`
# on the step/action models keeps the descriptor forgiving — an unknown key is
# carried, not rejected (genesis "any model can author" forgiveness). The graph
# / ref / action validators below catch the defects that actually break a flow.
# ---------------------------------------------------------------------------


class StepContinuation(BaseModel):
    """A continuation in a StepAction's `on_success` / `on_error` chain (§1.3).

    A continuation is NOT a button — it carries no `id` and no `label`. The
    design authors them bare (`{"verb":"toast","message":…,"variant":…}` /
    `{"verb":"set","key":…,"value":…}`), so requiring button fields here would
    reject every naturally-authored chain. It mirrors `StepAction`'s verb +
    payload fields and nests its OWN `on_success`/`on_error`, validated per-verb
    by `_validate_actions` (same required-key table, minus id/label).
    """

    model_config = ConfigDict(extra="allow")

    verb: str
    # verb-specific payload (validated per verb in `_validate_actions`)
    tool: str | None = None
    args: dict[str, Any] | None = None
    binding: str | None = None
    path: str | None = None
    params: dict[str, Any] | None = None
    url: str | None = None
    method: str | None = None
    body: dict[str, Any] | None = None
    key: str | None = None
    value: Any = None
    message: str | None = None
    variant: str | None = None
    # nested continuations — a continuation may itself chain on_success/on_error
    on_success: list[StepContinuation] | None = None
    on_error: list[StepContinuation] | None = None


class StepAction(BaseModel):
    """A mid-flow button: runs a tool/API/binding without leaving the step (§1.3)."""

    model_config = ConfigDict(extra="allow")

    id: str
    label: str
    verb: str
    # verb-specific payload (validated per verb in `_validate_actions`)
    tool: str | None = None
    args: dict[str, Any] | None = None
    binding: str | None = None
    path: str | None = None
    params: dict[str, Any] | None = None
    url: str | None = None
    method: str | None = None
    body: dict[str, Any] | None = None
    key: str | None = None
    value: Any = None
    message: str | None = None
    variant: str | None = None
    # continuations — standard ripple on_success / on_error action chains. These
    # are NOT buttons (no id/label) — see StepContinuation above.
    on_success: list[StepContinuation] | None = None
    on_error: list[StepContinuation] | None = None


class TerminalAction(BaseModel):
    """A flow's hand-off when the user clicks the final button (§1.4)."""

    model_config = ConfigDict(extra="allow")

    action: str
    # existing kinds
    message: str | None = None
    url: str | None = None
    event: str | None = None
    payload: dict[str, Any] | None = None
    # new kinds
    tool: str | None = None
    args: dict[str, Any] | None = None
    binding: str | None = None
    path: str | None = None
    params: dict[str, Any] | None = None
    name: str | None = None
    template: str | None = None
    spec: dict[str, Any] | None = None
    seed_from_flow: bool | None = None
    # optional post-action (capped at depth 2)
    then: TerminalAction | None = None


class FlowStep(BaseModel):
    """One step in the flat graph (§1.2)."""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["select", "form", "confirm", "info"]
    title: str | None = None
    subtitle: str | None = None
    # branch-agnostic shared slot (§2.2): the node's flowId = slot or id
    slot: str | None = None
    # content (kind-specific)
    options: list[dict[str, Any]] | None = None
    fields: list[dict[str, Any]] | None = None
    review: list[dict[str, Any]] | None = None
    body: str | None = None
    # UI hints (optional, never required)
    ui: dict[str, Any] | None = None
    # transitions — exactly ONE of next | branch on a non-terminal step
    next: str | None = None
    branch: dict[str, str] | None = None
    # mid-flow actions
    actions: list[StepAction] | None = None
    # terminal action (overrides the flow-level default)
    complete: TerminalAction | None = None


class FlowDescriptor(BaseModel):
    """The flat top-level descriptor the agent emits (§1.1)."""

    model_config = ConfigDict(extra="allow")

    flow: str
    entry: str
    title: str | None = None
    steps: list[FlowStep] = Field(default_factory=list)
    complete: TerminalAction | None = None


# StepContinuation / StepAction / TerminalAction are self-referential; rebuild so
# forward refs bind. StepContinuation first — StepAction references it.
StepContinuation.model_rebuild()
StepAction.model_rebuild()
TerminalAction.model_rebuild()


# ---------------------------------------------------------------------------
# Small node helpers — keep the builders readable and the emitted shapes
# consistent. Each returns a plain UINode dict (the shape ripple's NodeRenderer
# and pocketpaw's catalog walker both understand). The general builder
# (`assemble_node`) REUSES these so its buttons are byte-identical to the
# presets (§7 D4).
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


def _form_field(
    field_id: str,
    label: str,
    placeholder: str = "",
    field_type: str = "text",
    required: bool = True,
    options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A genesis-style structured form-field descriptor.

    Carried on a form step's `form_fields` array so ripple's FormLayout →
    FormSection renders a DESIGNED form (not the raw widget tree). `field_id`
    matches the step's input `bind` so the entered value lands under the same
    key the `flow.next` handler forwards. Shape mirrors genesis:
    `{id, label, type, placeholder, required, options?}`.
    """
    field: dict[str, Any] = {
        "id": field_id,
        "label": label,
        "type": field_type,
        "required": required,
    }
    if placeholder:
        field["placeholder"] = placeholder
    if options is not None:
        field["options"] = options
    return field


def _review_row(label: str, value: str) -> dict[str, Any]:
    """A structured review row for a terminal confirm/summary step.

    Carried on the terminal step's `review_rows` so a designed summary layout
    can render label/value pairs; `value` keeps the same `{state.x}` pre-fill
    expression the raw `ui` text uses, so both render paths read identical data.
    """
    return {"label": label, "value": value}


def _back_button(label: str = "Back") -> dict[str, Any]:
    """A step-back button — fires `flow.back`, which pops the FlowRunner's
    history stack and re-renders the previous step.

    Mirrors `_continue_button` / `_submit_button`: same `emit` action shape,
    targeting the `flow.back` verb the inline chat adapter routes into the
    ChainExecutor. Carries an empty `value` — stepping back needs no payload;
    the previously-accumulated step data is what the runner restores.
    """
    return {
        "type": "button",
        "props": {"label": label},
        "on_click": {"action": "emit", "target": "flow.back", "value": {}},
    }


def _nav_row(children: list[dict[str, Any]]) -> dict[str, Any]:
    """A small inline container grouping a step's navigation buttons so Back
    and Continue sit together in one row."""
    return _container(children, cls="flow-nav")


def _container(children: list[dict[str, Any]], cls: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "container", "children": children}
    if cls:
        node["props"] = {"class": cls}
    return node


def _action_button(action: StepAction) -> dict[str, Any]:
    """Lower a mid-flow StepAction into a `button` whose `on_click` is the
    matching ripple action-VM verb (§1.3 / §7 D4).

    The on_click is `{action:<verb>, ...payload, on_success?, on_error?}` — a
    plain action handler the dispatcher already routes (and pocketpaw's
    `validate_action_verbs` already accepts). Payload keys are copied through
    per verb; `{state.…}` / `{result.…}` refs inside arg/param strings are
    rewritten in the prefill pass and resolved client-side at dispatch.
    """
    handler: dict[str, Any] = {"action": action.verb}
    # Copy only the keys that matter for the verb, in a stable order.
    for key in ("tool", "args", "binding", "path", "params", "url", "method", "body", "key"):
        val = getattr(action, key, None)
        if val is not None:
            handler[key] = val
    # `value` is meaningful for `set`; copy it whenever provided (it may be
    # falsy like False/0, so check the field, not truthiness).
    if action.value is not None:
        handler["value"] = action.value
    if action.message is not None:
        handler["message"] = action.message
    if action.variant is not None:
        handler["variant"] = action.variant
    if action.on_success:
        handler["on_success"] = [_action_handler_only(a) for a in action.on_success]
    if action.on_error:
        handler["on_error"] = [_action_handler_only(a) for a in action.on_error]
    return {"type": "button", "props": {"label": action.label}, "on_click": handler}


def _action_handler_only(action: StepContinuation) -> dict[str, Any]:
    """Lower a continuation (on_success/on_error entry) into a bare action
    handler dict (no surrounding button) — the shape ripple's action chain
    expects. A continuation carries no id/label, so the emitted handler is the
    same bare `{action:<verb>, …payload, on_success?, on_error?}` shape it was
    before (id/label were never copied into the handler)."""
    handler: dict[str, Any] = {"action": action.verb}
    for key in ("tool", "args", "binding", "path", "params", "url", "method", "body", "key"):
        val = getattr(action, key, None)
        if val is not None:
            handler[key] = val
    if action.value is not None:
        handler["value"] = action.value
    if action.message is not None:
        handler["message"] = action.message
    if action.variant is not None:
        handler["variant"] = action.variant
    if action.on_success:
        handler["on_success"] = [_action_handler_only(a) for a in action.on_success]
    if action.on_error:
        handler["on_error"] = [_action_handler_only(a) for a in action.on_error]
    return handler


# ---------------------------------------------------------------------------
# Prefill / cross-step reference rewrite (§1.5 / §2.2).
#
# The author writes `{stepId.field}` (or `{slot.field}`); the builder rewrites
# it to the executor's real namespaced key — `{state.<flowId>_selection.field}`
# for a select step, `{state.<flowId>_formData.field}` for a form step. The
# author never writes the `_selection` / `_formData` suffix (risk #8). `{flow.*}`
# and `{result.*}` and bare `{state.*}` pass through untouched.
# ---------------------------------------------------------------------------

import re  # noqa: E402  (kept next to the rewrite logic it serves)

# Matches `{token.field}` where token is a bare identifier and field a dotted
# path. Excludes tokens we never rewrite: `state`, `flow`, `result`.
_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z0-9_.]+)\}")
_PASSTHROUGH_TOKENS = frozenset({"state", "flow", "result"})


def _ref_targets(text: str) -> list[tuple[str, str]]:
    """Every `{token.field}` ref in `text` that is NOT a passthrough token.

    Returns `(token, field)` pairs — `token` is a step id or slot name, `field`
    the dotted remainder. Used by both the validator (to check the ref resolves)
    and the rewrite (to namespace it).
    """
    out: list[tuple[str, str]] = []
    for m in _REF_RE.finditer(text):
        token, field = m.group(1), m.group(2)
        if token in _PASSTHROUGH_TOKENS:
            continue
        out.append((token, field))
    return out


def _rewrite_refs(text: str, slot_kind: dict[str, str]) -> str:
    """Rewrite every `{stepId.field}` / `{slot.field}` ref in `text` to its
    namespaced `{state.<flowId>_selection|_formData.field}` form.

    `slot_kind` maps a resolvable token (step id OR slot name) to the executor
    suffix (`selection` / `formData`) plus the flowId to namespace under. A
    token that isn't resolvable is left untouched here — `_validate_refs` has
    already hard-rejected the unknown ones, so anything that survives to this
    pass is either resolvable or a deliberate passthrough.
    """

    def _sub(m: re.Match[str]) -> str:
        token, field = m.group(1), m.group(2)
        if token in _PASSTHROUGH_TOKENS:
            return m.group(0)
        entry = slot_kind.get(token)
        if entry is None:
            return m.group(0)
        flow_id, suffix = entry.split("|", 1)
        return f"{{state.{flow_id}_{suffix}.{field}}}"

    return _REF_RE.sub(_sub, text)


def _rewrite_in_obj(obj: Any, slot_kind: dict[str, str]) -> Any:
    """Recursively rewrite `{stepId.field}` refs inside any JSON value (string,
    list, dict). Returns a NEW structure (does not mutate the input)."""
    if isinstance(obj, str):
        return _rewrite_refs(obj, slot_kind)
    if isinstance(obj, list):
        return [_rewrite_in_obj(v, slot_kind) for v in obj]
    if isinstance(obj, dict):
        return {k: _rewrite_in_obj(v, slot_kind) for k, v in obj.items()}
    return obj


def _collect_ref_strings(obj: Any, out: list[str]) -> None:
    """Collect every string leaf in a JSON value — used to scan action payloads
    for refs to validate."""
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ref_strings(v, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_ref_strings(v, out)


# ---------------------------------------------------------------------------
# THE GENERAL BUILDER — build_flow_from_descriptor (§2). Flat FlowDescriptor →
# {version, ui} nested Chain Flow doc, via the §2.1 ordered pipeline.
# ---------------------------------------------------------------------------


def _field_type(field: dict[str, Any]) -> str:
    """A form field's `type`, mapping unknowns → `text` (genesis forgiveness)."""
    raw = field.get("type")
    known = {
        "text",
        "textarea",
        "number",
        "email",
        "tel",
        "url",
        "date",
        "select",
        "radio",
        "checkbox",
        "switch",
        "rating",
        "slider",
        "file",
    }
    return raw if isinstance(raw, str) and raw in known else "text"


def _flow_id_of(step: FlowStep) -> str:
    """The node flowId for a step: its `slot` (branch-agnostic) else its `id`."""
    return step.slot or step.id


def _declared_fields(step: FlowStep) -> set[str]:
    """The field names a step exposes to prefill refs (§2.5).

    A select step exposes `id` / `label` (the selection's keys). A form step
    exposes each declared `fields[].id`.
    """
    if step.kind == "select":
        return {"id", "label"}
    if step.kind == "form":
        return {str(f.get("id")) for f in (step.fields or []) if f.get("id")}
    return set()


# ---------------------------------------------------------------------------
# §2.0 — Forgiveness front-matter (genesis: REPAIR imperfect specs, don't reject).
#
# Two passes run BEFORE the strict validators:
#   _normalize_descriptor  — coerce the model's instinctive key slips
#                            (type:/kind: → action:/verb:) on the raw dict;
#   _repair_descriptor     — patch recoverable GRAPH defects on the parsed model
#                            (missing terminal complete, no terminal, dead-end
#                            select/info last step).
# Between them sits the friendly-error wrap (_friendly_parse_error) so a shape
# defect that survives normalization NAMES the fix instead of leaking Pydantic.
# ---------------------------------------------------------------------------

# The flow-level terminal default injected when a terminal step (and the flow)
# declare no `complete` — hand the answers back to the agent, the safest default.
_DEFAULT_COMPLETE: dict[str, Any] = {"action": "chat", "message": "Done."}


def _normalize_complete_aliases(obj: Any) -> None:
    """Rewrite a terminal `complete` dict's `type:`/`kind:` → `action:`.

    Models instinctively write `type`/`kind` (the rest of ripple uses those for
    NODES). A terminal hand-off keys on `action:`; coerce the slip in place so
    the natural authoring just parses. Only fills `action` when it is absent —
    an explicit `action` always wins. Recurses into a `then` post-action.
    """
    if not isinstance(obj, dict):
        return
    if "action" not in obj:
        for alias in ("type", "kind"):
            if isinstance(obj.get(alias), str) and obj[alias]:
                obj["action"] = obj.pop(alias)
                break
    # a `then` chain is itself a terminal action — normalize it too
    if isinstance(obj.get("then"), dict):
        _normalize_complete_aliases(obj["then"])


def _normalize_action_aliases(obj: Any) -> None:
    """Rewrite a StepAction / continuation dict's `type:` → `verb:`.

    A mid-flow action (and its `on_success`/`on_error` continuations) keys on
    `verb:`; models reach for `type:` out of node-authoring habit. Coerce the
    slip in place (only when `verb` is absent) and recurse into the
    continuation chains.
    """
    if not isinstance(obj, dict):
        return
    if "verb" not in obj and isinstance(obj.get("type"), str) and obj["type"]:
        obj["verb"] = obj.pop("type")
    for chain_key in ("on_success", "on_error"):
        chain = obj.get(chain_key)
        if isinstance(chain, list):
            for entry in chain:
                _normalize_action_aliases(entry)


def _normalize_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Return a DEEP COPY of the raw descriptor with the model's common key
    slips coerced to the canonical keys, BEFORE Pydantic parse (§2.0).

    Coercions (genesis forgiveness — the model's instinct just works):
      - terminal `complete` (flow-level + per-step) with `type:`/`kind:` and no
        `action:` → `action:`;
      - StepAction + continuation with `type:` and no `verb:` → `verb:`.

    The caller's dict is never mutated — a fresh structure is built so a retry
    or a second build sees the original input.
    """
    import copy

    if not isinstance(descriptor, dict):
        return descriptor
    out = copy.deepcopy(descriptor)

    # flow-level default complete
    _normalize_complete_aliases(out.get("complete"))

    for step in out.get("steps") or []:
        if not isinstance(step, dict):
            continue
        _normalize_complete_aliases(step.get("complete"))
        for action in step.get("actions") or []:
            _normalize_action_aliases(action)
    return out


def _friendly_parse_error(exc: Exception) -> str:
    """Turn a Pydantic ValidationError (or any parse failure) into a friendly,
    agent-readable :class:`FlowBuildError` message that NAMES the fix instead of
    leaking the `errors.pydantic.dev` URL and the raw error dump.

    Recognizes the highest-signal slips:
      - a terminal `complete` missing its `action` key (the #1 slip after the
        type:/kind: alias path, e.g. a `complete` written as a bare message);
      - a step missing its `kind`, or an unknown `kind`.
    Falls back to a clean one-line summary of the first error for anything else.
    """
    try:
        from pydantic import ValidationError
    except Exception:  # pragma: no cover - pydantic always present here
        return f"descriptor is malformed: {exc}"

    if not isinstance(exc, ValidationError):
        return f"descriptor is malformed: {exc}"

    errors = exc.errors()
    for err in errors:
        loc = err.get("loc", ())
        loc_str = ".".join(str(p) for p in loc)
        # a terminal `complete` (or flow default) missing `action`
        if "complete" in loc and loc and loc[-1] == "action":
            return (
                f"terminal `complete` (at {loc_str or 'complete'}) needs an `action` key "
                "(chat | navigate | emit | call_binding | create_pocket) — "
                'e.g. {"action": "chat", "message": "Done."}'
            )
        # a step missing / mis-typing `kind`
        if "kind" in loc:
            return f"step field `kind` (at {loc_str}) must be one of select | form | confirm | info"
        # a mid-flow action missing `verb`
        if "actions" in loc and loc and loc[-1] == "verb":
            return (
                f"mid-flow action (at {loc_str}) needs a `verb` key "
                "(call_binding | api | invoke_tool | emit | navigate | set | toast)"
            )

    # generic fallback — name the first offending field, drop the URL noise.
    if errors:
        first = errors[0]
        loc_str = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
        msg = first.get("msg", "invalid value")
        return f"descriptor is malformed at `{loc_str}`: {msg}"
    return f"descriptor is malformed: {exc}"


def _repair_descriptor(model: FlowDescriptor) -> None:
    """PATCH recoverable graph defects on the parsed model IN PLACE, before the
    strict validate-graph pass (§2.0 — genesis stays bulletproof by repairing
    imperfect specs, not rejecting them).

    Repairs (each turns a silent dead-end into a completable flow), kept
    SURGICAL so they never MASK a genuine bug:
      1. A terminal step of kind confirm/info/form (no `next`/`branch`) with no
         `complete` AND no flow-level `complete` → inject a default `chat`
         complete. (A bare dead-end SELECT is NOT auto-rescued here — only as a
         LAST step, via repair 3 — so a mid-flow dead-end select still rejects.)
      2. A flow where EVERY step transitions (no terminal at all) AND the
         last-declared step's transition targets are all DECLARED → make that
         step terminal (drop its transition) + give it a default `complete`.
         (Guarded on declared targets so a DANGLING transition is left for the
         validator to reject precisely, not silently erased.)
      3. A `select`/`info` step that dead-ends (no next/branch/complete) AND is
         the LAST declared step → convert to terminal with a default `complete`
         (instead of the historical hard reject).

    Genuinely unrenderable bugs (dangling transition, duplicate id, bad branch
    key, a mid-flow dead-end select) are LEFT for the strict validators to
    reject precisely — repair only touches the recoverable dead-end class.
    """
    steps = model.steps
    if not steps:
        return
    last = steps[-1]
    declared = {s.id for s in steps}

    # Repair 3: a select/info LAST step with no transition and no complete
    # becomes terminal with a default complete. (A select normally must
    # transition; as the last step with nowhere to go, the forgiving move is to
    # end the flow rather than reject the whole build.)
    if (
        last.kind in ("select", "info")
        and not last.next
        and not last.branch
        and last.complete is None
        and model.complete is None
    ):
        last.complete = TerminalAction.model_validate(dict(_DEFAULT_COMPLETE))

    # Repair 2: NO terminal anywhere (every step transitions) → make the last
    # step terminal by dropping its transition. GUARD: only when the last step's
    # transition targets are all declared — a dangling transition is a real bug
    # the validator must surface, not something repair should silently erase.
    has_terminal = any(_is_terminal_step(s) for s in steps)
    if not has_terminal and _transition_targets_declared(last, declared):
        last.next = None
        last.branch = None
        if last.complete is None and model.complete is None:
            last.complete = TerminalAction.model_validate(dict(_DEFAULT_COMPLETE))

    # Repair 1: a terminal confirm/info/form step lacking a complete (no
    # flow-level default) → inject the default so the terminal has a hand-off.
    # Selects are excluded: a bare dead-end select is only ever rescued as a
    # LAST step (repair 3); a mid-flow one must reject. When a flow-level default
    # exists, attach-terminal inherits it — leave those steps alone.
    if model.complete is None:
        for step in steps:
            if (
                step.kind in ("confirm", "info", "form")
                and _is_terminal_step(step)
                and step.complete is None
            ):
                step.complete = TerminalAction.model_validate(dict(_DEFAULT_COMPLETE))


def _transition_targets_declared(step: FlowStep, declared: set[str]) -> bool:
    """True if every id `step` transitions to (`next` / `branch` values) is a
    declared step id. Used to GUARD repair 2 so it never erases a dangling
    transition (a real bug the validator should reject)."""
    if step.next is not None and step.next not in declared:
        return False
    if step.branch:
        return all(target in declared for target in step.branch.values())
    return True


def build_flow_from_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Flat FlowDescriptor → {version, ui} nested Chain Flow doc.

    Implements the §2.1 ordered pipeline (genesis forgiveness front-loaded):
        normalize-aliases → parse → repair → index → validate-graph
        → validate-refs → validate-actions → assemble-nodes
        → resolve-transitions → wire-prefills → attach-actions
        → attach-terminal → final deep-validate → envelope

    Forgiveness, NOT rejection, for recoverable author slips (genesis's
    reliability philosophy):
      - `normalize-aliases` (§2.0) rewrites the model's instinctive key slips
        before parse — a terminal `complete` written with `type:`/`kind:` →
        `action:`, a StepAction/continuation written with `type:` → `verb:`
        (the rest of ripple uses `type`/`kind` for NODES, so the model reaches
        for them here too). The model's instinct just works.
      - `repair` (§2.0) PATCHES recoverable graph defects instead of raising:
        a terminal step with no `complete` gets a default `chat`; a flow whose
        every step transitions has its last-declared step made terminal; a
        dead-ending `select`/`info` last step is converted to terminal.

    Raises :class:`FlowBuildError` (with a precise, agent-readable message) ONLY
    for genuinely unrenderable authoring bugs the repair pass leaves untouched:
    a `next`/`branch` to an UNDECLARED id (dangling transition), a duplicate
    step id, a branch key that is not an option id, an unknown action verb, a
    dangling prefill ref. The tool layer surfaces the message so the model can
    fix and retry. A friendly :class:`FlowBuildError` (not a raw Pydantic dump)
    wraps any shape defect that survives normalization — it NAMES the likely fix.

    A flat graph cannot mis-nest — that is the whole point. Most "sloppy"
    descriptors now BUILD (repaired) and render something polished; only real
    structural bugs error.
    """
    # 0. normalize-aliases (§2.0) — rewrite type:/kind: slips to action:/verb:
    #    on a deep copy so the caller's dict is never mutated.
    descriptor = _normalize_descriptor(descriptor)

    # 1. parse — wrap any surviving shape defect in a FRIENDLY FlowBuildError
    #    that names the likely fix instead of leaking a raw Pydantic error URL.
    try:
        model = FlowDescriptor.model_validate(descriptor)
    except Exception as exc:  # pydantic.ValidationError + anything malformed
        raise FlowBuildError(_friendly_parse_error(exc)) from exc

    # 2. repair (§2.0) — PATCH recoverable graph defects before the strict
    #    validate-graph pass (genesis: repair imperfect specs, don't reject).
    _repair_descriptor(model)

    if not model.steps:
        raise FlowBuildError("flow has no steps; a flow needs at least one step")
    if len(model.steps) > _MAX_STEPS:
        raise FlowBuildError(
            f"flow has {len(model.steps)} steps — over the {_MAX_STEPS}-step cap; "
            "split the flow into smaller flows"
        )

    # 3. index -------------------------------------------------------------
    by_id: dict[str, FlowStep] = {}
    for step in model.steps:
        if step.id in by_id:
            raise FlowBuildError(f'duplicate step id "{step.id}"; step ids must be unique')
        by_id[step.id] = step

    # 4. validate-graph (§2.3) --------------------------------------------
    _validate_graph(model, by_id)

    # Build the slot→(flowId, suffix) map for ref resolution/rewrite. A token is
    # resolvable by step id AND by slot name (when a slot is declared).
    slot_kind: dict[str, str] = {}
    for step in model.steps:
        if step.kind not in ("select", "form"):
            continue
        suffix = "selection" if step.kind == "select" else "formData"
        fid = _flow_id_of(step)
        slot_kind[step.id] = f"{fid}|{suffix}"
        if step.slot:
            slot_kind[step.slot] = f"{fid}|{suffix}"

    # 5. validate-refs (§2.5) ---------------------------------------------
    warnings = _validate_refs(model, by_id)

    # 6. validate-actions (§2.4) ------------------------------------------
    _validate_actions(model)

    # 7. assemble-nodes ----------------------------------------------------
    reachable = _reachable_ids(model, by_id)
    nodes: dict[str, dict[str, Any]] = {
        step.id: _assemble_node(step, is_entry=(step.id == model.entry))
        for step in model.steps
        if step.id in reachable
    }

    # 8. resolve-transitions (§2.2) — flat pointers → nested object refs ---
    for step in model.steps:
        if step.id not in reachable:
            continue
        node = nodes[step.id]
        if step.branch:
            node["chain_map"] = {sel: nodes[target] for sel, target in step.branch.items()}
        elif step.next:
            node["chain"] = nodes[step.next]
        # else: terminal — no chain/chain_map; onComplete attached in step 10.

    # 9. wire-prefills — rewrite {stepId.field} sugar into namespaced keys --
    for step in model.steps:
        if step.id not in reachable:
            continue
        node = nodes[step.id]
        if "review_rows" in node:
            node["review_rows"] = _rewrite_in_obj(node["review_rows"], slot_kind)
        # info body copy lives in the ui text; rewritten via the ui pass below.
        node["ui"] = _rewrite_in_obj(node["ui"], slot_kind)
        # attach-actions — already lowered in assemble; rewrite their refs.
        # (assemble appended action buttons to ui, so the ui rewrite covers them)

    # 10. attach-terminal --------------------------------------------------
    for step in model.steps:
        if step.id not in reachable:
            continue
        if not _is_terminal_step(step):
            continue
        terminal = step.complete or model.complete
        # _validate_graph guarantees a terminal has a complete (own or default);
        # the guard keeps the type checker honest and is a cheap belt anyway.
        if terminal is None:
            raise FlowBuildError(
                f'terminal step "{step.id}" has no complete action and no flow-level default'
            )
        node = nodes[step.id]
        node["onComplete"] = _lower_terminal(terminal, slot_kind)

    root = nodes[model.entry]
    doc = {"version": INLINE_SPEC_VERSION, "ui": root}

    # 11. final deep-validate — belt-and-suspenders on widget/verb content --
    # Imported here to keep the module's import graph light and avoid a cycle.
    from pocketpaw.ripple.manifest import validate_action_verbs

    verb_issues = validate_action_verbs(doc)
    if verb_issues:
        bad = ", ".join(sorted({str(i.get("action")) for i in verb_issues}))
        raise FlowBuildError(
            f"assembled flow has unknown action verb(s): {bad}; use a known ripple action verb"
        )

    # Stash any soft warnings on the doc so the tool layer can relay them
    # without failing the build (§2.5 single-branch reachability).
    if warnings:
        doc["_warnings"] = warnings

    # 12. envelope ---------------------------------------------------------
    return doc


def _is_terminal_step(step: FlowStep) -> bool:
    """A step with neither `next` nor `branch` is terminal."""
    return not step.next and not step.branch


def _reachable_ids(model: FlowDescriptor, by_id: dict[str, FlowStep]) -> set[str]:
    """BFS from `entry` over next ∪ branch.values() — the reachable step ids."""
    seen: set[str] = set()
    if model.entry not in by_id:
        return seen
    queue = [model.entry]
    while queue:
        sid = queue.pop()
        if sid in seen:
            continue
        seen.add(sid)
        step = by_id[sid]
        if step.next and step.next in by_id:
            queue.append(step.next)
        if step.branch:
            for target in step.branch.values():
                if target in by_id:
                    queue.append(target)
    return seen


# ---------------------------------------------------------------------------
# Graph validation (§2.3) — each invariant rejects with a precise message.
# ---------------------------------------------------------------------------


def _validate_graph(model: FlowDescriptor, by_id: dict[str, FlowStep]) -> None:
    declared = sorted(by_id)

    # entry exists
    if model.entry not in by_id:
        raise FlowBuildError(f'entry "{model.entry}" is not a step id; declared: {declared}')

    for step in model.steps:
        has_next = bool(step.next)
        has_branch = bool(step.branch)

        # both next and branch on one step is ambiguous
        if has_next and has_branch:
            raise FlowBuildError(
                f'step "{step.id}" has both `next` and `branch`; a step transitions '
                "exactly one way — use one or the other"
            )

        # every next target exists
        if has_next and step.next not in by_id:
            raise FlowBuildError(
                f'step "{step.id}".next → "{step.next}" but "{step.next}" is not a declared step'
            )

        # every branch value exists; every branch key matches an option id
        if step.branch:
            option_ids = {str(o.get("id")) for o in (step.options or []) if o.get("id")}
            for sel, target in step.branch.items():
                if target not in by_id:
                    raise FlowBuildError(
                        f'step "{step.id}".branch["{sel}"] → "{target}" but "{target}" '
                        "is not declared"
                    )
                if step.kind == "select" and sel not in option_ids:
                    raise FlowBuildError(
                        f'step "{step.id}".branch key "{sel}" is not one of {step.id}\'s '
                        f"option ids {sorted(option_ids)}"
                    )

        # a select step must transition (branch or next) — else it dead-ends.
        # EXCEPTION: a select the repair pass converted to TERMINAL carries a
        # `complete` (it is the last step and ends the flow); that is allowed.
        if step.kind == "select" and not has_next and not has_branch and step.complete is None:
            raise FlowBuildError(
                f'select step "{step.id}" has no branch/next; it would dead-end after a pick'
            )

        # no non-terminal step carries `complete` (complete is terminal-only)
        if (has_next or has_branch) and step.complete is not None:
            raise FlowBuildError(
                f'step "{step.id}" has both a transition and a complete action; '
                "complete is terminal-only"
            )

    # reachability — every declared step reachable from entry
    reachable = _reachable_ids(model, by_id)
    for step in model.steps:
        if step.id not in reachable:
            raise FlowBuildError(f'step "{step.id}" is unreachable from entry "{model.entry}"')

    # at least one terminal step (among the reachable)
    terminals = [by_id[sid] for sid in reachable if _is_terminal_step(by_id[sid])]
    if not terminals:
        raise FlowBuildError(
            "flow has no terminal step — every step transitions onward; a flow must end"
        )

    # every terminal has a complete (own or flow-level default) — §7 D3
    for term in terminals:
        if term.complete is None and model.complete is None:
            raise FlowBuildError(
                f'terminal step "{term.id}" has no complete action and no flow-level default'
            )

    # cycle check — the reachable subgraph must be a DAG
    _detect_cycle(model, by_id, reachable)


def _detect_cycle(model: FlowDescriptor, by_id: dict[str, FlowStep], reachable: set[str]) -> None:
    """DFS over next ∪ branch on the reachable subgraph; raise on a back-edge."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {sid: WHITE for sid in reachable}

    def _succ(sid: str) -> list[str]:
        step = by_id[sid]
        out: list[str] = []
        if step.next and step.next in reachable:
            out.append(step.next)
        if step.branch:
            out.extend(t for t in step.branch.values() if t in reachable)
        return out

    def _visit(sid: str, stack: list[str]) -> None:
        color[sid] = GRAY
        stack.append(sid)
        for nxt in _succ(sid):
            if color[nxt] == GRAY:
                # back-edge — reconstruct the cycle path for the message
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                raise FlowBuildError(f"cycle detected: {' → '.join(cycle)}; flows must be a DAG")
            if color[nxt] == WHITE:
                _visit(nxt, stack)
        stack.pop()
        color[sid] = BLACK

    if model.entry in reachable:
        _visit(model.entry, [])


# ---------------------------------------------------------------------------
# Prefill-ref validation (§2.5).
# ---------------------------------------------------------------------------


def _validate_refs(model: FlowDescriptor, by_id: dict[str, FlowStep]) -> list[str]:
    """Validate every `{stepId.field}` ref; return soft warnings (hard errors
    raise). A ref may resolve by step id or by slot name.

    - unknown step/slot → hard FlowBuildError.
    - field not declared on that step → hard FlowBuildError.
    - referenced step on one branch only, referencing step reachable from both
      → soft warning (the value renders empty on the other branch, sometimes
      intended). Hard error only if unreachable on EVERY path.
    """
    warnings: list[str] = []

    # token → the FlowStep it resolves to (by id or slot). A slot shared by two
    # branch steps maps to the FIRST declarer for field-set purposes; both
    # branch steps that share a slot must declare the same fields for the
    # branch-agnostic read to make sense, so the first is representative.
    token_step: dict[str, FlowStep] = {}
    for step in model.steps:
        token_step.setdefault(step.id, step)
        if step.slot:
            token_step.setdefault(step.slot, step)

    # Map each reachable step → which entry-options/branch it sits under, so we
    # can warn on single-branch reachability. Approximate "on one branch only":
    # a step reached only via a specific branch key of the entry select.
    branch_owner = _branch_membership(model, by_id)

    # token → the set of distinct branch-owners that WRITE its flowId slot. A
    # shared-slot read (the §2.2 pattern, e.g. `{financials.headline}` written by
    # both branch forms) is branch-agnostic, so it must NOT warn even though each
    # individual writer step is single-branch. We suppress the warning whenever a
    # token's slot is written on more than one branch-owner.
    slot_writers: dict[str, set[str | None]] = {}
    for step in model.steps:
        if step.kind not in ("select", "form"):
            continue
        owner = branch_owner.get(step.id)
        tokens = [step.id] + ([step.slot] if step.slot else [])
        for tok in tokens:
            slot_writers.setdefault(tok, set()).add(owner)

    for step in model.steps:
        # gather every ref-bearing string on this step: review values, info body,
        # action arg/param/body payloads.
        ref_strings: list[str] = []
        for row in step.review or []:
            v = row.get("value")
            if isinstance(v, str):
                ref_strings.append(v)
        if step.body:
            ref_strings.append(step.body)
        for action in step.actions or []:
            _collect_action_refs(action, ref_strings)

        for text in ref_strings:
            for token, field in _ref_targets(text):
                target = token_step.get(token)
                if target is None:
                    raise FlowBuildError(
                        f'prefill ref {{{token}.{field}}} points at unknown step/slot "{token}"'
                    )
                declared = _declared_fields(target)
                # only check the FIRST field segment against declarations
                head = field.split(".", 1)[0]
                if declared and head not in declared:
                    raise FlowBuildError(
                        f'prefill ref {{{token}.{field}}} — step "{token}" declares no '
                        f'field "{head}" (has: {sorted(declared)})'
                    )
                # single-branch reachability warning. Skip it when the token's
                # slot is written on more than one branch (the §2.2 shared-slot
                # pattern makes the read branch-agnostic — that is exactly its
                # purpose, so warning there would be noise).
                writers = slot_writers.get(token, set())
                multi_branch_slot = len({w for w in writers if w is not None}) > 1
                owner = branch_owner.get(target.id)
                ref_owner = branch_owner.get(step.id)
                if owner is not None and owner != ref_owner and not multi_branch_slot:
                    warnings.append(
                        f'prefill ref {{{token}.{field}}} in step "{step.id}" reads step '
                        f'"{target.id}", which is only reachable on the "{owner}" branch; '
                        "it renders empty on other branches (use a shared `slot` to make "
                        "it branch-agnostic)"
                    )

    return warnings


def _collect_action_refs(action: StepAction, out: list[str]) -> None:
    """Collect ref-bearing strings from an action's payloads (recursively into
    on_success / on_error)."""
    for payload in (action.args, action.params, action.body):
        if payload:
            _collect_ref_strings(payload, out)
    for cont in (action.on_success or []) + (action.on_error or []):
        _collect_continuation_refs(cont, out)


def _collect_continuation_refs(cont: StepContinuation, out: list[str]) -> None:
    """Collect ref-bearing strings from a continuation's payloads (recursively
    into its own on_success / on_error). Mirrors `_collect_action_refs`; a
    continuation has the same payload fields, just no id/label."""
    for payload in (cont.args, cont.params, cont.body):
        if payload:
            _collect_ref_strings(payload, out)
    if cont.value is not None and isinstance(cont.value, str):
        out.append(cont.value)
    for nested in (cont.on_success or []) + (cont.on_error or []):
        _collect_continuation_refs(nested, out)


def _branch_membership(model: FlowDescriptor, by_id: dict[str, FlowStep]) -> dict[str, str | None]:
    """Map step id → the entry-branch key it is EXCLUSIVELY reachable under, or
    None if reachable on every branch / via a linear path.

    Used only for the §2.5 single-branch-reachability warning. Computed by, for
    each branch of the entry select, BFS-collecting the steps reachable from
    that branch; a step in exactly one branch's set (and not in the linear
    pre-branch set) is owned by that branch.
    """
    entry = by_id.get(model.entry)
    if entry is None or not entry.branch:
        # No top-level branch — nothing is single-branch-owned.
        return {}

    branch_sets: dict[str, set[str]] = {}
    for sel, target in entry.branch.items():
        seen: set[str] = set()
        queue = [target]
        while queue:
            sid = queue.pop()
            if sid in seen or sid not in by_id:
                continue
            seen.add(sid)
            st = by_id[sid]
            if st.next:
                queue.append(st.next)
            if st.branch:
                queue.extend(st.branch.values())
        branch_sets[sel] = seen

    owner: dict[str, str | None] = {}
    all_ids = set(by_id)
    for sid in all_ids:
        owning = [sel for sel, members in branch_sets.items() if sid in members]
        owner[sid] = owning[0] if len(owning) == 1 else None
    # the entry itself is shared
    owner[model.entry] = None
    return owner


# ---------------------------------------------------------------------------
# Action validation (§2.4).
# ---------------------------------------------------------------------------


def _validate_actions(model: FlowDescriptor) -> None:
    for step in model.steps:
        for action in step.actions or []:
            _validate_step_action(action, step.id)
        if step.complete is not None:
            _validate_terminal_action(step.complete, step.id)
    if model.complete is not None:
        _validate_terminal_action(model.complete, "<flow default>")


def _validate_step_action(action: StepAction, step_id: str, depth: int = 0) -> None:
    if action.verb not in _STEP_ACTION_VERBS:
        raise FlowBuildError(
            f'step "{step_id}" action "{action.id}" uses unknown verb "{action.verb}"; '
            f"allowed: {sorted(_STEP_ACTION_VERBS)}"
        )
    for key in _STEP_VERB_REQUIRED.get(action.verb, ()):  # required payload keys
        if getattr(action, key, None) in (None, ""):
            raise FlowBuildError(
                f'step "{step_id}" action "{action.id}" (verb "{action.verb}") '
                f'is missing required key "{key}"'
            )
    if depth >= _MAX_THEN_DEPTH + 2:
        raise FlowBuildError(
            f'step "{step_id}" action "{action.id}" nests continuations too deeply'
        )
    # Continuations are NOT buttons — recurse the SAME per-verb required-key
    # checks (no id/label demanded). The action's id is carried into the message
    # so a defective continuation still names the button it hangs off.
    for cont in (action.on_success or []) + (action.on_error or []):
        _validate_continuation(cont, step_id, action.id, depth + 1)


def _validate_continuation(
    cont: StepContinuation, step_id: str, action_id: str, depth: int = 1
) -> None:
    """Validate one `on_success` / `on_error` continuation — same verb-allow-set
    and per-verb required-key table as a StepAction, but without requiring the
    button-only `id` / `label`. Recurses into nested continuations."""
    if cont.verb not in _STEP_ACTION_VERBS:
        raise FlowBuildError(
            f'step "{step_id}" action "{action_id}" continuation uses unknown verb '
            f'"{cont.verb}"; allowed: {sorted(_STEP_ACTION_VERBS)}'
        )
    for key in _STEP_VERB_REQUIRED.get(cont.verb, ()):  # required payload keys
        if getattr(cont, key, None) in (None, ""):
            raise FlowBuildError(
                f'step "{step_id}" action "{action_id}" continuation (verb "{cont.verb}") '
                f'is missing required key "{key}"'
            )
    if depth >= _MAX_THEN_DEPTH + 2:
        raise FlowBuildError(
            f'step "{step_id}" action "{action_id}" nests continuations too deeply'
        )
    for nested in (cont.on_success or []) + (cont.on_error or []):
        _validate_continuation(nested, step_id, action_id, depth + 1)


def _validate_terminal_action(terminal: TerminalAction, step_id: str, depth: int = 1) -> None:
    if terminal.action not in _TERMINAL_ACTIONS:
        raise FlowBuildError(
            f'terminal of step "{step_id}" uses unknown action "{terminal.action}"; '
            f"allowed: {sorted(_TERMINAL_ACTIONS)}"
        )
    for key in _TERMINAL_REQUIRED.get(terminal.action, ()):
        if getattr(terminal, key, None) in (None, ""):
            raise FlowBuildError(
                f'terminal of step "{step_id}" (action "{terminal.action}") '
                f'is missing required key "{key}"'
            )
    if terminal.then is not None:
        if depth + 1 > _MAX_THEN_DEPTH:
            raise FlowBuildError(
                f'terminal of step "{step_id}" chains `then` deeper than '
                f"{_MAX_THEN_DEPTH}; cap the chain at an action plus one post-action"
            )
        _validate_terminal_action(terminal.then, step_id, depth + 1)


# ---------------------------------------------------------------------------
# Node assembly (§2.1 step 6) — one bare UniversalSpec node per step. REUSES the
# `_select_button` / `_continue_button` / `_submit_button` / `_back_button`
# helpers so buttons are byte-identical to the presets (§7 D4).
# ---------------------------------------------------------------------------

_KIND_TO_INTENT = {"select": "select", "form": "form", "confirm": "confirm", "info": "info"}


def _assemble_node(step: FlowStep, *, is_entry: bool) -> dict[str, Any]:
    """Build a bare UniversalSpec node for `step` (no chain/chain_map yet —
    transitions are spliced in resolve-transitions).

    The node carries `flowId` (slot or id), the kind→intent, structured
    `form_fields` / `review_rows` where applicable, and a raw `ui` widget tree
    whose buttons reuse the shared helpers. A non-entry form step gets a Back
    button (the intermediate-Back convention the presets and tests expect).
    """
    flow_id = _flow_id_of(step)
    intent = _KIND_TO_INTENT[step.kind]
    node: dict[str, Any] = {
        "version": _STEP_VERSION,
        "id": step.id,
        "flowId": flow_id,
        "intent": intent,
        "title": step.title or step.id,
    }
    if step.kind == "select":
        node["selection"] = "single"

    children: list[dict[str, Any]] = []
    title = step.title or step.id
    children.append(_heading(title))
    if step.subtitle:
        children.append(_text(step.subtitle))

    if step.kind == "select":
        for opt in step.options or []:
            oid = str(opt.get("id"))
            olabel = str(opt.get("label") or oid)
            children.append(_select_button(olabel, oid))
        # A select is normally non-terminal (it branches). When the repair pass
        # made a dead-ending LAST select terminal, it carries no transition — add
        # a Finish (flow.submit) so the recorded pick can run the `complete`,
        # rather than leaving option buttons that emit flow.next with nowhere to
        # go. The common branching select never reaches this (it has a chain_map).
        if _is_terminal_step(step):
            children.append(_submit_button("Finish"))

    elif step.kind == "form":
        binds: list[str] = []
        form_fields: list[dict[str, Any]] = []
        for f in step.fields or []:
            fid = str(f.get("id"))
            flabel = str(f.get("label") or fid)
            placeholder = str(f.get("placeholder") or "")
            ftype = _field_type(f)
            required = bool(f.get("required", True))
            opts = f.get("options") if isinstance(f.get("options"), list) else None
            children.append(_input(fid, flabel, placeholder))
            binds.append(fid)
            form_fields.append(_form_field(fid, flabel, placeholder, ftype, required, opts))
        node["form_fields"] = form_fields
        # action buttons (mid-flow) sit above the nav row
        for action in step.actions or []:
            children.append(_action_button(action))
        # nav row: Back (only when not the entry) precedes Continue (§7 D4)
        nav_children: list[dict[str, Any]] = []
        if not is_entry:
            nav_children.append(_back_button("Back"))
        nav_children.append(_continue_button("Continue", binds))
        children.append(_nav_row(nav_children))

    elif step.kind == "confirm":
        review_rows: list[dict[str, Any]] = []
        for row in step.review or []:
            label = str(row.get("label") or "")
            value = str(row.get("value") or "")
            review_rows.append(_review_row(label, value))
            children.append(_text(f"{label}: {value}"))
        node["review_rows"] = review_rows
        for action in step.actions or []:
            children.append(_action_button(action))
        children.append(_submit_button("Finish"))

    elif step.kind == "info":
        if step.body:
            children.append(_text(step.body))
        for action in step.actions or []:
            children.append(_action_button(action))
        # An info step that is terminal gets a Finish; otherwise a Continue.
        if _is_terminal_step(step):
            children.append(_submit_button("Finish"))
        else:
            nav_children = []
            if not is_entry:
                nav_children.append(_back_button("Back"))
            # info Continue carries no formData — bare flow.next
            nav_children.append(
                {
                    "type": "button",
                    "props": {"label": "Continue"},
                    "on_click": {"action": "emit", "target": "flow.next", "value": {}},
                }
            )
            children.append(_nav_row(nav_children))

    # A non-form, non-info step that is ALSO terminal (a confirm) already added a
    # submit button above. A select step is never terminal (graph invariant).
    node["ui"] = _container(children, cls=f"flow-{step.kind}")
    return node


# ---------------------------------------------------------------------------
# Terminal lowering (§1.4 / §3 / §7 D1). A descriptor `complete` TerminalAction
# → the node's `onComplete` FlowAction (ripple's union). Existing kinds map
# `action`→`kind`; new kinds carry their payload; an optional `then` lowers
# recursively (chain capped at 2 by the validator).
# ---------------------------------------------------------------------------


def _lower_terminal(terminal: TerminalAction, slot_kind: dict[str, str]) -> dict[str, Any]:
    """Lower a TerminalAction into the ripple `onComplete` FlowAction shape.

    The descriptor uses `action` as the discriminator; ripple's FlowAction uses
    `kind`. The mapping is 1:1 by name. `{stepId.field}` / `{flow.payload}` refs
    in payloads are rewritten with `slot_kind` (passthrough tokens untouched).
    """
    action = terminal.action
    out: dict[str, Any] = {"kind": action}

    if action == "chat":
        out["message"] = terminal.message or ""
    elif action == "navigate":
        out["url"] = _rewrite_refs(terminal.url or "", slot_kind)
    elif action == "emit":
        if terminal.event is not None:
            out["event"] = terminal.event
        if terminal.payload is not None:
            out["payload"] = _rewrite_in_obj(terminal.payload, slot_kind)
    elif action == "invoke_tool":
        out["tool"] = terminal.tool
        if terminal.args is not None:
            out["args"] = _rewrite_in_obj(terminal.args, slot_kind)
    elif action == "call_binding":
        out["binding"] = terminal.binding
        out["path"] = _rewrite_refs(terminal.path or "", slot_kind)
        if terminal.params is not None:
            out["params"] = _rewrite_in_obj(terminal.params, slot_kind)
    elif action == "create_pocket":
        out["name"] = _rewrite_refs(terminal.name or "", slot_kind)
        if terminal.template is not None:
            out["template"] = terminal.template
        if terminal.spec is not None:
            out["spec"] = _rewrite_in_obj(terminal.spec, slot_kind)
        if terminal.seed_from_flow is not None:
            out["seed_from_flow"] = terminal.seed_from_flow

    if terminal.then is not None:
        out["then"] = _lower_terminal(terminal.then, slot_kind)
    return out


# ---------------------------------------------------------------------------
# Preset 1 — Onboarding wizard (NON-COMMERCE proof, RFC 13 M1/M3). Now emits a
# FLAT descriptor and delegates to build_flow_from_descriptor (§2.6). The
# shared-slot pattern ("enter_details") keeps both branches' flowId identical so
# the confirm step's {state.enter_details_formData.workspace} read resolves on
# either path — byte-identical to the pre-v2 hand-built tree.
#
#   Step 1 (root):  pick a goal           — branches via chain_map
#   Step 2a:        focus → workspace name (form)        ┐ slot: enter_details
#   Step 2b:        collaborate → team workspace (form)  ┘ both next → confirm
#   Step 3 (term):  confirm — pre-filled from steps 1+2, onComplete.chat
# ---------------------------------------------------------------------------


def _onboarding_descriptor(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    product_name = str(config.get("product_name") or "your workspace")
    complete_message = str(
        config.get("complete_message")
        or "I've finished onboarding — here are my choices, please set up my workspace."
    )
    default_goals = [
        {"id": "focus", "label": "Focus on my own work"},
        {"id": "collaborate", "label": "Collaborate with a team"},
    ]
    goals = config.get("goals") or default_goals

    # Branch each goal id → the step-2 it routes to. `collaborate` gets the
    # invite-flavored step; everything else shares the focus step. Both step-2s
    # share slot "enter_details" so the confirm read-back is branch-agnostic.
    branch: dict[str, str] = {}
    for goal in goals:
        gid = str(goal.get("id"))
        branch[gid] = "onboard-invite" if gid == "collaborate" else "onboard-details"

    return {
        "flow": "onboarding_wizard",
        "entry": "onboard-goal",
        "title": "What brings you here?",
        "steps": [
            {
                "id": "onboard-goal",
                "kind": "select",
                "slot": "pick_goal",
                "title": "What brings you here?",
                "subtitle": "Pick your primary goal to get started.",
                "options": [
                    {"id": str(g.get("id")), "label": str(g.get("label") or g.get("id"))}
                    for g in goals
                ],
                "branch": branch,
                # Welcome copy lives on the heading via the title; brand it in.
                "ui": {"_brand": product_name},
            },
            {
                "id": "onboard-details",
                "kind": "form",
                "slot": "enter_details",
                "title": "Set up your workspace",
                "fields": [
                    {
                        "id": "workspace",
                        "label": "Workspace name",
                        "type": "text",
                        "placeholder": "Acme HQ",
                        "required": True,
                    },
                ],
                "next": "onboard-confirm",
            },
            {
                "id": "onboard-invite",
                "kind": "form",
                "slot": "enter_details",
                "title": "Set up your workspace",
                "fields": [
                    {
                        "id": "workspace",
                        "label": "Workspace name",
                        "type": "text",
                        "placeholder": "Acme Team",
                        "required": True,
                    },
                ],
                "next": "onboard-confirm",
            },
            {
                "id": "onboard-confirm",
                "kind": "confirm",
                "slot": "confirm",
                "title": "You are all set",
                "review": [
                    {"label": "Goal", "value": "{pick_goal.label}"},
                    {"label": "Workspace", "value": "{enter_details.workspace}"},
                ],
                "complete": {"action": "chat", "message": complete_message},
            },
        ],
    }


def build_onboarding_wizard(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the onboarding-wizard flow as a `{version, ui}` doc.

    Emits a flat descriptor (`_onboarding_descriptor`) and delegates to
    `build_flow_from_descriptor`. `config` is the optional descriptor override.
    Recognized keys (all optional — each has a default so a bare `flow_type`
    works): `product_name`, `goals` (list[{id, label}]), `complete_message`.
    """
    doc = build_flow_from_descriptor(_onboarding_descriptor(config))
    # Brand the welcome copy onto the root heading/subtitle (the descriptor's
    # _brand marker is the only preset-specific touch the flat schema can't carry
    # natively — the heading text is "What brings you here?", the welcome line is
    # injected as the first body text so the existing product-name copy assertion
    # holds without changing the generic builder).
    root = doc["ui"]
    config = config or {}
    product_name = str(config.get("product_name") or "your workspace")
    _inject_welcome(root, f"Welcome to {product_name}")
    # remove the descriptor marker if it leaked into the assembled ui
    root.get("ui", {}).pop("_brand", None)
    return doc


def _inject_welcome(root_node: dict[str, Any], welcome: str) -> None:
    """Prepend a welcome line to the root step's ui (after the heading) so the
    onboarding preset keeps its branded copy. Idempotent enough for one call."""
    ui = root_node.get("ui")
    if not isinstance(ui, dict):
        return
    children = ui.get("children")
    if not isinstance(children, list):
        return
    # insert right after the leading heading
    children.insert(1, _text(welcome))


# ---------------------------------------------------------------------------
# Preset 2 — Due-diligence intake (NON-COMMERCE vertical, RFC 13 §7.1, M3). Now
# emits a FLAT descriptor and delegates. Shared slot "financials" keeps both
# branch forms' flowId identical so {financials.headline} reads branch-
# agnostically — byte-identical to the pre-v2 hand-built tree.
#
#   Step 1 (root):  pick the deal stage     — branches via chain_map
#   Step 2a:        early → traction + raise (form)  ┐ slot: financials
#   Step 2b:        growth → revenue + metrics (form)┘ both next → risk
#   Step 3:         risk & flags (form)              → review
#   Step 4 (term):  review — pre-filled, onComplete.chat
# ---------------------------------------------------------------------------


def _diligence_descriptor(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    company_name = str(config.get("company_name") or "the company")
    complete_message = str(
        config.get("complete_message")
        or "Diligence intake complete — here are my inputs, please summarize and flag risks."
    )
    return {
        "flow": "due_diligence_intake",
        "entry": "dd-stage",
        "title": "Due-diligence intake",
        "steps": [
            {
                "id": "dd-stage",
                "kind": "select",
                "slot": "deal_stage",
                "title": "Due-diligence intake",
                "subtitle": f"Diligence intake for {company_name} — pick the deal stage.",
                "options": [
                    {"id": "early", "label": "Early stage (pre-seed / seed)"},
                    {"id": "growth", "label": "Growth stage (Series A+)"},
                ],
                "branch": {"early": "dd-financials-early", "growth": "dd-financials-growth"},
            },
            {
                "id": "dd-financials-early",
                "kind": "form",
                "slot": "financials",
                "title": "Financial snapshot",
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
                        "required": True,
                    },
                ],
                "next": "dd-risk",
            },
            {
                "id": "dd-financials-growth",
                "kind": "form",
                "slot": "financials",
                "title": "Financial snapshot",
                "fields": [
                    {
                        "id": "headline",
                        "label": "Headline revenue metric",
                        "type": "text",
                        "placeholder": "$4M ARR",
                        "required": True,
                    },
                    {
                        "id": "growth",
                        "label": "YoY growth",
                        "type": "text",
                        "placeholder": "180%",
                        "required": True,
                    },
                ],
                "next": "dd-risk",
            },
            {
                "id": "dd-risk",
                "kind": "form",
                "slot": "risk_review",
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
                        "placeholder": "Diversifying pipeline",
                        "required": False,
                    },
                ],
                "next": "dd-review",
            },
            {
                "id": "dd-review",
                "kind": "confirm",
                "slot": "review",
                "title": "Review the intake",
                "review": [
                    {"label": "Deal stage", "value": "{deal_stage.label}"},
                    {"label": "Headline metric", "value": "{financials.headline}"},
                    {"label": "Key risk", "value": "{risk_review.key_risk}"},
                ],
                "complete": {"action": "chat", "message": complete_message},
            },
        ],
    }


def build_due_diligence_intake(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the due-diligence intake flow as a `{version, ui}` doc.

    Emits a flat descriptor (`_diligence_descriptor`) and delegates to
    `build_flow_from_descriptor`. `config` overrides (all optional):
    `company_name`, `complete_message`. The legacy `submit_event` key is
    accepted but ignored (the terminal loops to the agent via onComplete.chat,
    so there is no host event to override).
    """
    return build_flow_from_descriptor(_diligence_descriptor(config))


# ---------------------------------------------------------------------------
# Dispatch — `flow_type` -> preset descriptor -> build_flow_from_descriptor.
# `domain` is accepted as a forward-compatible hint (RFC 13 §7.1) but does not
# change the tree today.
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
    """Expand a PRESET descriptor into a full `{version, ui}` Chain Flow doc.

    Raises ``ValueError`` for an unknown ``flow_type`` (the tool layer turns
    that into an agent-readable error listing the valid presets). For an
    arbitrary flat graph, call :func:`build_flow_from_descriptor` directly.
    """
    builder = _BUILDERS.get(flow_type)
    if builder is None:
        valid = ", ".join(sorted(_BUILDERS))
        raise ValueError(f"Unknown flow_type {flow_type!r}. Known templates: {valid}.")
    return builder(config)


__all__ = [
    "FLOW_TYPES",
    "INLINE_SPEC_VERSION",
    "FlowBuildError",
    "FlowDescriptor",
    "FlowStep",
    "StepAction",
    "StepContinuation",
    "TerminalAction",
    "build_due_diligence_intake",
    "build_flow",
    "build_flow_from_descriptor",
    "build_onboarding_wizard",
]
