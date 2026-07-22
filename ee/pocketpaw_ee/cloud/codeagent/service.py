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
#
# Modified: 2026-07-21 (CA-4, Edit mode). The turn now runs under a MODE, and the
# mode picks the system prompt, the offered tools, and the filter that enforces
# them — all from one row in ``domain._MODES``, so a mode cannot be half-added.
# Edit widens the set to include ``writeFile``; it does not remove the filter.
# That matters more than it looks: the client executes whatever it is handed, so
# this filter is the only thing standing between a hallucinated ``deleteEntry``
# and a browser that would run it.
#
# Modified: 2026-07-22 (CD-1, delegate channel). Adds ``resolve_delegate`` — the
# service half of ``POST /codeagent/resolve``. It is a thin pass-through to
# ``delegates.resolve_pending`` and exists only so the router keeps ONE shape
# (router → service) for both of its routes; the rendezvous logic itself has no
# business in this file, which is about calling a model.
from __future__ import annotations

import logging
import os

from pocketpaw_ee.cloud._core.errors import CloudError, with_cause
from pocketpaw_ee.cloud.codeagent import delegates, transport
from pocketpaw_ee.cloud.codeagent.domain import (
    MAX_OUTPUT_TOKENS,
    MAX_TOOL_ITERATIONS,
    MAX_TOOL_RESULT_CHARS,
    MODEL_TIMEOUT_SECONDS,
    build_user_content,
    mode_config,
    pack_context,
)
from pocketpaw_ee.cloud.codeagent.dto import (
    AgentTurnRequest,
    AgentTurnResponse,
    DelegateResolveRequest,
    DelegateResolveResponse,
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


def _api_key() -> str:
    """The Anthropic key, or "" when there is none. Never raises — a config
    import that blows up is "unconfigured", not a crash."""
    try:
        from pocketpaw.config import get_settings

        return (get_settings().anthropic_api_key or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("codeagent: settings unavailable while resolving a key", exc_info=True)
        return ""


def _default_client():  # noqa: ANN202
    """Pick a transport. A KEY is preferred; the CLI is the fallback that works
    without one.

    The order is not a preference for Anthropic-the-company, it is a preference
    for the NATIVE TOOL CHANNEL. With a key we get real `tool_use` blocks,
    schema-validated by the API. Without one, `transport.ClaudeCliClient` carries
    the same protocol in the prompt and parses it back — correct, and one step
    less certain, so it is second.

    Reaching the CLI at all is what makes this feature work in AGENT MODE, where
    there is no key by design and where every deployment of this product runs.
    That is the case `codeagent` used to 503 on, in defiance of a rule written
    down in `instinct/auto_triage.py` — see transport.py's header.
    """
    key = _api_key()
    if key:
        try:
            from anthropic import AsyncAnthropic
        except Exception as exc:  # noqa: BLE001
            raise with_cause(
                CloudError(
                    503,
                    "codeagent.unavailable",
                    "An API key is set but the Anthropic SDK is not installed. "
                    "Run `uv sync --dev --group ee`.",
                ),
                exc,
            ) from exc
        return AsyncAnthropic(api_key=key, timeout=MODEL_TIMEOUT_SECONDS, max_retries=1)

    executable = transport.claude_executable()
    if executable:
        logger.info("codeagent: no API key; using the claude CLI at %s", executable)
        return transport.ClaudeCliClient(executable)

    # Both doors shut. The message names BOTH ways out, because which one a
    # reader wants depends on where they are running, and "not configured" told
    # them neither.
    raise CloudError(
        503,
        "codeagent.unavailable",
        "The agent model is not configured. Either install the Claude CLI "
        "(so it can authenticate itself, no key needed), or set "
        "POCKETPAW_ANTHROPIC_API_KEY to a key from console.anthropic.com. "
        "Then restart the backend.",
    )


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
        client = _default_client()

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


def _tool_calls_of(response, permitted: frozenset[str]) -> list[ToolCall]:  # noqa: ANN001
    """Pull the tool_use blocks the model wants executed.

    A name outside ``permitted`` is DROPPED rather than forwarded. The client
    executes whatever it is handed, so this filter is the enforcement and the
    prompt is only the explanation — without it, "Ask cannot edit" would mean
    "we asked the browser nicely not to". Edit mode WIDENS this set; it never
    turns the check off.
    """
    calls: list[ToolCall] = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = getattr(block, "name", "")
        if name not in permitted:
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
    """Run ONE step of a turn, in whichever mode the caller asked for.

    Flow: validate → resolve the mode's prompt and permission set → pack the
    context to the budget → assemble the final user turn → replay any tool
    traffic already executed → call the model → either return its answer, or
    return the tool calls the client should run next.

    The loop lives on the CLIENT, and that is the design rather than a
    limitation. The tools are `CodeFileSession`'s own verbs, which only the
    client can execute — it has them against a Daytona socket and against the
    in-tab WebContainer fs. A server-side loop would have to reach into the
    browser to run them, which is the shape the whole module exists to avoid.

    Ask is read-only by construction: only the three read verbs are offered, and
    a call naming anything else is dropped rather than forwarded. Edit adds
    ``writeFile`` — which the client stages for the user's per-hunk review rather
    than applying — and nothing else.
    """
    body = AgentTurnRequest.model_validate(body)
    system, tools, permitted = mode_config(body.mode)

    # Tenancy is carried for metering and logs. There is no sandbox row to
    # authorize against — the caller already owns everything it sent us.
    logger.debug(
        "codeagent.turn ws=%s user=%s mode=%s messages=%d context=%d tools=%d",
        workspace_id,
        user_id,
        body.mode,
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
        system,
        messages,
        tools=None if exhausted else tools,
        client=client,
    )

    calls = [] if exhausted else _tool_calls_of(response, permitted)
    if calls:
        return AgentTurnResponse(
            done=False,
            # Text the model wrote ALONGSIDE the calls. Ask ignores it and loops
            # again; Edit shows it, because a `writeFile` call ends the loop at
            # the review gate and this sentence is the model's explanation of
            # what it changed. Dropping it would send the proposal up unlabelled.
            answer=_text_of(response),
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


async def resolve_delegate(
    workspace_id: str,
    user_id: str,
    body: DelegateResolveRequest | dict,
) -> DelegateResolveResponse:
    """Deliver the browser's answer to the turn parked on ``body.corrId``.

    Raises ``NotFound`` when nothing is parked under that id — unknown, already
    resolved, already timed out, or belonging to another workspace all land
    there, deliberately (see ``delegates.resolve_pending``).

    ``user_id`` is carried for logging symmetry with ``run_turn`` and is NOT an
    authorization input: the delegate belongs to a workspace's live stream, and
    two tabs of the same workspace legitimately share it.
    """
    # no-event: nothing is persisted. The whole write is waking an in-process
    # future, and the caller it wakes is the thing that goes on to emit.
    body = DelegateResolveRequest.model_validate(body)
    logger.debug(
        "codeagent.resolve ws=%s user=%s corr=%s",
        workspace_id,
        user_id,
        body.corrId,
    )
    delegates.resolve_pending(workspace_id, body.corrId, body.result)
    return DelegateResolveResponse(accepted=True)


__all__ = ["resolve_delegate", "run_turn"]
