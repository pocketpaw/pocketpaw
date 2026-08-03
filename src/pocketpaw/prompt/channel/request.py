"""The channel blocks built from THIS REQUEST — who asked, where, about what.

Created: 2026-08-03 (PA-7a, feat/prompt-assembler-channel).

Ten of the channel path's fifteen blocks, lifted out of
``AgentContextBuilder.build_system_prompt`` byte for byte. Every one of them is
a function of what the caller sent — the bootstrap identity resolved for this
turn, the memories and articles retrieved for this message, who is speaking,
which channel they are on, which pocket is open, which files the desktop client
has selected. The other five come from the box the process is running on and
live in :mod:`.environment`.

WHY THAT SPLIT AND NOT ONE MODULE PER LAYER. Fifteen files of thirty lines each
buries the one thing worth seeing — that these blocks share an input object and
those blocks share a failure mode. The five in :mod:`.environment` are exactly
the five the old builder had to wrap in ``try/except`` (health, skills, atlas,
AGENTS.md, GWS), because each reaches for a loader that can be missing or
broken; :func:`~pocketpaw.prompt.assembler.assemble`'s render guard is what
replaces those five ``except`` clauses. Nothing in THIS module can raise on a
resource, so nothing in it needed a guard before and nothing does now. The line
between the two files is that property, not file size.

NOT ONE BYTE MOVED. Each ``render`` reproduces its block's text exactly,
including the leading ``\\n`` most of them carry, the em dashes, and the
``\\n\\n## Current Context`` the channel-instructions block appends. The block
ORDER moved out of this file entirely — see :mod:`pocketpaw.prompt.channel`,
which owns it, because the old assembler re-sorted by priority and the new one
never reorders.

EVERY LAYER RENDERS ``""`` FOR "NOTHING TO SAY", VIA :func:`_nonblank`.
``_assemble_with_budget`` skipped a block when ``not content.strip()``;
``assemble`` skips one only when the text is EMPTY. Those agree only while a
layer with nothing to contribute returns ``""`` rather than whitespace, and a
layer returning ``"   "`` would silently add a separator to every prompt. Two of
these blocks (``pocket_context``, ``identity``) render caller-supplied text and
so cannot promise otherwise on their own, which is why the guard is applied by
all ten rather than argued per layer.

THE CAPS ARE ``_INJECTION_CAPS``' NUMBERS, UNCHANGED, now declared on the layer
that owns each one. Two of them were MISSING from that table and are therefore
``None`` here: ``pocket_context`` and ``current_pocket`` have never been capped
on any channel turn. That is recorded as an explicit ``max_chars = None`` with a
note rather than left to read as an oversight — PA-9 is the task with the
measurements to change it, and PA-7a's acceptance is that no byte moves.
"""

from __future__ import annotations

import hashlib
import json
import re

from pocketpaw.prompt.channel.inputs import ChannelInputs
from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext

_NO_INPUTS = ChannelInputs()


def _inputs(ctx: PromptContext) -> ChannelInputs:
    """The channel inputs, or an empty set for a caller that has none.

    A cloud assembly never sets ``channel_inputs``. Returning the empty default
    rather than raising means a channel layer that finds its way into a cloud
    layer list renders nothing and costs nothing, which is how every other
    plain-data layer in this package already behaves when its channel is unset.
    """
    return ctx.channel_inputs or _NO_INPUTS


def _nonblank(text: str) -> str:
    """``""`` unless there is something other than whitespace to say.

    Reproduces ``_assemble_with_budget``'s ``not content.strip()`` skip. See the
    module docstring — the new assembler only skips EMPTY text, so the guard has
    to move into the layers or a whitespace-only block starts contributing a
    separator.
    """
    return text if text.strip() else ""


