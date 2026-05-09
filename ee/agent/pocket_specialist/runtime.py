"""Pocket-specialist runtime - the only public entry point for the tool surfaces.

Orchestrates backend selection, tool wiring, event emission, and result
assembly. Always persists a pocket - see feedback_pocket_always_ships.md.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ee.agent.pocket_specialist.events import (
    SpecialistEvent,
    emit_specialist_event,
)
from ee.agent.pocket_specialist.settings import (
    _BACKEND_MODEL_FIELD,
    resolve_specialist_model,
)
from ee.agent.pocket_specialist.tools import (
    make_persist_pocket_tool,
)
from ee.cloud.pockets.service import agent_create as _agent_create_for_fallback
from ee.ripple._pockets import POCKET_ID_TOKEN, POCKET_SPECIALIST_PROMPT
from pocketpaw.agents.router import AgentRouter
from pocketpaw.config import Settings

# _agent_create_for_fallback is imported (rather than referenced via the
# ee.cloud.pockets.service path at call time) so tests can patch it on this
# module. Mirrors the pattern in tools.py.

log = logging.getLogger(__name__)


class PocketSpecialistHints(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    icon: str | None = None
    target_pocket_id: str | None = None


class PocketSpecialistCreateInput(BaseModel):
    brief: str = Field(..., min_length=10, max_length=4000)
    hints: PocketSpecialistHints | None = None


class PocketSpecialistCreateOutput(BaseModel):
    ok: bool
    action: Literal["created", "extended"]
    pocket: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int
    backend_used: str


async def run_specialist(
    input: PocketSpecialistCreateInput,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
) -> PocketSpecialistCreateOutput:
    """Run the pocket specialist end-to-end.

    Builds an isolated backend, attaches the three internal tools, runs the
    agent loop, captures the persist_pocket result, and emits status events
    along the way. Always returns a persisted pocket - the safety-net
    fallback (Task 8) covers the rare case where the LLM finishes without
    calling persist_pocket.
    """
    started = time.monotonic()
    backend_name = settings.pocket_specialist_backend
    model_id = resolve_specialist_model(settings)

    await emit_specialist_event(
        SpecialistEvent.START,
        {
            "brief": input.brief[:200],
            "hints": input.hints.model_dump() if input.hints else None,
            "backend": backend_name,
        },
    )

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
            ),
        ]
    )

    system_prompt = _build_system_prompt(input.hints)
    user_message = _build_user_message(input)

    persist_called = False

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
                    await emit_specialist_event(SpecialistEvent.PERSISTING, {})
            elif event.type == "tool_result":
                meta = event.metadata or {}
                if meta.get("name") == "persist_pocket":
                    persist_called = True
    finally:
        await backend.stop()

    captured_pocket: dict[str, Any] | None = persist_capture.get("pocket")
    captured_warnings: list[str] = list(persist_capture.get("warnings", []))

    if not persist_called or captured_pocket is None:
        log.warning("specialist run finished without persist_pocket; using fallback")
        captured_pocket = await _force_persist_fallback(
            workspace_id=workspace_id,
            user_id=user_id,
            input=input,
        )
        captured_warnings.append(
            "Specialist did not call persist_pocket; force-persisted a "
            "minimal pocket. Ask the user to refine."
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    action: Literal["created", "extended"] = (
        "extended" if (input.hints and input.hints.target_pocket_id) else "created"
    )

    await emit_specialist_event(
        SpecialistEvent.DONE,
        {
            "pocket_id": captured_pocket.get("id", ""),
            "action": action,
            "duration_ms": duration_ms,
            "warning_count": len(captured_warnings),
        },
    )

    # Single-line operator-grep summary: emit OUTSIDE the per-event helper
    # so it shows up once per run regardless of bus state.
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
    prompt + any hints from the caller."""
    base = POCKET_SPECIALIST_PROMPT.replace(POCKET_ID_TOKEN, "")
    if not hints:
        return base
    hint_block = ["", "CALLER HINTS (respect when set, otherwise decide yourself):"]
    for field in ("name", "description", "color", "icon", "target_pocket_id"):
        v = getattr(hints, field)
        if v:
            hint_block.append(f"  {field}: {v}")
    if len(hint_block) == 2:
        # No hints set on the model - skip the block entirely.
        return base
    return base + "\n".join(hint_block)


def _build_user_message(input: PocketSpecialistCreateInput) -> str:
    return (
        "Create a pocket per the brief below. Draft the rippleSpec in one "
        "pass and call persist_pocket exactly once. Do NOT call any other "
        "tools.\n\nBRIEF:\n" + input.brief
    )


# Module-level - exposed for tests to validate against the live manifest.
# If the renderer's manifest changes prop names, the regression test in
# tests/ee/agent/test_pocket_specialist/test_runtime.py fails before we
# ship a blank pocket to a real user.
_MINIMAL_SPEC_FOR_FALLBACK: dict[str, Any] = {
    "version": "1.0",
    "state": {},
    "ui": {
        "type": "text",
        "props": {
            "text": (
                "This pocket was auto-created from a brief. "
                "Ask me to refine it and I'll fill it out."
            )
        },
    },
}


async def _force_persist_fallback(
    *,
    workspace_id: str,
    user_id: str,
    input: PocketSpecialistCreateInput,
) -> dict[str, Any]:
    """Persist a minimal pocket when the LLM finished without calling
    persist_pocket. Always ships output - never raises on LLM/spec content.
    """
    name = (input.hints and input.hints.name) or _derive_name_from_brief(input.brief)
    description = (input.hints and input.hints.description) or input.brief[:200]
    pocket, _id, err = await _agent_create_for_fallback(
        workspace_id=workspace_id,
        owner_id=user_id,
        name=name,
        description=description,
        icon=(input.hints and input.hints.icon) or "Sparkles",
        color=(input.hints and input.hints.color) or "#a78bfa",
        ripple_spec=_MINIMAL_SPEC_FOR_FALLBACK,
    )
    if err or pocket is None:
        raise RuntimeError(f"force-persist fallback failed: {err or 'no pocket returned'}")
    return pocket


def _derive_name_from_brief(brief: str) -> str:
    """Best-effort short title from the brief - first 6 words, capped at 40 chars."""
    words = brief.strip().split()[:6]
    name = " ".join(words).rstrip(".,!?:;")[:40]
    return name or "Untitled pocket"
