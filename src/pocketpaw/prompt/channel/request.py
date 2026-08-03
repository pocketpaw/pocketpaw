"""The channel blocks built from THIS REQUEST — who asked, where, about what.

Created: 2026-08-03 (PA-7a, feat/prompt-assembler-channel).
Updated: 2026-08-03 (PA-8a, feat/prompt-bulk-retrieval) — ``current_pocket``
  stops being able to price itself out of the prompt. The block dumped the
  client's widget summary with no bound, so a 300-widget pocket rendered it at
  ~41,000 chars against a 32,000-char budget and the budget dropped it WHOLE:
  the id, the ``<current-pocket>`` tag and the ``get_pocket`` instruction went
  with it, on exactly the pockets too big to answer without a fetch. Two changes
  and one non-change. (1) The widget list is bounded BEFORE serialisation, whole
  elements only, so the block has a constant ceiling and the tail — where the
  instruction lives — is never what gets cut. (2) A new setting,
  ``prompt_pocket_summary_only`` (default OFF), drops bulk detail entirely and
  leaves id / name / widget COUNT / a snapshot stamp plus the same order to
  fetch. (3) ``max_chars`` is still ``None``, and now for a reason: this
  assembler's cap truncates the tail, so it would trade a missing block for a
  corrupt one. Default-off, and byte-identical there for every input that does
  not hit the new bound.

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
PA-8a came back with the measurements for ``current_pocket`` and the answer was
NOT a cap: see :class:`ChannelCurrentPocketLayer`. ``pocket_context`` is still
uncapped and still unmeasured.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

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


# --------------------------------------------------------------------------
# ``<current-pocket>`` bounds (PA-8a)
# --------------------------------------------------------------------------
# The two caller-supplied fields the block renders that have no bound on the
# wire. ``api.v1.schemas.chat.PocketContext`` declares ``widgets: list[dict]``
# and ``name: str`` with no ``max_length`` on either, and the block interpolates
# both, so the block's length is whatever the client posted. Measured: a
# 300-widget pocket renders it at ~41,000 chars, past the whole 32,000-char
# default budget, and the budget answers by dropping the block WHOLE — taking
# the pocket id and the ``get_pocket`` instruction with it. See
# ``ChannelCurrentPocketLayer`` for why the bound is here and not a ``max_chars``.
#
# The numbers are the ones the cloud path already lives with: its pocket
# preamble renders the first 12 widgets under a 1500-char ceiling
# (``pocketpaw_ee.cloud.surface.handlers.pocket``), so 2000 chars of widget JSON
# is a slightly more generous version of a bound that has been in production
# since 2026-05-24. The name's 200 is not measured against anything — no real
# pocket name is close to it, and it exists so the block's ceiling is a constant
# rather than "a constant plus whatever the client sent".
_WIDGET_SUMMARY_MAX_CHARS = 2000
_POCKET_NAME_MAX_CHARS = 200


def _bounded_widget_summary(widgets: Any) -> tuple[Any, int]:
    """The head of ``widgets`` whose JSON fits the ceiling, and how many were cut.

    WHOLE WIDGETS ONLY, and that is the entire point of doing this here rather
    than letting the block be truncated after it is built. ``json.dumps`` output
    cut at a character boundary is not JSON; the model reads a dangling
    ``{"name": "Bur`` and has to guess whether the list ended. Admitting whole
    elements keeps the value parseable at any bound.

    Counted incrementally rather than by re-dumping the growing list, which
    would be O(n^2) on the pockets this exists for. The arithmetic is exact
    because ``json.dumps`` on a list at the default separators is
    ``"[" + ", ".join(dumps(item)) + "]"`` — so one more element costs its own
    dump plus the two-char ``", "``. Pinned by a test rather than trusted.

    NON-LISTS AND UNSERIALISABLE ELEMENTS PASS STRAIGHT THROUGH, unbounded. Both
    are shapes the wire schema says cannot happen and that ``metadata`` (a bare
    ``dict`` on every channel) can nonetheless carry. Today the first renders as
    whatever ``json.dumps`` makes of it and the second RAISES, which the
    assembler's render guard turns into a dropped layer. Reproducing both
    exactly keeps this function out of the byte-identity argument entirely: it
    can only ever shorten a well-formed list.
    """
    if not isinstance(widgets, list):
        return widgets, 0
    kept: list = []
    total = 2  # the enclosing "[]"
    for widget in widgets:
        try:
            piece = json.dumps(widget)
        except (TypeError, ValueError):
            # Let the caller's own ``json.dumps`` raise on this input exactly as
            # it does today, rather than inventing a rendering for it here.
            return widgets, 0
        cost = len(piece) + (2 if kept else 0)
        if total + cost > _WIDGET_SUMMARY_MAX_CHARS:
            break
        kept.append(widget)
        total += cost
    return kept, len(widgets) - len(kept)


def _bounded_name(value: Any) -> str:
    """The pocket name as the block renders it, bounded to a constant.

    Bounds the RENDERED form rather than the input, so a name that is not a
    ``str`` (which the wire schema forbids and ``metadata`` permits) is bounded
    too instead of interpolating a 10,000-element list into the prompt.
    """
    text = f"{value}"
    if len(text) <= _POCKET_NAME_MAX_CHARS:
        return text
    return text[:_POCKET_NAME_MAX_CHARS] + "..."


def _pocket_snapshot_stamp(pocket_context: dict) -> str:
    """A content hash of the descriptor the block was rendered from.

    THE FILED TASK ASKED FOR ``updatedAt`` AND IT IS THE ONE SIGNAL THAT CANNOT
    BE USED. beanie's ``Initializer.init_actions`` skips ``_``-prefixed
    attributes when it collects event hooks, and ``TimestampedDocument``'s hooks
    are ``_set_created`` / ``_set_updated``, so neither ever registers and every
    cloud document's ``updatedAt`` holds its construction-time value forever. A
    stamp on it would review clean and report every edit as "unchanged" — a
    freshness claim the field does not have. Same finding, independently, as
    ``pocketpaw_ee.cloud.surface.handlers._helpers.content_key``'s note.

    So: hash what we were actually given. ``sort_keys`` because a dict that
    round-tripped through a client may not preserve order and a stamp that moved
    on key order would report every turn as a change; ``default=str`` because
    ``metadata`` is a bare ``dict`` and can carry a ``datetime``.

    WHAT IT ATTESTS, precisely, because the name invites a stronger reading: the
    DESCRIPTOR the client posted, not the stored pocket document. A pocket edited
    by another user between two turns of this conversation moves this stamp only
    once the client posts the new descriptor. It is the right signal anyway,
    because the descriptor is what this block is a view of — a stamp over
    something the block never read could not tell you whether the block is stale.
    """
    try:
        serialised = json.dumps(pocket_context, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        # A descriptor that will not serialise still gets a stamp, because the
        # alternative is raising out of ``render`` and losing the whole block —
        # which is the failure PA-8a exists to close, arriving by a new route.
        serialised = repr(pocket_context)
    return _short_digest(serialised)


def _summary_only() -> bool:
    """Whether bulk widget detail is kept out of the block (PA-8a).

    ``is True`` RATHER THAN A TRUTHINESS TEST, for the reason
    :class:`ChannelIdentityLayer` spells out about ``isinstance``: several
    suites hand this path a ``MagicMock`` settings object, and a mock's
    auto-created attribute is TRUTHY. A bare ``if settings.x`` would silently
    flip this flag ON inside every test that stubs settings — including the byte
    goldens, whose entire job is to prove that it is OFF by default. The setting
    is a pydantic ``bool``, so the real value is always ``True`` or ``False`` and
    nothing legitimate is lost by demanding the former exactly.

    Failures resolve to OFF rather than propagating. A layer that raises is
    dropped by the assembler's render guard, and this is the layer whose absence
    is the bug being fixed — a settings load that goes wrong must not cost the
    agent its pocket id.
    """
    try:
        from pocketpaw.config import get_settings

        return getattr(get_settings(), "prompt_pocket_summary_only", False) is True
    except Exception:  # pragma: no cover - defensive, see docstring
        return False


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
        #
        # ONLY A NON-EMPTY ``str`` COUNTS AS A CLAIM, and the ``isinstance`` is
        # load-bearing rather than defensive habit. ``BootstrapContext`` declares
        # the field ``str | None``, but the bootstrap provider is a Protocol and
        # this layer reads whatever the object actually carries. Anything else
        # reaching ``LayerOutput`` lands in ``assembler._digest``, which calls
        # ``.encode`` on it — and that is OUTSIDE the render guard, so it does
        # not degrade the layer, it kills the turn. On this path that trade is
        # all downside: ``build_system_prompt`` discards the digest entirely.
        # Found by ``tests/test_memory_isolation.py`` and ``tests/test_mem0_store.py``,
        # whose providers are ``MagicMock``s — a mock's attribute is truthy, so
        # a bare ``or`` forwarded it and six suites went red.
        claim = ci.identity_cache_key
        return LayerOutput(
            text=_nonblank(ci.identity),
            cache_key=claim if isinstance(claim, str) and claim else _short_digest(ci.identity),
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
            f"\n# Current Conversation\nYou are speaking with sender_id={sender_id} (role: {role})."
        )
        if is_owner:
            block += "\nThis is your owner."
        else:
            block += (
                "\nThis is NOT your owner. Be helpful but do not share owner-private information."
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
    """Which pocket is open, and the standing order to fetch it before answering.

    PA-8a IS ABOUT THE ONE LINE THAT MADE THIS BLOCK SELF-DEFEATING. It
    ``json.dumps``-ed the client's widget summary with no bound, near the TOP,
    while the SCOPE rules and the ``get_pocket`` instruction sit at the BOTTOM.
    Measured on the real builder at the default 32,000-char budget, a
    300-widget pocket rendered this at ~41,000 chars — larger than the whole
    budget — so ``_fit_to_budget`` dropped it whole:

        Dropped prompt layer 'channel.current_pocket' (34988 chars, priority
        HIGH) — budget exhausted

    and the agent lost the pocket id, the ``<current-pocket>`` tag AND the
    instruction telling it the fetch exists. On exactly the pockets big enough
    to need a fetch, it was told there was none.

    A ``max_chars`` IS THE WRONG FIX AND STAYS ``None``. The assembler's cap
    truncates the TAIL, so capping this block cuts away the instruction that
    matters and leaves a half-serialised JSON dump where it used to be — it
    turns a missing block into a corrupt one. The bound belongs on the widget
    LIST, before serialisation, where it can drop whole elements and keep the
    value parseable. See :func:`_bounded_widget_summary`. (Two tests also assert
    this layer's ``max_chars`` is ``None``, on the grounds that the old
    ``_INJECTION_CAPS`` never gave it one; they still pass, and now for a
    reason rather than by inheritance.)

    ``settings.prompt_pocket_summary_only`` (default OFF) is the second half:
    ON, the block keeps the CHEAP summary — id, name, widget COUNT, a snapshot
    stamp — and the same standing order to fetch, moving bulk detail onto the
    tool-result path where a 300-widget pocket costs nothing until it is asked
    for. OFF is byte-for-byte the block shipped before PA-8a for every input
    that does not hit the bound.
    """

    name = "channel.current_pocket"
    priority: Priority = Priority.HIGH
    # STILL UNCAPPED, and now on purpose rather than by inheritance from a table
    # that had no row for it. The class docstring has the argument: this
    # assembler's cap truncates the tail, and this block's tail is the
    # ``get_pocket`` instruction. The ceiling this layer needs is enforced on its
    # INPUTS instead (``_WIDGET_SUMMARY_MAX_CHARS``, ``_POCKET_NAME_MAX_CHARS``),
    # which bounds the block to a constant plus the length of the pocket id
    # without ever cutting a line of the text.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        metadata = _inputs(ctx).metadata or {}
        pc = metadata.get("pocket_context")
        if not pc:
            return LayerOutput(text="", cache_key=_short_digest(""))

        # THE POCKET ID IS THE ONE FIELD WITH NO BOUND, deliberately. It is not
        # display text — it is the literal argument of the ``get_pocket`` call
        # this block exists to instruct, so a truncated id would produce a
        # confidently wrong tool call, which is worse than the oversized block
        # PA-8a is removing. Real ids are ObjectIds and uuids; the block's
        # ceiling is therefore "a constant plus three times the id".
        pocket_id = pc.get("id", "unknown")
        summary_only = _summary_only()

        if summary_only:
            widgets = pc.get("widgets", [])
            count = len(widgets) if isinstance(widgets, (list, tuple, dict, str)) else 0
            head = (
                f"\n<current-pocket>\n"
                f"id: {pocket_id}\n"
                f"name: {_bounded_name(pc.get('name', 'Untitled'))}\n"
                f"widgets_count: {count}\n"
                f"snapshot: {_pocket_snapshot_stamp(pc)}\n"
            )
            note = (
                "NOTE: this block deliberately carries NO widget detail.\n"
                "`widgets_count` is the only shape hint here, and it reads 0\n"
                "for a UISpec-tree pocket whose content lives in\n"
                "rippleSpec.ui — 0 does NOT mean the pocket is empty. Widget\n"
                "names, types, layout and data are NOT in this prompt; the\n"
                "call below is the only way to see them.\n"
                "`snapshot` is a content hash of the pocket descriptor this\n"
                "block was built from. If you have seen a different snapshot\n"
                "value in this conversation, the pocket changed and anything\n"
                "you remember about its contents is stale — fetch it again.\n"
            )
        else:
            bounded, omitted = _bounded_widget_summary(pc.get("widgets", []))
            head = (
                f"\n<current-pocket>\n"
                f"id: {pocket_id}\n"
                f"name: {_bounded_name(pc.get('name', 'Untitled'))}\n"
                f"widgets_summary: {json.dumps(bounded)}\n"
            )
            if omitted:
                # Emitted ONLY when something was actually cut, so a pocket
                # inside the bound renders the bytes it always did. A truncated
                # list that does not say it is truncated is a lie the model has
                # no way to catch — it would read a complete-looking JSON array
                # and conclude the pocket has 12 widgets.
                head += (
                    f"widgets_summary_truncated: showing {len(bounded)} of "
                    f"{len(bounded) + omitted} — call get_pocket below for the rest\n"
                )
            note = (
                "NOTE: `widgets_summary` is a shallow hint (names + types)\n"
                "and is OFTEN EMPTY for UISpec-tree pockets — absence here\n"
                "does NOT mean the pocket is empty. The real content lives\n"
                "in rippleSpec.ui.\n"
            )

        # SCOPE and the fetch instruction are byte-identical in both modes and
        # are written once. They are the part of the block that must survive
        # every bound — the reason the block exists at all.
        scope = (
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
        )
        fetch = (
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
        pocket_tag = head + scope + note + fetch
        # KEYED ON WHAT WAS RENDERED, in both modes, which is the discipline
        # ``surface.handlers._helpers.content_key`` argues for: the digest
        # cannot hold still while the prompt moves, and what a bound dropped is
        # not in the prompt, so no cached prompt is stale for it. In
        # summary-only mode the snapshot stamp is IN the text, so this key
        # reaches every descriptor change even though the rendered detail no
        # longer does — the stamp is what buys that, not a second key rule.
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