def _short_digest(value: str) -> str:
    """16 hex chars of sha256 — the bound the rest of this package uses."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


class ChannelIdentityLayer:
    """The bootstrap provider's system prompt — persona, soul, style, profile."""

    name = "channel.identity"
    priority: Priority = Priority.CRITICAL
    # UNCAPPED, from ``_INJECTION_CAPS["identity"] = None``, and for the reason
    # ``prompt.identity`` spells out at length: this key deliberately
    # under-reports its text when a soul provider made a claim, and a cap on a
    # key like that would let a growing counter push stable persona bytes off
    # the end.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        ci = _inputs(ctx)
        # The provider's own claim wins when it made one — a soul renders mood,
        # energy, bond level and a memory count that all move on ordinary
        # interaction, and only the provider knows which of those bytes mean
        # anything. With no claim, hash the text: it over-keys (the digest moves
        # when a counter does) and over-keying is the safe direction.
        return LayerOutput(
            text=_nonblank(ci.identity),
            cache_key=ci.identity_cache_key or _short_digest(ci.identity),
        )


class ChannelMemoryLayer:
    """Memories retrieved for this message, already fetched by the builder."""

    name = "channel.memory_context"
    priority: Priority = Priority.HIGH
    max_chars: int | None = 4000  # _INJECTION_CAPS["memory_context"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        memory_context = _inputs(ctx).memory_context
        if not memory_context:
            return LayerOutput(text="", cache_key=None)
        return LayerOutput(
            text=_nonblank(
                "\n# Memory Context (already loaded — use this directly, "
                "do NOT call recall unless you need something not listed here)\n" + memory_context
            ),
            # VOLATILE. A semantic recall is keyed on the user's message, so a
            # key over these bytes would move every turn and make every turn
            # look like a new agent — the exact churn ``cache_key=None`` exists
            # to prevent (``RetrievalLayer`` answers the same way).
            cache_key=None,
        )


class ChannelKnowledgeBaseLayer:
    """kb-go articles retrieved for this message, already fetched by the builder."""

    name = "channel.kb_context"
    priority: Priority = Priority.HIGH
    max_chars: int | None = 3000  # _INJECTION_CAPS["kb_context"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        kb_context = _inputs(ctx).kb_context
        if not kb_context:
            return LayerOutput(text="", cache_key=None)
        return LayerOutput(
            text=_nonblank(
                "\n# Knowledge Base (relevant articles from the project wiki)\n"
                "These are compiled from source files. Use them for implementation "
                "details and current-state facts. Use soul_recall for past decisions "
                "and conversation history.\n\n" + kb_context
            ),
            cache_key=None,  # Per-message retrieval, same as the memory block.
        )


class ChannelSenderLayer:
    """Who is speaking, and whether they own this agent."""

    name = "channel.sender_block"
    priority: Priority = Priority.HIGH
    max_chars: int | None = 500  # _INJECTION_CAPS["sender_block"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        sender_id = _inputs(ctx).sender_id
        if not sender_id:
            return LayerOutput(text="", cache_key=_short_digest(""))

        from pocketpaw.config import get_settings

        settings = get_settings()
        if not settings.owner_id:
            # No configured owner means the agent cannot say whether this
            # sender is one, so it says nothing rather than guessing.
            return LayerOutput(text="", cache_key=_short_digest(""))

        is_owner = sender_id == settings.owner_id
        role = "owner" if is_owner else "external user"
        block = (
            f"\n# Current Conversation\n"
            f"You are speaking with sender_id={sender_id} (role: {role})."
        )
        if is_owner:
            block += "\nThis is your owner."
        else:
            block += (
                "\nThis is NOT your owner. Be helpful but do not share "
                "owner-private information."
            )
        # The text carries both the id and the role, so a digest of it moves on
        # a sender change AND on an owner-config change. Nothing is left for a
        # separate id half to discriminate.
        return LayerOutput(text=_nonblank(block), cache_key=_short_digest(block))


class ChannelPocketContextLayer:
    """The pocket-creation preamble the pocket chat endpoint hands in."""

    name = "channel.pocket_context"
    priority: Priority = Priority.HIGH
    # UNCAPPED — and it always has been. ``_INJECTION_CAPS`` has no
    # ``pocket_context`` entry, so ``_assemble_with_budget``'s
    # ``_INJECTION_CAPS.get(name)`` returned ``None`` and this block went into
    # every prompt at whatever length its caller chose. Written out rather than
    # omitted so it reads as inherited, not overlooked. PA-9 owns cap
    # arithmetic and is the task with the measurements.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        metadata = _inputs(ctx).metadata or {}
        text = metadata.get("pocket_system_context") or ""
        return LayerOutput(text=_nonblank(text), cache_key=_short_digest(text))


