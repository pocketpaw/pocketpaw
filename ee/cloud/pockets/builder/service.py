# Pockets builder — public service surface.
#
# Created 2026-05-01.  Three sync-style helpers (``detect_intent``,
# ``build_pocket_spec``, ``build_update_patch``) plus the async generator
# ``run_intent_from_message`` that the SSE handler consumes end-to-end.
#
# Touch-time consolidation: ``run_intent_from_message`` calls into
# ``ee.cloud.pockets.service.agent_create`` / ``agent_update`` and links
# sessions via ``ee.cloud.sessions.service``.  No Beanie writes here.

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from pocketpaw.config import Settings

from ee.cloud.pockets import service as pockets_service
from ee.cloud.pockets.builder.domain import (
    BuilderEvent,
    IntentKind,
    PocketSpec,
    PocketUpdatePatch,
)
from ee.cloud.pockets.builder.dto import BuildRequest, IntentDetectionResult
from ee.cloud.pockets.builder.prompts import (
    INTENT_CLASSIFIER_SYSTEM,
    SPEC_BUILDER_SYSTEM,
    UPDATE_BUILDER_SYSTEM,
)
from ee.cloud.pockets.builder.providers import ProviderError, structured_call

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step-level helpers
# ---------------------------------------------------------------------------


async def detect_intent(
    req: BuildRequest, *, settings: Settings | None = None
) -> IntentDetectionResult:
    """Classify whether ``req.user_message`` is a pocket create / update /
    unrelated request.  Single structured-output LLM call."""
    messages = [
        {"role": "system", "content": INTENT_CLASSIFIER_SYSTEM},
        {"role": "user", "content": req.user_message},
    ]
    result = await structured_call(
        req.provider,
        IntentDetectionResult,
        messages,
        model=req.model,
        settings=settings,
    )
    assert isinstance(result, IntentDetectionResult)
    return result


async def build_pocket_spec(
    req: BuildRequest, *, settings: Settings | None = None
) -> PocketSpec:
    """Turn the user's natural-language request into a validated
    ``PocketSpec``.  Single structured-output LLM call.  ``providers``
    handles the one-retry-on-parse-failure contract."""
    messages = [
        {"role": "system", "content": SPEC_BUILDER_SYSTEM},
        {"role": "user", "content": req.user_message},
    ]
    result = await structured_call(
        req.provider,
        PocketSpec,
        messages,
        model=req.model,
        settings=settings,
    )
    assert isinstance(result, PocketSpec)
    return result


async def build_update_patch(
    req: BuildRequest, *, settings: Settings | None = None
) -> PocketUpdatePatch:
    """Turn the user's natural-language edit request into a validated
    ``PocketUpdatePatch``.  Caller is responsible for fetching the current
    pocket if it wants to inject the spec into the prompt (Phase 1.5)."""
    if not req.pocket_id:
        raise ProviderError(
            "bad_request",
            "build_update_patch requires req.pocket_id",
        )
    messages = [
        {"role": "system", "content": UPDATE_BUILDER_SYSTEM},
        {"role": "user", "content": req.user_message},
    ]
    result = await structured_call(
        req.provider,
        PocketUpdatePatch,
        messages,
        model=req.model,
        settings=settings,
    )
    assert isinstance(result, PocketUpdatePatch)
    return result


# ---------------------------------------------------------------------------
# Top-level async generator the SSE handler iterates
# ---------------------------------------------------------------------------


def _confirmation_text(spec: PocketSpec) -> str:
    """Deterministic post-create confirmation chunk.  Zero LLM calls."""
    widget_count = len(spec.widgets) if spec.widgets else 0
    base = f"Built {spec.name} — a {spec.type} pocket with {widget_count} widgets."
    if spec.ripple_spec:
        return base + " The canvas is ready."
    return base


async def run_intent_from_message(
    req: BuildRequest,
    *,
    settings: Settings | None = None,
) -> AsyncGenerator[BuilderEvent, None]:
    """Top-level entry point.  Yields ``BuilderEvent`` objects the SSE
    router serialises into ``event:``/``data:`` SSE frames.

    Sequence:
      1. ``intent.detected``           (always)
      2. (if create) ``spec.building`` → ``pocket.created`` → ``chunk``
      3. (if update) ``spec.building`` → ``pocket.updated``
      4. ``error``                     (on any ``ProviderError`` / save failure)
      5. (if intent == none) yield exactly one ``intent.detected`` event
         — caller falls through to the normal agent run.
    """
    if not req.workspace_id or not req.user_id:
        yield BuilderEvent(
            "error",
            {
                "code": "builder.bad_request",
                "message": "Missing session context. Refresh and try again.",
            },
        )
        return

    # Step 1 — intent detection (skip when frontend pre-classified).
    if req.intent_hint in ("pocket_create", "pocket_update"):
        intent = req.intent_hint
        confidence = 1.0
    else:
        try:
            classify = await detect_intent(req, settings=settings)
        except ProviderError as exc:
            # Exception clause from §8: classifier failure with no hint
            # falls through silently rather than emitting an error.
            logger.info(
                "builder classifier failed; falling through (code=%s)", exc.code
            )
            yield BuilderEvent(
                "intent.detected", {"intent": "none", "confidence": 0.0}
            )
            return
        intent = classify.intent
        confidence = classify.confidence
        # Captain rule 1: log confidence; do NOT gate routing on it.
        logger.info(
            "builder intent=%s confidence=%.2f provider=%s",
            intent,
            confidence,
            req.provider,
        )

    yield BuilderEvent(
        "intent.detected", {"intent": intent, "confidence": confidence}
    )

    if intent == IntentKind.NONE.value:
        return

    if intent == IntentKind.CREATE.value:
        async for event in _run_create(req, settings=settings):
            yield event
        return

    if intent == IntentKind.UPDATE.value:
        async for event in _run_update(req, settings=settings):
            yield event
        return

    # Unknown intent string — treat as ``none`` and fall through.
    return


