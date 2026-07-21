# domain.py — Pure domain rules for the Code Mode agent turn (CA-1).
#
# Created 2026-07-21 (feat/codeagent-turn). No I/O, no FastAPI, no SDK: limits,
# the server-owned system prompt, and the context-packing rule. Everything here
# is a pure function so the packing policy can be tested without a model.
#
# Why the packing rule lives in its own module: the budget is the whole of
# "context management" the user can see, and a budget that silently drops the
# file the answer depended on is worse than no budget at all. ``pack_context``
# therefore returns what it KEPT and what it DROPPED, and the caller is expected
# to report both. It never reorders silently either — items are kept in the
# order the client sent them, because the client sends them in priority order
# (selection first, then the active file, then pinned @-mentions).
from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids a dto -> domain cycle
    from pocketpaw_ee.cloud.codeagent.dto import ContextItem

# ── Wire limits ─────────────────────────────────────────────────────────────
# Bounds on what a single turn may carry. These are DoS/cost guards, not model
# limits — they sit well inside any frontier context window so that hitting one
# is a sign the client is misbehaving, not that the user wrote a long file.
MAX_MESSAGES = 40
MAX_MESSAGE_CHARS = 20_000
MAX_CONTEXT_ITEMS = 20
MAX_PATH_CHARS = 1024

# Total characters of file content allowed across all context items in one turn.
# Roughly 50k tokens of code — generous for an ask, and bounded enough that a
# runaway client cannot bill an unbounded completion.
MAX_CONTEXT_CHARS = 200_000

# Ceiling on the model's reply.
MAX_OUTPUT_TOKENS = 4096

# Wall-clock ceiling for the model call, in seconds.
MODEL_TIMEOUT_SECONDS = 120

# ── The server-owned system prompt ──────────────────────────────────────────
# Ask mode is READ-ONLY by construction: CA-1 exposes no tools at all, so the
# model has no mechanism to change a file even if it wanted to. The prompt says
# so anyway, because a model that believes it can edit will answer as though it
# already has ("I've updated the handler") and the user will believe it.
ASK_SYSTEM_PROMPT = (
    "You are a coding assistant embedded in an IDE. You answer questions about "
    "the user's code.\n\n"
    "You are in READ-ONLY mode. You cannot modify files, run commands, or apply "
    "changes, and nothing you say will be applied automatically. If the answer "
    "is a change, SHOW the code the user should write and say where it goes — "
    "never claim you have made the change.\n\n"
    "You are given only the excerpts listed below. If the answer depends on code "
    "you were not given, say precisely which file or symbol you would need "
    "rather than guessing. A short honest answer beats a confident invented one.\n\n"
    "Refer to files by the paths given. Be concise; the user is reading this in "
    "a narrow side panel."
)


class PackedContext(NamedTuple):
    """The outcome of applying the budget to a turn's context.

    ``text`` is the rendered block handed to the model; ``kept`` and ``dropped``
    are the paths on each side of the budget line, reported to the user verbatim.
    """

    text: str
    kept: list[str]
    dropped: list[str]

    @property
    def truncated(self) -> bool:
        return bool(self.dropped)


def render_item(item: ContextItem) -> str:
    """Render one context item as a labelled code block.

    A selection carries its 1-based inclusive line range into the label so the
    model can talk about line numbers the user can actually see in the editor.
    """
    if item.startLine is not None and item.endLine is not None:
        header = f"File `{item.path}` (lines {item.startLine}-{item.endLine}, selected):"
    else:
        header = f"File `{item.path}`:"
    return f"{header}\n```\n{item.content}\n```"


def pack_context(items: list[ContextItem]) -> PackedContext:
    """Fit context items into ``MAX_CONTEXT_CHARS``, keeping client priority order.

    Whole items only — a half-truncated file reads to the model as a complete
    one and invites an answer about code that was cut off. An item too large to
    ever fit is dropped rather than clipped, and it is reported. Later items are
    still considered after a large one is dropped, so one oversized file does not
    starve the small ones behind it.
    """
    kept_parts: list[str] = []
    kept: list[str] = []
    dropped: list[str] = []
    used = 0

    for item in items:
        rendered = render_item(item)
        cost = len(rendered)
        if used + cost > MAX_CONTEXT_CHARS:
            dropped.append(item.path)
            continue
        kept_parts.append(rendered)
        kept.append(item.path)
        used += cost

    return PackedContext(text="\n\n".join(kept_parts), kept=kept, dropped=dropped)


def build_user_content(question: str, packed: PackedContext) -> str:
    """Assemble the final user turn: the context block, then the question.

    The question goes LAST so it is the most recent thing in the model's view —
    with a large context block the trailing position measurably improves how well
    the model actually answers what was asked instead of summarising the code.
    """
    if not packed.text:
        return question
    parts = [
        "Here are the excerpts I am looking at:",
        packed.text,
    ]
    if packed.dropped:
        parts.append(
            "(Some excerpts were omitted to fit the context budget: "
            + ", ".join(packed.dropped)
            + ". Say so if you need one of them.)"
        )
    parts.append(f"My question:\n{question}")
    return "\n\n".join(parts)


__all__ = [
    "ASK_SYSTEM_PROMPT",
    "MAX_CONTEXT_CHARS",
    "MAX_CONTEXT_ITEMS",
    "MAX_MESSAGES",
    "MAX_MESSAGE_CHARS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PATH_CHARS",
    "MODEL_TIMEOUT_SECONDS",
    "PackedContext",
    "build_user_content",
    "pack_context",
    "render_item",
]
