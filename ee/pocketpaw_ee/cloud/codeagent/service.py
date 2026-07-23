# service.py — The Code Mode delegate channel's service half.
#
# History: this file used to run a whole STATELESS agent turn (CA-1..CA-4) — a
# second, self-contained model loop with its own transport, prompts, and tool
# filter, reached at ``POST /codeagent/turn``. That turn-agent was removed
# 2026-07-23 (remove/codeagent-turn-agent). It was a parallel reimplementation of
# an agent, and the /code surface is meant to run on the MAIN PocketPaw cloud
# agent instead — the one that already streams and handles tools through the
# claude_sdk backend. Two agents was one too many; the keyless-CLI turn-agent was
# the weaker one, so it went.
#
# What remains is the INBOUND half of the browser-delegate rendezvous. The main
# agent's ``code_mode`` tool parks on a future (``delegates.delegate_to_browser``)
# and the browser wakes it by POSTing here. This service function is a thin
# pass-through to ``delegates.resolve_pending``; it exists only so the router
# keeps ONE shape (router → service) for the ``/codeagent/resolve`` route. The
# rendezvous logic itself lives in ``delegates.py``.
from __future__ import annotations

import logging

from pocketpaw_ee.cloud.codeagent import delegates
from pocketpaw_ee.cloud.codeagent.dto import (
    DelegateResolveRequest,
    DelegateResolveResponse,
)

logger = logging.getLogger(__name__)


async def resolve_delegate(
    workspace_id: str,
    user_id: str,
    body: DelegateResolveRequest | dict,
) -> DelegateResolveResponse:
    """Deliver the browser's answer to the turn parked on ``body.corrId``.

    Raises ``NotFound`` when nothing is parked under that id — unknown, already
    resolved, already timed out, or belonging to another workspace all land
    there, deliberately (see ``delegates.resolve_pending``).

    ``user_id`` is carried for logging symmetry with the rest of the cloud
    surface and is NOT an authorization input: the delegate belongs to a
    workspace's live stream, and two tabs of the same workspace legitimately
    share it.
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


__all__ = ["resolve_delegate"]
