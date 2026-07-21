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
# Modified: 2026-07-21 (CA-2, retrieval loop). ``run_turn`` is now ONE STEP of a
# turn, not the whole turn: it either answers or returns the files the model
# wants read. The loop lives on the client, which is the only place that can
# execute the tools — they are `CodeFileSession`'s own read verbs, and the client
# has them against a Daytona socket AND against the in-tab WebContainer fs. A
# server-side loop would have to reach into the browser to run them, which is the
# shape this module exists to avoid.
#
# Stateless still holds, and costs something: the server keeps no record of what
# it asked for, so the client hands back each call's name and input alongside its
# output and ``_replay_tool_exchanges`` rebuilds both halves for the model.
from __future__ import annotations

import logging
import os

from pocketpaw_ee.cloud._core.errors import CloudError, with_cause
from pocketpaw_ee.cloud.codeagent.domain import (
    ASK_SYSTEM_PROMPT,
    ASK_TOOL_NAMES,
    ASK_TOOLS,
    MAX_OUTPUT_TOKENS,
    MAX_TOOL_ITERATIONS,
    MAX_TOOL_RESULT_CHARS,
    MODEL_TIMEOUT_SECONDS,
    build_user_content,
    pack_context,
)
from pocketpaw_ee.cloud.codeagent.dto import (
    AgentTurnRequest,
    AgentTurnResponse,
    ToolCall,
    ToolResult,
)

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


def _replay_tool_exchanges(results: list[ToolResult]) -> list[dict]:
    """Rebuild the assistant/user block pairs for tool traffic already executed.

    The server kept no record of what it asked for — that is what stateless
    means — so both halves are reconstructed from what the client hands back.
    Each exchange becomes an assistant turn holding the ``tool_use`` block and a
    user turn holding the matching ``tool_result``.

    They are emitted as one pair per exchange rather than batching a round's
    calls into a single assistant turn. The model reads either shape, and one
    pair per exchange means a client that drops or reorders a result cannot
    produce a turn with an unanswered ``tool_use`` in it, which the API rejects
    outright.
    """
    out: list[dict] = []
    for r in results:
        out.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": r.id, "name": r.name, "input": r.input}],
            }
        )
        body = r.output[:MAX_TOOL_RESULT_CHARS]
        if len(r.output) > MAX_TOOL_RESULT_CHARS:
            body += "\n…(truncated)"
        out.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.id,
                        "content": body or "(empty)",
                        "is_error": r.isError,
                    }
                ],
            }
        )
    return out


async def _run_model(system: str, messages: list[dict], *, tools: list[dict] | None, client):  # noqa: ANN001, ANN201
    """Call the model and return the raw response. ``client`` is the DI seam.

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

    kwargs: dict = {
        "model": _model(),
        "max_tokens": MAX_OUTPUT_TOKENS,
        # Adaptive thinking is OFF unless asked for on this model family, and
        # a question about unfamiliar code is exactly where reasoning pays.
        # `display` stays default (omitted) — the panel renders the answer,
        # not the reasoning, so summarising it would only cost tokens.
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "system": [{"type": "text", "text": system}],
        "messages": messages,
    }
    # Omitted entirely rather than passed empty on the final round: an empty
    # tool list is a different request shape, and leaving the key out is what
    # makes "the model cannot call anything now" true at the API rather than
    # merely intended.
    if tools:
        kwargs["tools"] = tools

    try:
        response = await client.messages.create(**kwargs)
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

    return response


def _text_of(response) -> str:  # noqa: ANN001
    """Join the text blocks. Thinking and tool_use blocks sit alongside the
    answer in ``content``; only text is the answer."""
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


def _tool_calls_of(response) -> list[ToolCall]:  # noqa: ANN001
    """Pull the tool_use blocks the model wants executed.

    A name outside ``ASK_TOOL_NAMES`` is DROPPED rather than forwarded. Ask mode
    is meant to be read-only by construction, and the client executes whatever it
    is handed — so if a mutating verb ever reached this list (a stale tool set, a
    hallucinated name), forwarding it would turn "cannot edit" into "asked the
    browser nicely not to".
    """
    calls: list[ToolCall] = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = getattr(block, "name", "")
        if name not in ASK_TOOL_NAMES:
            logger.warning("codeagent: dropping unexpected tool call %r", name)
            continue
        calls.append(
            ToolCall(
                id=block.id,
                name=name,
                input=dict(getattr(block, "input", {}) or {}),
            )
        )
    return calls


async def run_turn(
    workspace_id: str,
    user_id: str,
    body: AgentTurnRequest | dict,
    *,
    client=None,  # noqa: ANN001 — an AsyncAnthropic-shaped object (DI seam for tests)
) -> AgentTurnResponse:
    """Run ONE step of an Ask-mode turn.

    Flow: validate → pack the context to the budget → assemble the final user
    turn → replay any tool traffic already executed → call the model → either
    return the answer, or return the files it wants read next.

    The loop lives on the CLIENT, and that is the design rather than a
    limitation. The tools are `CodeFileSession`'s own read verbs, which only the
    client can execute — it has them against a Daytona socket and against the
    in-tab WebContainer fs. A server-side loop would have to reach into the
    browser to run them, which is the shape the whole module exists to avoid.

    Read-only by construction: only the three read verbs are offered, and a call
    naming anything else is dropped rather than forwarded — the client executes
    what it is handed, so filtering here is the enforcement.
    """
    body = AgentTurnRequest.model_validate(body)

    # Tenancy is carried for metering and logs. There is no sandbox row to
    # authorize against — the caller already owns everything it sent us.
    logger.debug(
        "codeagent.turn ws=%s user=%s messages=%d context=%d tools=%d",
        workspace_id,
        user_id,
        len(body.messages),
        len(body.context),
        len(body.toolResults),
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

    # Tool traffic for THIS question replays after it, in the order it happened.
    messages.extend(_replay_tool_exchanges(body.toolResults))

    # Spend the budget, then withhold the tools entirely. Asking the model to
    # "please stop looking now" would leave it free to call again; taking the
    # tools away leaves it no option but to answer with what it has, which is a
    # better outcome than failing a question it can mostly answer.
    exhausted = len(body.toolResults) >= MAX_TOOL_ITERATIONS
    if exhausted:
        messages.append(
            {
                "role": "user",
                "content": (
                    "You have used your file-reading budget for this question. "
                    "Answer now with what you have, and say plainly what you "
                    "could not check."
                ),
            }
        )

    response = await _run_model(
        ASK_SYSTEM_PROMPT,
        messages,
        tools=None if exhausted else ASK_TOOLS,
        client=client,
    )

    calls = [] if exhausted else _tool_calls_of(response)
    if calls:
        return AgentTurnResponse(
            done=False,
            toolCalls=calls,
            citedPaths=packed.kept,
            droppedPaths=packed.dropped,
            truncated=packed.truncated,
        )

    answer = _text_of(response)
    if not answer:
        # Reached when the model returned only tool_use blocks that were ALL
        # filtered out, as well as on a genuinely empty completion. Either way
        # there is nothing to show, and a blank bubble is worse than an error.
        raise CloudError(502, "codeagent.failed", "The agent model returned no content")

    return AgentTurnResponse(
        done=True,
        answer=answer,
        citedPaths=packed.kept,
        droppedPaths=packed.dropped,
        truncated=packed.truncated,
    )


__all__ = ["run_turn"]