async def _run_create(
    req: BuildRequest, *, settings: Settings | None
) -> AsyncGenerator[BuilderEvent, None]:
    yield BuilderEvent("spec.building", {})

    try:
        spec = await build_pocket_spec(req, settings=settings)
    except ProviderError as exc:
        yield BuilderEvent(
            "error",
            {"code": f"builder.{exc.code}", "message": exc.message},
        )
        return

    # Hand off to the existing pocket service.  Note: ``agent_create``
    # currently accepts ``ripple_spec`` only — flat ``widgets`` arrays are
    # converted via the ripple normalizer downstream when integrated.  For
    # Phase 1 we hand ``ripple_spec`` through and let the service do its
    # normalisation.  Flat widgets are persisted by skipping ripple_spec
    # and adding widgets via ``agent_add_widget``.
    view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=req.workspace_id,
        owner_id=req.user_id,
        name=spec.name,
        description=spec.description,
        type_=spec.type,
        icon=spec.icon,
        color=spec.color,
        ripple_spec=spec.ripple_spec,
    )
    if err is not None or view is None or pocket_id is None:
        yield BuilderEvent(
            "error",
            {
                "code": "builder.mongo_error",
                "message": err or "Pocket was designed but could not be saved.",
            },
        )
        return

    # If the spec used flat widgets, add them now.
    if spec.widgets:
        for widget in spec.widgets:
            try:
                await pockets_service.agent_add_widget(
                    pocket_id, widget.model_dump(exclude_none=True)
                )
            except Exception:
                logger.warning("agent_add_widget failed (non-fatal)", exc_info=True)

    # Best-effort link to active session + SessionUpdated emit.
    if req.session_mongo_id:
        try:
            from ee.cloud.sessions import service as sessions_service

            linked_session_oid = await sessions_service.attach_pocket_to_session_doc(
                req.session_mongo_id, req.user_id, pocket_id
            )
            if linked_session_oid:
                try:
                    from ee.cloud.realtime.emit import emit
                    from ee.cloud.realtime.events import SessionUpdated

                    await emit(
                        SessionUpdated(
                            data={
                                "session_id": linked_session_oid,
                                "user_id": req.user_id,
                                "pocket_id": pocket_id,
                            }
                        )
                    )
                except Exception:
                    logger.debug(
                        "SessionUpdated emit after pocket-link failed",
                        exc_info=True,
                    )
        except Exception:
            logger.warning(
                "session attach for newly-created pocket failed", exc_info=True
            )

    # Push ``pocket_created`` SSE event onto the active stream so the
    # frontend mounts the new pocket without waiting for sidebar refresh.
    # Lazy import is the one permitted reach into ``ee.cloud.chat`` per the
    # importlinter contract — module-level imports are forbidden.
    try:
        from ee.cloud.chat.agent_service import push_sse_event

        push_sse_event(
            "pocket_created",
            {
                "pocket_id": pocket_id,
                "pocket": view,
                "session_id": req.session_mongo_id,
            },
        )
    except Exception:
        logger.debug("push_sse_event(pocket_created) failed", exc_info=True)

    yield BuilderEvent(
        "pocket.created", {"pocket_id": pocket_id, "pocket": view}
    )

    # Conversational reply policy (§9): emit a deterministic confirmation
    # chunk so the chat thread isn't silent after creation.
    yield BuilderEvent("chunk", {"content": _confirmation_text(spec), "type": "text"})


async def _run_update(
    req: BuildRequest, *, settings: Settings | None
) -> AsyncGenerator[BuilderEvent, None]:
    if not req.pocket_id:
        yield BuilderEvent(
            "error",
            {
                "code": "builder.bad_request",
                "message": "Update intent requires an active pocket.",
            },
        )
        return

    yield BuilderEvent("spec.building", {})

    try:
        patch = await build_update_patch(req, settings=settings)
    except ProviderError as exc:
        yield BuilderEvent(
            "error",
            {"code": f"builder.{exc.code}", "message": exc.message},
        )
        return

    # Apply the patch via the existing agent_update helper.  Only fields
    # the patch explicitly set are forwarded.
    patch_kwargs: dict[str, Any] = {}
    for field_name in ("name", "description", "icon", "color"):
        value = getattr(patch, field_name)
        if value is not None:
            patch_kwargs[field_name] = value

    view, err = await pockets_service.agent_update(req.pocket_id, **patch_kwargs)
    if err is not None or view is None:
        yield BuilderEvent(
            "error",
            {
                "code": "builder.mongo_error",
                "message": err or "Pocket update could not be saved.",
            },
        )
        return

    # Mirror the ``_push_replace`` pattern from agent_context so the canvas
    # refreshes.  Lazy import keeps the ``ee.cloud.chat`` cycle out.
    try:
        from ee.cloud.chat.agent_service import push_pocket_mutation

        push_pocket_mutation(
            {
                "action": "replace",
                "pocket_id": req.pocket_id,
                "pocket": view,
            }
        )
    except Exception:
        logger.debug("push_pocket_mutation failed (non-fatal)", exc_info=True)

    yield BuilderEvent("pocket.updated", {"pocket_id": req.pocket_id})


__all__ = [
    "build_pocket_spec",
    "build_update_patch",
    "detect_intent",
    "run_intent_from_message",
]