class ChannelCurrentPocketLayer:
    """Which pocket is open, and the standing order to fetch it before answering."""

    name = "channel.current_pocket"
    priority: Priority = Priority.HIGH
    # UNCAPPED, inherited — see ``ChannelPocketContextLayer``. This is the more
    # interesting of the two missing entries: the block ``json.dumps`` the
    # widget summary straight into the prompt with no bound, so a pocket with
    # many widgets writes an unbounded block. Left as-is deliberately; a cap
    # here would move bytes on live pockets, which is PA-9's call to make with
    # numbers rather than PA-7a's to make in passing.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        metadata = _inputs(ctx).metadata or {}
        pc = metadata.get("pocket_context")
        if not pc:
            return LayerOutput(text="", cache_key=_short_digest(""))

        pocket_id = pc.get("id", "unknown")
        widget_summary = pc.get("widgets", [])
        pocket_tag = (
            f"\n<current-pocket>\n"
            f"id: {pocket_id}\n"
            f"name: {pc.get('name', 'Untitled')}\n"
            f"widgets_summary: {json.dumps(widget_summary)}\n"
            f"\n"
            f"SCOPE — read this carefully before doing anything:\n"
            f'In this conversation, "pocket" / "this pocket" / "the\n'
            f'pocket" always means THIS workspace dashboard\n'
            f"(id ``{pocket_id}``) — a MongoDB document the user is\n"
            f"viewing on screen. It is NOT the PocketPaw application,\n"
            f"NOT the source tree on disk, NOT any file under\n"
            f'``D:\\paw`` or ``backend/`` or ``ee/cloud/``. "Edit the\n'
            f'pocket", "add a widget", "more widgets" all refer to\n'
            f"this document — operate on it through the\n"
            f"``mcp__pocketpaw_pocket__*`` tools ONLY. Do NOT use\n"
            f"shell, file_edit, grep, or web_search for pocket\n"
            f"operations — they cannot read or write the document.\n"
            f"\n"
            f"NOTE: `widgets_summary` is a shallow hint (names + types)\n"
            f"and is OFTEN EMPTY for UISpec-tree pockets — absence here\n"
            f"does NOT mean the pocket is empty. The real content lives\n"
            f"in rippleSpec.ui.\n"
            f"\n"
            f"BEFORE answering any question about this pocket's contents,\n"
            f"widgets, layout, data, or configuration, you MUST first call:\n"
            f"  tool: mcp__pocketpaw_pocket__get_pocket\n"
            f'  args: {{"pocket_id": "{pocket_id}"}}\n'
            f"That returns the full document (rippleSpec, widgets,\n"
            f"metadata, visibility). Base your answer on that, not on\n"
            f"the summary above.\n"
            f"</current-pocket>\n"
        )
        return LayerOutput(text=_nonblank(pocket_tag), cache_key=_short_digest(pocket_tag))


class ChannelInstructionsLayer:
    """The per-channel behaviour file (today: ``discord.md``) plus live context."""

    name = "channel.channel_instructions"
    priority: Priority = Priority.MEDIUM
    max_chars: int | None = 1000  # _INJECTION_CAPS["channel_instructions"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        ci = _inputs(ctx)
        if ci.channel is None:
            return LayerOutput(text="", cache_key=_short_digest(""))

        text = load_channel_instructions(ci.channel)
        if not text:
            return LayerOutput(text="", cache_key=_short_digest(""))

        meta = ci.metadata or {}
        username = meta.get("username", "")
        guild_id = meta.get("guild_id", "")
        ctx_lines = []
        if ci.sender_id:
            ctx_lines.append(f"sender_id: {ci.sender_id}")
        if username:
            ctx_lines.append(f"discord_username: {username}")
        if guild_id:
            ctx_lines.append(f"discord_guild_id: {guild_id}")
        if ctx_lines:
            text += "\n\n## Current Context\n" + "\n".join(ctx_lines)
        return LayerOutput(text=_nonblank(text), cache_key=_short_digest(text))


