"""Pocket-specialist runtime - the only public entry point for the tool surfaces.

Orchestrates backend selection, tool wiring, event emission, and result
assembly. Always persists a pocket - see feedback_pocket_always_ships.md.

Changes: 2026-05-21 (#1163) — the edit-specialist stream loop now inspects
``event.type == "error"`` (the deep_agents backend yields error events
instead of raising), so a backend failure surfaces as ``ok=False`` with a
populated ``error`` field instead of a silent ``ok=True, ops=[]``. A
genuine 0-ops outcome with no error now surfaces the planner's final text
reply via the new ``PocketSpecialistEditOutput.warnings`` field so the
caller learns WHY the specialist declined. Service-rejected granular ops
are no longer counted as applied — their rejection reasons are folded
into ``warnings`` whether or not other ops landed. The 0-ops reason is
joined with "" because deep_agents emits message events as token-level
chunks. Added targeted observability logging for error events and
tool_use / applied / rejected counts.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from pocketpaw.agents.router import AgentRouter
from pocketpaw.config import Settings
from pocketpaw.ripple._pockets import (
    POCKET_EDIT_SPECIALIST_PROMPT_MCP,
    POCKET_ID_TOKEN,
    POCKET_SPECIALIST_PROMPT,
)
from pocketpaw_ee.agent.pocket_specialist.settings import (
    _BACKEND_MODEL_FIELD,
    resolve_specialist_model,
)
from pocketpaw_ee.agent.pocket_specialist.tools import (
    make_edit_pocket_tools,
    make_persist_pocket_tool,
)

log = logging.getLogger(__name__)


class PocketSpecialistHints(BaseModel):
    """Caller-supplied guidance for a specialist run.

    The first five fields are surface metadata. The remaining fields
    are the STRUCTURAL PLAN — when set, the specialist follows them
    rather than re-deciding. This shifts the open-ended design work
    onto the parent agent (Claude — better at dialogue + layout
    reasoning) and leaves the specialist (often a cheaper / faster
    model) to do faithful translation into rippleSpec.

    All fields are optional; bare-brief calls still work.
    """

    name: str | None = None
    description: str | None = None
    color: str | None = None
    icon: str | None = None
    target_pocket_id: str | None = None

    # ---- structural plan (parent agent decides these before delegating) ----
    purpose: str | None = Field(
        default=None,
        description=(
            "One-sentence statement of what this pocket should ACCOMPLISH "
            "for the user (not what it contains). Drives focal-widget "
            "selection and layout."
        ),
    )
    layout: str | None = Field(
        default=None,
        description=(
            "High-level layout shape. One of: 'hero+grid', 'single-pane', "
            "'sidebar+main', 'tabs', 'master-detail', 'stacked', 'wizard'. "
            "If unset, the specialist picks."
        ),
    )
    focal_widget: str | None = Field(
        default=None,
        description=(
            "The ONE widget that IS this pocket. e.g. 'calendar', "
            "'kanban', 'data-grid', 'tree-table', 'funnel', 'heatmap', "
            "'treemap', 'timeline', 'pricing-table', 'comparison-layout', "
            "'entity-detail', 'form-layout', 'report-layout'. Most "
            "pockets are dominated by ONE widget; this names it."
        ),
    )
    data_shape: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Sketch of the state schema the specialist should seed. "
            "Keys are state field names; values describe the shape. "
            "Example: "
            "{'tasks': '[{id, label, status, due}]', 'filter': 'string'}"
        ),
    )
    key_interactions: list[str] | None = Field(
        default=None,
        description=(
            "What the user should be able to DO with this pocket. "
            "Drives controls + action chains. e.g. "
            "['add task', 'mark done', 'filter by status']."
        ),
    )


class PocketSpecialistCreateInput(BaseModel):
    brief: str = Field(..., min_length=10, max_length=4000)
    hints: PocketSpecialistHints | None = None
    spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Pre-drafted rippleSpec for agent-mode's second call. When set, "
            "the specialist skips its own LLM draft phase and goes straight "
            "to validate-and-persist. Ignored in subagent mode."
        ),
    )


class PocketSpecialistCreateOutput(BaseModel):
    ok: bool
    action: Literal["created", "extended", "failed", "draft_kit", "redraft"]
    pocket: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    duration_ms: int
    backend_used: str
    draft_kit: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Agent-mode first-call payload: design rules digest, structural "
            "plan echo, available widget list, and instructions for the "
            "calling chat agent to draft a rippleSpec and call back with "
            "``spec=<draft>``. None in subagent mode."
        ),
    )


async def run_specialist(
    input: PocketSpecialistCreateInput,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
) -> PocketSpecialistCreateOutput:
    """Entry point — pick the adapter for ``settings.pocket_specialist_mode``
    and delegate.

    Two adapters live in ``adapters.py``. The default ``subagent`` mode
    runs the historical pipeline below (an isolated backend with the
    specialist's own model). The ``agent`` mode short-circuits the
    backend spawn and hands a draft kit back to the calling chat agent
    so it can draft the rippleSpec inline using its own LLM.

    Signature is the public contract — call sites in ``mcp_tool``,
    ``cli_tool``, and ``tool`` rely on it being adapter-agnostic.
    """
    from pocketpaw_ee.agent.pocket_specialist.adapters import pick_adapter

    adapter = pick_adapter(settings.pocket_specialist_mode)
    return await adapter.create(
        input, workspace_id=workspace_id, user_id=user_id, settings=settings
    )


async def _run_subagent_pipeline(
    input: PocketSpecialistCreateInput,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
) -> PocketSpecialistCreateOutput:
    """Subagent-mode pipeline (historical flow).

    Builds an isolated backend, attaches the three internal tools, runs the
    agent loop, captures the persist_pocket result, and emits status events
    along the way. Always returns a persisted pocket - the safety-net
    fallback (Task 8) covers the rare case where the LLM finishes without
    calling persist_pocket.

    Invoked by ``SubagentAdapter.create`` — kept private so the only
    entry point remains ``run_specialist`` (which dispatches via
    ``pick_adapter``).
    """
    started = time.monotonic()
    backend_name = settings.pocket_specialist_backend
    model_id = resolve_specialist_model(settings)

    # Push intermediate sub-stage status to the parent chat stream so
    # the loader updates from "Designing pocket..." (set by the outer
    # tool_start) to a more specific label as work progresses. We use
    # synthetic ``tool_start`` events (not ``thinking``) so they flow
    # through the established TOOL_LABELS lookup in the desktop client
    # — same path as every other tool, no special-casing in the UI.
    # The tool names here MUST match the entries in
    # paw-enterprise/src/lib/core/chat/service.ts:TOOL_LABELS.
    # ContextVars from the parent agent_router stream are inherited
    # here (in-process MCP tool call shares the task context), so the
    # SSE sink is in scope. Best-effort — when there's no active sink
    # (CLI/test runs) the call is a no-op.
    def _push_chat_status(stage: str) -> None:
        try:
            from pocketpaw_ee.cloud.chat.agent_service import push_sse_event

            push_sse_event("tool_start", {"tool": stage, "input": {}})
        except Exception:
            log.debug("push_chat_status failed (non-fatal)", exc_info=True)

    log.info(
        "[pocket-specialist] start brief=%r hints=%s backend=%s",
        input.brief[:80],
        input.hints.model_dump() if input.hints else None,
        backend_name,
    )
    _push_chat_status("pocket_specialist:build")

    override: dict[str, Any] = {}
    if model_id:
        field_name = _BACKEND_MODEL_FIELD.get(backend_name, f"{backend_name}_model")
        override[field_name] = model_id

    backend = AgentRouter.create_isolated_backend(
        backend_name,
        settings,
        settings_override=override or None,
    )
    # Side-channel capture dicts: real agent backends only surface
    # {"name": tool_name} in tool_result metadata - they never put the
    # tool's return dict in metadata["result"]. The factories mutate these
    # dicts when their tools run, giving the runtime access to the actual
    # return values without parsing truncated stringified content.
    persist_capture: dict[str, Any] = {}
    backend.attach_specialist_tools(
        [
            make_persist_pocket_tool(
                workspace_id=workspace_id,
                user_id=user_id,
                capture=persist_capture,
                max_validation_retries=settings.pocket_specialist_max_validation_retries,
            ),
        ]
    )

    system_prompt = _build_system_prompt(input.hints)
    user_message = _build_user_message(input)

    log.info(
        "[pocket-specialist] dispatching to backend.run (model=%s, system_prompt_len=%d)",
        model_id or "<inherited>",
        len(system_prompt),
    )

    first_event_seen = False
    try:
        async for event in backend.run(user_message, system_prompt=system_prompt):
            if not first_event_seen:
                log.info(
                    "[pocket-specialist] backend stream started (first event: %s)",
                    event.type,
                )
                first_event_seen = True
            if event.type == "tool_use":
                tool_name = (event.metadata or {}).get("name", "")
                if tool_name == "persist_pocket":
                    _push_chat_status("pocket_specialist:save")
    finally:
        await backend.stop()

    # Source of truth for "did we actually persist" is the capture dict —
    # not whether the tool was invoked. With the validation-retry gate,
    # the model may call persist_pocket several times before the spec is
    # clean enough to actually save; only the successful call sets
    # capture["pocket"].
    captured_pocket: dict[str, Any] | None = persist_capture.get("pocket")
    captured_warnings: list[str] = list(persist_capture.get("warnings", []))
    duration_ms = int((time.monotonic() - started) * 1000)

    if captured_pocket is None:
        # No placeholder. If the model errored mid-run, ran out of
        # retries on invalid props, or hit a transport error
        # (DeepSeek 400, etc.), surface the failure cleanly so the
        # parent agent can ask the user to retry. The previous
        # auto-shipped-shell behavior left users staring at empty
        # canvases captioned "auto-created from a brief".
        log.warning(
            "[pocket-specialist] no pocket persisted — returning failure "
            "(backend=%s duration=%dms warnings=%d)",
            backend_name,
            duration_ms,
            len(captured_warnings),
        )
        return PocketSpecialistCreateOutput(
            ok=False,
            action="failed",
            pocket=None,
            warnings=captured_warnings,
            error=(
                "Specialist did not produce a valid pocket — either the "
                "model exhausted validation retries or the run errored "
                "before persist_pocket succeeded. No placeholder was "
                "created. Ask the user to clarify the brief or try again."
            ),
            duration_ms=duration_ms,
            backend_used=backend_name,
        )

    action: Literal["created", "extended", "failed"] = (
        "extended" if (input.hints and input.hints.target_pocket_id) else "created"
    )

    # Single-line operator-grep summary.
    log.info(
        "[pocket-specialist] complete: pocket_id=%s action=%s backend=%s duration=%dms warnings=%d",
        captured_pocket.get("id", ""),
        action,
        backend_name,
        duration_ms,
        len(captured_warnings),
    )

    return PocketSpecialistCreateOutput(
        ok=True,
        action=action,
        pocket=captured_pocket,
        warnings=captured_warnings,
        duration_ms=duration_ms,
        backend_used=backend_name,
    )


def _build_system_prompt(hints: PocketSpecialistHints | None) -> str:
    """Compose the specialist system prompt from the canonical creation
    prompt + any hints from the caller.

    Surface-metadata hints (name, color, icon, target_pocket_id) and
    structural-plan hints (purpose, layout, focal_widget, data_shape,
    key_interactions) land in the same block. The plan fields, when
    set, are AUTHORITATIVE — the specialist follows them rather than
    re-deciding. See the FOLLOW-THE-PLAN rule in the specialist
    workflow block.
    """
    base = POCKET_SPECIALIST_PROMPT.replace(POCKET_ID_TOKEN, "")
    if not hints:
        return base

    surface = ("name", "description", "color", "icon", "target_pocket_id")
    plan = ("purpose", "layout", "focal_widget", "data_shape", "key_interactions")

    lines: list[str] = []
    surface_values: list[tuple[str, Any]] = [
        (f, getattr(hints, f)) for f in surface if getattr(hints, f)
    ]
    plan_values: list[tuple[str, Any]] = [(f, getattr(hints, f)) for f in plan if getattr(hints, f)]

    if surface_values:
        lines.append("")
        lines.append("CALLER METADATA (respect when set):")
        for k, v in surface_values:
            lines.append(f"  {k}: {v}")

    if plan_values:
        lines.append("")
        lines.append("STRUCTURAL PLAN FROM PARENT AGENT — FOLLOW THESE, DO NOT REDESIGN:")
        for k, v in plan_values:
            lines.append(f"  {k}: {v}")
        lines.append(
            "The parent already collected the user's intent and picked "
            "the shape. Your job is faithful translation to rippleSpec, "
            "not creative reimagining."
        )

    if not lines:
        return base
    return base + "\n".join(lines)


def _build_user_message(input: PocketSpecialistCreateInput) -> str:
    """Build the agent's first user message.

    When the parent passed a structural plan in hints, surface it in
    the message body too — system-prompt blocks can get truncated /
    re-ordered by some backends, but the user message is always the
    last thing the model sees before responding.
    """
    plan_lines: list[str] = []
    if input.hints:
        for field in ("purpose", "layout", "focal_widget", "data_shape", "key_interactions"):
            v = getattr(input.hints, field)
            if v:
                plan_lines.append(f"  {field}: {v}")

    msg = (
        "Create a pocket per the brief below. Draft the rippleSpec in one "
        "pass and call persist_pocket exactly once. Do NOT call any other "
        "tools.\n\nBRIEF:\n" + input.brief
    )
    if plan_lines:
        msg += "\n\nPLAN (from parent agent — follow these):\n" + "\n".join(plan_lines)
    return msg


# Placeholder-pocket fallback intentionally removed. When the specialist
# fails to ship a real pocket (model errored, exhausted validation
# retries, transport error), we now return ok=false with an error message
# rather than persisting a blank shell. Users would rather see "I
# couldn't build that, can you clarify?" than open an empty canvas
# captioned "auto-created from a brief".


def _build_edit_user_message(input: PocketSpecialistEditInput) -> str:
    """Build the edit specialist's first user message.

    When the parent already read the pocket and/or identified the
    target nodes, surface that in the body of the message. The
    specialist's system prompt has matching rules for skipping its
    own ``get_pocket`` call and working only on the targeted nodes.
    """
    import json as _json

    lines: list[str] = [
        "Edit the pocket per the intent below. Apply the smallest set "
        "of granular ops that satisfies the intent.",
        "",
        f"INTENT:\n{input.intent}",
    ]
    if input.target_node_ids:
        lines.append("")
        lines.append(
            "TARGET NODE IDS (from parent agent — these are already "
            "the right nodes; do not search for others):"
        )
        for nid in input.target_node_ids:
            lines.append(f"  - {nid}")
    if input.pocket is not None:
        # Compact JSON to keep token count tight. The specialist prompt
        # tells it where to look.
        try:
            payload = _json.dumps(input.pocket, separators=(",", ":"))
        except Exception:
            payload = str(input.pocket)
        lines.append("")
        lines.append(
            "CURRENT POCKET (parent agent already read it — skip get_pocket, use this directly):"
        )
        lines.append(payload)
    elif not input.target_node_ids:
        # No payload, no targets — tell the specialist to read first.
        lines.append("")
        lines.append("Read the pocket first with get_pocket, then apply ops.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Edit specialist — pairs with the creation specialist above. The main
# chat agent delegates ALL pocket edits here. The specialist receives
# the heavy interaction prompt + granular mutation tools and runs an
# isolated agent loop just like the creation flow.
# ---------------------------------------------------------------------------


class PocketSpecialistEditInput(BaseModel):
    """Input for an edit specialist run.

    ``pocket_id`` + ``intent`` are required. The remaining fields are
    **shift-thinking-upstream** handoffs: the parent agent does the
    work of reading + disambiguating + targeting, and the specialist
    runs more deterministically as a result.

    All optional fields are backwards-compatible — a bare
    ``{pocket_id, intent}`` call still works.
    """

    pocket_id: str = Field(..., min_length=1, description="Pocket to edit.")
    intent: str = Field(
        ...,
        min_length=3,
        max_length=4000,
        description="Natural-language description of the change the user wants.",
    )

    # ---- handoff fields from parent agent ----
    pocket: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Current pocket view (rippleSpec + metadata) the parent "
            "already fetched. When set, the specialist skips its own "
            "get_pocket call and works directly on this data. Useful "
            "when the parent had to read the pocket anyway (to "
            "disambiguate targets or to confirm the edit makes sense)."
        ),
    )
    target_node_ids: list[str] | None = Field(
        default=None,
        description=(
            "Node ids the parent agent identified as edit targets. When "
            "set, the specialist works ONLY on these nodes and does not "
            "search for others. Eliminates disambiguation in the "
            "specialist — the parent did the lookup."
        ),
    )


class PocketSpecialistEditOutput(BaseModel):
    ok: bool
    pocket_id: str
    ops: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int
    backend_used: str
    error: str | None = Field(
        default=None,
        description=(
            "Set when the specialist run FAILED — backend raised, or the "
            "deep_agents backend yielded an error event. ``ok`` is False "
            "whenever this is populated. Distinct from ``warnings``."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Set when the run SUCCEEDED but applied zero ops — the planner "
            "declined to act (target not found, intent ambiguous, no change "
            "needed). Carries the planner's final text reply so the caller "
            "knows WHY. ``ok`` stays True; this is not a failure. A silent "
            "``ok=True, ops=[]`` with no explanation is the #1163 bug."
        ),
    )


async def run_edit_specialist(
    input: PocketSpecialistEditInput,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
) -> PocketSpecialistEditOutput:
    """Run the pocket edit specialist end-to-end.

    Spawns an isolated backend with the interaction prompt + granular
    mutation tools. The granular ops persist as they go (no
    persist_pocket needed); each op also emits its own SSE event so
    the canvas updates in place.
    """
    started = time.monotonic()
    backend_name = settings.pocket_specialist_backend
    model_id = resolve_specialist_model(settings)

    log.info(
        "[pocket-specialist:edit] start pocket_id=%s intent=%r backend=%s",
        input.pocket_id,
        input.intent[:80],
        backend_name,
    )

    # Push a chat-stream tool_start so the desktop client shows
    # "Editing pocket..." while the inner specialist works. Each granular
    # op the specialist's LLM emits is forwarded below as its own
    # tool_start, so the user sees per-op progress too.
    def _push_chat_status(stage: str, payload: dict[str, Any] | None = None) -> None:
        try:
            from pocketpaw_ee.cloud.chat.agent_service import push_sse_event

            push_sse_event("tool_start", {"tool": stage, "input": payload or {}})
        except Exception:
            log.debug("push_chat_status failed (non-fatal)", exc_info=True)

    _push_chat_status("pocket_specialist:edit")

    override: dict[str, Any] = {}
    if model_id:
        field_name = _BACKEND_MODEL_FIELD.get(backend_name, f"{backend_name}_model")
        override[field_name] = model_id

    backend = AgentRouter.create_isolated_backend(
        backend_name,
        settings,
        settings_override=override or None,
    )

    ops_capture: dict[str, Any] = {"ops": []}
    backend.attach_specialist_tools(
        make_edit_pocket_tools(pocket_id=input.pocket_id, capture=ops_capture)
    )

    system_prompt = POCKET_EDIT_SPECIALIST_PROMPT_MCP.replace(POCKET_ID_TOKEN, input.pocket_id)
    user_message = _build_edit_user_message(input)

    log.info(
        "[pocket-specialist:edit] dispatching (pocket_id=%s model=%s)",
        input.pocket_id,
        model_id or "<inherited>",
    )

    _GRANULAR_OP_PREFIXES = (
        "set_",
        "add_",
        "remove_",
        "move_",
        "replace_",
        "append_",
        "patch_",
    )
    # success starts False and flips True only after the backend.run
    # loop completes without exception AND without an error event.
    # Catches two silent-failure modes the caller previously saw as
    # ok=True with ops=[]:
    #   1. the inner backend raises mid-stream (transport drop, 400)
    #   2. the deep_agents backend yields AgentEvent(type="error")
    #      instead of raising — see deep_agents.py:974-977.
    success = False
    error_msg: str | None = None
    tool_use_count = 0
    # The planner's running text — used to explain a genuine 0-ops run.
    final_text_parts: list[str] = []
    try:
        async for event in backend.run(user_message, system_prompt=system_prompt):
            if event.type == "error":
                # #1163 root cause A — deep_agents converts internal
                # failures into error events rather than raising. Capture
                # the message, mark the run failed, and stop trusting the
                # clean loop exit.
                error_msg = str(event.content or "backend emitted an error event")
                log.warning(
                    "[pocket-specialist:edit] backend emitted error event: %s",
                    error_msg,
                )
                continue
            if event.type == "tool_use":
                tool_use_count += 1
                tool_name = (event.metadata or {}).get("name", "")
                if tool_name.startswith(_GRANULAR_OP_PREFIXES):
                    # Forward each inner op to the outer chat SSE stream so
                    # the desktop client renders per-op progress (matches
                    # TOOL_LABELS entries in paw-enterprise chat/service.ts).
                    _push_chat_status(tool_name, event.metadata.get("input") or {})
            elif event.type == "message" and event.content:
                # Keep the planner's text — it explains a no-op decline.
                # The deep_agents backend yields message events as
                # TOKEN-LEVEL chunks (deep_agents.py:897, inside the v2
                # "messages" stream path), so these parts are fragments of
                # one reply, not whole messages. They are joined with ""
                # below — joining with "\n" would chop the prose.
                final_text_parts.append(str(event.content))
        # A clean loop exit only counts as success when no error event
        # passed through. error_msg is set above for the error-event path.
        success = error_msg is None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "[pocket-specialist:edit] backend stream errored: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        error_msg = f"{type(exc).__name__}: {exc}"
        success = False
    finally:
        await backend.stop()

    duration_ms = int((time.monotonic() - started) * 1000)
    ops = list(ops_capture.get("ops", []))
    rejected = list(ops_capture.get("rejected", []))

    # Observability — distinguish "planner declined to act" (tool_use=0)
    # from "tool called but the service rejected it" (tool_use>0, ops=0).
    log.info(
        "[pocket-specialist:edit] planner emitted %d tool_use events, "
        "%d granular ops applied, %d rejected by the service",
        tool_use_count,
        len(ops),
        len(rejected),
    )

    # Build the warnings list. ``warnings`` is the channel for "the run
    # succeeded but the caller should know something" — never a failure
    # (that is ``error``). Two sources feed it:
    #
    #   1. Service-rejected ops. A granular op the planner attempted but
    #      the service refused. Surfaced WHETHER OR NOT other ops applied
    #      — a partial-apply still owes the caller the rejection reason.
    #      A run where every op was rejected ends up ok=true, ops=[], with
    #      the reasons in warnings — not a silent success (#1163 class).
    #
    #   2. A genuine 0-ops decline. The planner emitted no granular tool
    #      call at all and ops/rejected are both empty — surface its
    #      final text reply so the caller can tell the user WHY.
    warnings: list[str] = []
    for rej in rejected:
        op_name = rej.get("op", "edit op")
        reason = rej.get("error", "rejected by the service")
        warnings.append(f"Edit op '{op_name}' could not be applied: {reason}")

    if success and not ops and not rejected:
        # deep_agents yields message events as token-level chunks — join
        # with "" so the surfaced reason reads as clean prose, not a
        # newline-chopped fragment soup.
        reason = "".join(final_text_parts).strip()
        warnings.append(
            reason
            or (
                "The edit specialist applied no changes and gave no "
                "reason. The target may not have been found, or the "
                "intent may not have mapped to a supported edit."
            )
        )
        log.warning(
            "[pocket-specialist:edit] 0-ops run — surfacing planner reply "
            "as a warning (pocket_id=%s tool_use=%d)",
            input.pocket_id,
            tool_use_count,
        )

    log.info(
        "[pocket-specialist:edit] complete: pocket_id=%s ops=%d success=%s "
        "backend=%s duration=%dms",
        input.pocket_id,
        len(ops),
        success,
        backend_name,
        duration_ms,
    )

    return PocketSpecialistEditOutput(
        ok=success,
        pocket_id=input.pocket_id,
        ops=ops,
        duration_ms=duration_ms,
        backend_used=backend_name,
        error=error_msg,
        warnings=warnings,
    )
