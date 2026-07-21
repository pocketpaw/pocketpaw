# service.py — The Code Mode agent turn (CA-1, Ask mode).
#
# Created 2026-07-21 (feat/codeagent-turn). One stateless turn: take the
# conversation and the caller-supplied context, call the model, return the
# answer. Supersedes ``websandbox/edit.py``, which will be deleted in CA-4.
#
# The one design decision worth restating here, because it is what makes the
# module short: THIS SERVICE NEVER TOUCHES A SANDBOX. ``edit.py`` reached into
# Daytona with ``download_file`` and ran an in-VM ``rg`` to gather context,
# which is exactly why the WebContainer runtime shipped ``features.cmdK: false``
# — a runtime with no server-side row has nothing for that code to call. Here the
# CLIENT reads its own files through ``CodeFileSession`` (7 verbs, implemented on
# both runtimes) and sends what it read. So there is no DaytonaClient import, no
# ``row_id``, no ``authorize_sandbox``, and no path jail: the server is not
# opening files, so there is no path to escape.
#
# Tenancy still matters even though there is no sandbox to own. The turn spends
# money, so it is workspace-scoped and license-gated at the router; the ids are
# carried here for metering and logging, not for authorization of a resource.
#
# CA-2 will add tool_calls to the return shape (the read-only subset of
# CodeFileSession for Ask, plus the mutating verbs for Edit). The DI seam below
# is deliberately the whole model call, so that stage swaps one function.
from __future__ import annotations

import logging
import os

from pocketpaw_ee.cloud._core.errors import CloudError, with_cause
from pocketpaw_ee.cloud.codeagent.domain import (
    ASK_SYSTEM_PROMPT,
    MAX_OUTPUT_TOKENS,
    MODEL_TIMEOUT_SECONDS,
    build_user_content,
    pack_context,
)
from pocketpaw_ee.cloud.codeagent.dto import AgentTurnRequest, AgentTurnResponse

logger = logging.getLogger(__name__)

# Opus 4.8 — the current most-capable model, and the right default for a
# coding assistant that has to reason about unfamiliar code.
#
# NOTE for CA-0: the model seam this replaces defaulted to "claude-sonnet-4-7",
# which is not a real model id (the Sonnet line goes 4-5, 4-6, 5). A request for
# it cannot succeed, so the deployment's reported failure may be a bad default
# rather than a broken proxy route. Worth checking before anything else.
_DEFAULT_MODEL = "claude-opus-4-8"


def _model() -> str:
    """Resolve the model id.

    ``POCKETPAW_CODEAGENT_MODEL`` wins, then the shared
    ``POCKETPAW_WEBSANDBOX_EDIT_MODEL`` so the operator can point BOTH the edit
    and the ask paths at one working route with a single variable (CA-0), then
    the default.
    """
    for var in ("POCKETPAW_CODEAGENT_MODEL", "POCKETPAW_WEBSANDBOX_EDIT_MODEL"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return _DEFAULT_MODEL


async def _run_model(system: str, messages: list[dict], *, client) -> str:  # noqa: ANN001
    """Call the model and return its text. ``client`` is the DI seam.

    Builds a real ``AsyncAnthropic`` only when nothing is injected, so the whole
    suite runs without a key and without a network — the same seam discipline
    ``broker.py`` uses to keep git and tar out of its tests. This is also what
    lets CA-1…CA-4 be built and merged while CA-0 (confirming the live model
    route) is still outstanding.

    Every failure becomes a clean ``CloudError``. A model call that half-works
    must never be reported as an answer.
    """
    if client is None:
        try:
            from anthropic import AsyncAnthropic

            from pocketpaw.config import get_settings

            api_key = get_settings().anthropic_api_key
        except Exception as exc:  # noqa: BLE001 — a missing dep is "unconfigured", not a crash
            raise with_cause(
                CloudError(503, "codeagent.unavailable", "The agent model is not configured"),
                exc,
            ) from exc
        if not api_key:
            raise CloudError(503, "codeagent.unavailable", "The agent model is not configured")
        client = AsyncAnthropic(api_key=api_key, timeout=MODEL_TIMEOUT_SECONDS, max_retries=1)

    try:
        response = await client.messages.create(
            model=_model(),
            max_tokens=MAX_OUTPUT_TOKENS,
            # Adaptive thinking is OFF unless asked for on this model family, and
            # a question about unfamiliar code is exactly where reasoning pays.
            # `display` stays default (omitted) — the panel renders the answer,
            # not the reasoning, so summarising it would only cost tokens.
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[{"type": "text", "text": system}],
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a clean 502 below
        logger.warning("codeagent: model call failed", exc_info=True)
        raise with_cause(
            CloudError(502, "codeagent.failed", "The agent model call failed"),
            exc,
        ) from exc

    # A safety decline arrives as a successful response with an empty body, so
    # it must be checked BEFORE reading content or the read raises IndexError
    # and the user gets "the model call failed" for something that is not a
    # failure.
    if getattr(response, "stop_reason", None) == "refusal":
        raise CloudError(
            422,
            "codeagent.refused",
            "The agent declined to answer this request",
        )

    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise CloudError(502, "codeagent.failed", "The agent model returned no content")
    return text


async def run_turn(
    workspace_id: str,
    user_id: str,
    body: AgentTurnRequest | dict,
    *,
    client=None,  # noqa: ANN001 — an AsyncAnthropic-shaped object (DI seam for tests)
) -> AgentTurnResponse:
    """Answer one Ask-mode turn about the caller's code.

    Flow: validate → pack the context to the budget → assemble the final user
    turn → call the model → return the answer with an honest account of what was
    kept and what was dropped.

    Read-only by construction: CA-1 exposes no tools, so the model has no
    mechanism to change a file even if the prompt failed to dissuade it.
    """
    body = AgentTurnRequest.model_validate(body)

    # Tenancy is carried for metering and logs. There is no sandbox row to
    # authorize against — the caller already owns everything it sent us.
    logger.debug(
        "codeagent.turn ws=%s user=%s messages=%d context=%d",
        workspace_id,
        user_id,
        len(body.messages),
        len(body.context),
    )

    packed = pack_context(body.context)

    # The context rides on the FINAL user turn only. Attaching it to every turn
    # would re-send every file on every follow-up question, and the earlier
    # copies would be stale the moment the user edits a buffer.
    messages = [m.model_dump() for m in body.messages]
    last = messages[-1]
    if last["role"] != "user":
        raise CloudError(
            400,
            "codeagent.invalid_turn",
            "The last message must be from the user",
        )
    last["content"] = build_user_content(last["content"], packed)

    answer = await _run_model(ASK_SYSTEM_PROMPT, messages, client=client)

    return AgentTurnResponse(
        answer=answer,
        citedPaths=packed.kept,
        droppedPaths=packed.dropped,
        truncated=packed.truncated,
    )


__all__ = ["run_turn"]