class ChannelSessionKeyLayer:
    """The session key the session-management tools need as an argument."""

    name = "channel.session_key"
    priority: Priority = Priority.MEDIUM
    max_chars: int | None = 200  # _INJECTION_CAPS["session_key"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        session_key = _inputs(ctx).session_key
        if not session_key:
            return LayerOutput(text="", cache_key=_short_digest(""))
        block = (
            f"\n# Session Management\n"
            f"Current session_key: {session_key}\n"
            f"Pass this value to any session tool (new_session, list_sessions, "
            f"switch_session, clear_session, rename_session, delete_session)."
        )
        return LayerOutput(text=_nonblank(block), cache_key=_short_digest(block))


class ChannelFileContextLayer:
    """What the desktop client has open, with the paths sanitised."""

    name = "channel.file_context"
    priority: Priority = Priority.MEDIUM
    max_chars: int | None = 2000  # _INJECTION_CAPS["file_context"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        file_context = _inputs(ctx).file_context
        if not file_context:
            return LayerOutput(text="", cache_key=_short_digest(""))

        parts = []
        if file_context.get("current_dir"):
            parts.append(f"Working directory: {_sanitize_path(file_context['current_dir'])}")
        if file_context.get("open_file"):
            parts.append(f"Open file: {_sanitize_path(file_context['open_file'])}")
        if file_context.get("selected_files"):
            safe_files = [_sanitize_path(f) for f in file_context["selected_files"]]
            parts.append(f"Selected files: {', '.join(safe_files)}")
        if not parts:
            return LayerOutput(text="", cache_key=_short_digest(""))

        block = "\n# File Context\n" + "\n".join(parts)
        return LayerOutput(text=_nonblank(block), cache_key=_short_digest(block))


class ChannelFormatHintLayer:
    """How to format a reply so the channel renders it natively."""

    name = "channel.channel_hints"
    priority: Priority = Priority.LOW
    max_chars: int | None = 500  # _INJECTION_CAPS["channel_hints"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        channel = _inputs(ctx).channel
        if channel is None:
            return LayerOutput(text="", cache_key=_short_digest(""))

        from pocketpaw.bus.format import CHANNEL_FORMAT_HINTS

        hint = CHANNEL_FORMAT_HINTS.get(channel, "")
        if not hint:
            return LayerOutput(text="", cache_key=_short_digest(""))
        block = f"\n# Response Format\n{hint}"
        return LayerOutput(text=_nonblank(block), cache_key=_short_digest(block))


def load_channel_instructions(channel) -> str:  # noqa: ANN001 - opaque Channel
    """Read the per-channel behaviour file, or ``""`` when there isn't one.

    Moved here from ``AgentContextBuilder._load_channel_instructions``. The file
    still lives beside the bootstrap package, so the directory is resolved from
    the installed ``pocketpaw`` package rather than from ``__file__`` — this
    module is two directories away and importing ``pocketpaw.bootstrap`` to ask
    would close an import cycle (bootstrap imports the prompt registry, which
    imports this). Pinned by
    ``tests/test_channel_prompt_layers.py::test_the_channel_files_resolve_to_the_bootstrap_package``.
    """
    from pocketpaw.bus.events import Channel

    _channel_files = {
        Channel.DISCORD: "discord.md",
    }
    filename = _channel_files.get(channel)
    if not filename:
        return ""
    path = _bootstrap_dir() / filename
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _bootstrap_dir():  # noqa: ANN202 - Path, but keep the import local
    from pathlib import Path

    import pocketpaw

    return Path(pocketpaw.__file__).resolve().parent / "bootstrap"


def _sanitize_path(p: str) -> str:
    """Strip non-path characters to prevent prompt injection."""
    return re.sub(r"[^\w\s\-./\\:~]", "", p).strip()
