# ee/pocketpaw_ee/agent/mcp_servers/other_hand.py — the Otherhand illustration
# tool (``pocketpaw_other_hand`` → ``illustrate``).
#
# Created 2026-08-28 (feat/other-hand-illustrate-tool).
#
# Why a tool at all: the notebook's real job is explaining, and a lot of
# explanations want a picture the pen cannot draw — a bee's wing venation, a
# cross-section, a mechanism. The agent is the one who knows mid-explanation
# that a picture would help, so it has to be able to ask for one.
#
# THE ARCHITECTURE POINT. This tool does NOT return the drawing to the model.
# A real generated illustration converts to several thousand points; that is
# ~150KB of JSON, and routing it through the context window would cost more
# than the illustration and crowd out the explanation it exists to support. So:
#
#   * the ops are pushed STRAIGHT TO THE CLIENT over the SSE side channel
#     (``push_sse_event``), the same road Code Mode and pocket mutations use;
#   * the model gets back one short line — it drew, how many shapes, how tall.
#     That is all it needs to write "as you can see above" and carry on.
#
# The client places it: ops come back in a canonical box anchored at 0,0, and
# the page offsets them to its own free_y. Only the client knows where the page
# is empty, and it already computes that for every turn.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_other_hand"
ILLUSTRATE_TOOL_ID = f"mcp__{SERVER_NAME}__illustrate"
OTHER_HAND_TOOL_IDS: tuple[str, ...] = (ILLUSTRATE_TOOL_ID,)

#: The SSE frame the page listens for.
ILLUSTRATION_EVENT = "other_hand_illustration"

#: The canonical box the drawing is generated into. Square because the
#: generator's own canvas is square, and svg_to_ink fits uniformly — a
#: non-square box would only add empty margin. The client scales/places it.
CANON_W = 760.0
CANON_H = 760.0


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# Stands in for "could not tell who this is" so the gate reads as one thing.
_NO_IDENTITY = object()


async def _guest_or_none() -> Any:
    """The current caller's guest record, or None for a real account.

    Resolved from the run context the same way the budget resolves tenancy, so
    the tool needs no new plumbing. Any failure to resolve returns a guest-like
    refusal rather than None: this gates SPEND, so an unknown caller is treated
    as the case we do not want to pay for.
    """
    try:
        from pocketpaw_ee.cloud.auth import guest_budget
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id

        user_id = current_user_id()
        if not user_id:
            logger.warning("other-hand: illustration refused, no user in context")
            return _NO_IDENTITY
        return await guest_budget.load_guest(user_id)
    except Exception:  # noqa: BLE001 - a spend gate refuses when it cannot tell
        logger.warning("other-hand: illustration refused, guest check failed", exc_info=True)
        return _NO_IDENTITY


def _ok(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


async def _illustrate_handler(args: dict) -> dict:
    """Generate an illustration, push it to the page, tell the model it landed."""
    from pocketpaw_ee.cloud.chat.agent_service import push_sse_event
    from pocketpaw_ee.cloud.other_hand import illustrate as illustrator
    from pocketpaw_ee.cloud.other_hand.svg_to_ink import Box
    from pocketpaw_ee.cloud.studio import fal_edit

    subject = str(args.get("subject") or "").strip()
    if len(subject) < 2:
        return _error("Say what to illustrate — a subject of at least two characters.")

    api_key = fal_edit.fal_api_key()
    if not api_key:
        # Not an error the agent should retry or apologise at length for. Tell
        # it plainly so it explains in words instead and moves on.
        return _error(
            "No illustrator is configured on this deployment. Explain in words "
            "and with your own drawing instead; do not try again."
        )

    from pocketpaw_ee.cloud.other_hand import illustration_budget as budget

    # Guests do not get to spend platform money on pictures, on this path
    # either. The REST route refuses them too; gating only there would leave
    # the whole feature reachable by simply ASKING the agent to draw, which is
    # the more natural way in. Refused before the budget is claimed, and worded
    # so the agent explains instead of retrying.
    guest = await _guest_or_none()
    if guest is not None:
        return _error(
            "Illustrations need an account. Say so plainly and offer to keep "
            "going in words and your own drawing; do not try again this turn."
        )

    allowed, spent, cap = await budget.try_spend()
    if not allowed:
        return _error(
            f"Today's illustration limit is used up ({spent}/{cap}). Explain in "
            "words and with your own drawing instead; do not try again today."
        )

    try:
        ops = await illustrator.illustrate_as_ops(
            subject,
            Box(x=0, y=0, w=CANON_W, h=CANON_H),
            api_key=api_key,
            # The agent asking IS the authorisation on this path: the tool is
            # only reachable from the Otherhand surface, which allow-lists it.
            allowed=True,
        )
    except illustrator.IllustrateError as exc:
        logger.info("other-hand illustrate failed: %s", exc)
        return _error(f"The illustrator failed ({exc}). Explain in words instead.")

    if not ops:
        return _error("Nothing drawable came back. Explain in words instead.")

    bottom = max((p[1] for op in ops for p in op["pts"]), default=0.0)
    push_sse_event(ILLUSTRATION_EVENT, {"ops": ops, "subject": subject, "height": bottom})

    # Deliberately terse. The model does not need the geometry, only enough to
    # refer to the picture in the sentence it writes next.
    return _ok(
        f"Drew '{subject}' on the page as {len(ops)} ink shapes, about "
        f"{int(bottom)} units tall. It is already on the paper — do NOT repeat "
        "it as page-ops. Write your explanation around it and refer to it."
    )


def build_other_hand_server() -> tuple[str, Any] | None:
    """Build the in-process MCP server. ``None`` when the SDK is absent."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        return None

    @tool(
        "illustrate",
        (
            "Draw a real illustration on the notebook page — something your pen "
            "cannot: an anatomy, a cross-section, a mechanism, a creature. The "
            "picture is drawn as INK on the same page, immediately, before you "
            "reply. Use it when a picture would genuinely carry the explanation, "
            "not for decoration and not for anything you can draw with page-ops "
            "yourself (arrows, boxes, graphs, simple diagrams). One per reply."
        ),
        {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "minLength": 2,
                    "description": (
                        "What to draw, as a plain noun phrase — 'a honeybee, side "
                        "view', 'a human heart in cross-section'. Describe the "
                        "SUBJECT only; the style is fixed to line art that suits "
                        "the page."
                    ),
                },
            },
            "required": ["subject"],
            "additionalProperties": False,
        },
    )
    async def illustrate(args):  # type: ignore[no-untyped-def]
        return await _illustrate_handler(args)

    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=[illustrate])
    return SERVER_NAME, server


__all__ = [
    "CANON_H",
    "CANON_W",
    "ILLUSTRATE_TOOL_ID",
    "ILLUSTRATION_EVENT",
    "OTHER_HAND_TOOL_IDS",
    "SERVER_NAME",
    "build_other_hand_server",
]
