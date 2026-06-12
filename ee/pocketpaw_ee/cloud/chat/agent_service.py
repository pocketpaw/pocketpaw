"""Cloud agent chat service — scope resolution, toolset assembly, context.

Keeps the router thin: the router handles HTTP + SSE plumbing; this module
handles *what the agent sees*:

* ``resolve_scope_context`` turns (scope, scope_id, user_id) into a
  ``ScopeContext`` including the target agent id, members, and
  pocket-scoped tool specs where applicable.
* ``load_history_for_scope`` rehydrates prior chat turns from Mongo so the
  agent carries context across backend restarts and pool evictions.

Changes: 2026-05-22 — ``ScopeContext`` carries the anchored pocket's
``pocket_type``; ``build_behavior_instructions`` appends ``HOME_POCKET_PROMPT``
when that type is ``"home"`` so the agent behaves correctly on the home page
(call ``add_widget`` for an explicit widget request, answer directly
otherwise). ``_resolve_pocket`` / ``_resolve_session`` populate the field.
Changes: 2026-05-22 (#1174) — ``build_behavior_instructions`` no longer emits
``POCKET_DELEGATION_RULE`` (nor the heavy interaction prompt) for a
``type="home"`` scope. The delegation rule ("never call add_widget, delegate
to the specialist") contradicts ``HOME_POCKET_PROMPT`` ("call add_widget");
the home agent now gets exactly one consistent widget-creation instruction.
Changes: 2026-05-22 (RFC 04 alpha follow-up 2) — ``build_behavior_instructions``
fills the interaction prompt's current-pocket block via ``fill_current_pocket``
(both the pocket-id and backend-summary tokens) instead of a bare
``POCKET_ID_TOKEN`` replace, so the new ``__BACKEND_SUMMARY__`` token never
leaks as literal text.
Changes: 2026-05-22 (Increment 3) — added ``push_pocket_execution``, the
SSE-sink push for the execution router's per-request ``pocket_execution``
observability frame.
Changes: 2026-05-24 — ``ScopeContext`` carries an optional
``surface_context`` (the resolved {surface_kind, meta, preamble} tuple
from ``surface_context.resolve_surface_context``). ``build_dynamic_context``
prepends its preamble before the legacy scope/participants/current-pocket
tags so the chat agent sees the surface snapshot first. Clients that
don't stamp a surface hint keep the old three-line shape unchanged —
``surface_context is None`` is the legacy path.
Changes: 2026-05-31 (feat/home-agent-source-authoring) — ``ScopeContext``
carries an optional ``backend_summary`` (the non-secret {base_url,
auth_type, configured} dict from ``pockets.service.get_pocket_backend``,
never the token). The resolvers populate it ONLY for a ``type="home"``
pocket (via the new ``_home_backend_summary`` helper);
``build_behavior_instructions`` fills it into ``HOME_POCKET_PROMPT``'s
``__BACKEND_SUMMARY__`` token via ``fill_current_pocket`` so the home agent
SEES whether a backend is configured (and its base_url) before authoring a
``sources`` block — fixing the smoke-test finding where the agent claimed
"no integration wired up" despite a configured backend. Non-home scopes
keep ``backend_summary=None`` and pay no extra read.
Changes: 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①)
— ``ScopeContext`` carries an optional ``resolved_profile`` (the ENTITY-AWARE
``SurfaceProfile`` resolved ONCE per run by ``run_core.execute_run``, which
folds a pocket-entity's ``surface_profile`` override over the surface base).
``build_behavior_instructions`` now gates the ripple block on
``ctx.resolved_profile.ripple_mode`` (pre-resolved, stays sync) instead of
calling ``resolve_profile`` itself — so a pocket bound to a room can flip
ripple off/on for that room. ``resolved_profile is None`` is the legacy /
non-entity path → ripple ON, byte-identical to today.
Changes: 2026-06-08 (feat/connector-mcp-execution / keystone) — the per-stream
identity now carries the room's ``pocket_id`` too. ``attach_agent_identity``
gained a ``pocket_id`` kwarg and ``current_pocket_id()`` was added beside
``current_workspace_id`` / ``current_user_id``; both are set in
``run_core`` and read by the connector-execution MCP server
(``mcp_servers/connectors.py``) so its tools scope to the current pocket.
The identity-token tuple grew from 3 to 4 entries; existing 3-arg callers are
unaffected (``pocket_id`` defaults to ``None``).

Changes: 2026-06-08 (feat/vip-agent-block, pp#1367) — ``ScopeContext`` carries
an optional ``about_member_block``: a concise, token-capped "about this member"
string (name · role · team · one-line focus) rendered from the member's Fabric
``Person`` (``people.service.get_person``). The resolvers pre-resolve it (async)
via ``_resolve_about_member`` and stash it; ``build_behavior_instructions``
APPENDS it to the base system message (additive — NOT a persona override) so the
agent greets the member by name from the first turn. A member with no Person
(pre-existing / non-invited user) → ``None`` → no block, behavior unchanged. The
render is HARD-capped (``_ABOUT_MEMBER_CHAR_CAP``) to kill the prompt-bloat
failure mode. Stays sync in ``build_behavior_instructions`` — the async read
happens once in the resolver, mirroring ``backend_summary``.

Changes: 2026-06-08 (VIP Onboarding Phase B — session-user isolation gate) —
``_kb_scopes_for_context`` now prepends a member-private ``user:{member_id}``
KB scope, GATED by the new ``_member_private_user_scope`` helper. The gate
emits the scope ONLY when ``ctx.members == [ctx.user_id]`` (the member's own
solo session) and suppresses it in every shared / multi-member room, so one
member's private Gmail/calendar KB is never injected into another member's
agent context. ``ctx.user_id`` (an opaque cloud user id) is the scope id —
no email, so kb-go's on-disk ``:``→``_`` sanitize can't alias two members.
Mirrors the OSS ``KbContext.user_id`` / ``_resolve_kb_scopes`` priority.
Changes: 2026-06-08 (VIP Onboarding Phase B chunk 5 — the "your day" briefing)
— ``_member_briefing_block`` builds a concise, capped (``_BRIEFING_MAX_CHARS``
≈ 400 tokens) "your day" block from the structured ``MemberDayDigest`` (the
per-member live mail/calendar pull) and ``build_knowledge_context`` PREPENDS
it. It is GATED by the SAME ``_member_private_user_scope`` decision as the
private ``user:`` KB scope — present ONLY in the member's solo session,
ABSENT (and the digest never even pulled) in every shared / multi-member
room. Graceful: an empty digest (no connected accounts) or a digest that
raises → ``""``, so unconnected and non-solo sessions are byte-identical to
before.
Changes: 2026-06-12 (fix/pocket-anchored-chat-context) — ``ScopeContext``
carries an optional ``pocket_summary``: the anchored pocket's orientation
data ({name, description, type, template_slug, pattern, ripple}) where
``ripple`` is ``spec_ops.summarize_ripple_spec`` over the pocket's
rippleSpec (top-level ui node count/types, capped state keys, source
summaries, action keys, legacy widgets count). Populated by
``_pocket_summary_data`` in ``_resolve_pocket`` AND ``_resolve_session``
(when the session is pocket-anchored) for ALL pocket types, from the
Pocket doc those resolvers already fetched — zero extra DB reads.
``build_behavior_instructions`` renders it as a ``<pocket-summary>``
block (description clamped, whole block hard-capped — the about-member
precedent) appended after the per-scope pocket prompts and gated off for
``intent="pocket_create"`` (mirrors the ``<current-pocket>`` tag gate).
Fixes the context-starvation bug where a chat anchored to a fully
composed template pocket had NO pocket content in its prompt, so the
agent read the empty legacy ``widgets[]`` via get_pocket and answered
"an empty shell". HOME pockets keep HOME_POCKET_PROMPT + backend_summary
byte-identical — the new block is purely additive.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only — the runtime read imports ``get_person`` lazily inside
    # ``_resolve_about_member`` so importing this module never drags in the
    # people service (and its journal accessor) for chat paths that never
    # render an about-block.
    from pocketpaw_ee.cloud.people.domain import Person

from pocketpaw.ripple import (
    HOME_POCKET_PROMPT,
    INLINE_RIPPLE_SYSTEM_PROMPT,
    POCKET_DELEGATION_RULE,
    fill_current_pocket,
    get_pocket_prompts,
)
from pocketpaw.ripple._pockets import _MCP_POCKET_BACKENDS
from pocketpaw_ee.cloud.shared.errors import CloudError, NotFound
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-stream SSE event sink
#
# Side-channel emitters (the in-process MCP pocket-write tools, the
# background session-titler) push named SSE event tuples onto whichever
# queue is bound to the current async context. The stream generator drains
# the queue between SDK events so the client receives ``pocket_mutation``
# / ``session_titled`` / etc. frames without waiting for the chat reply
# to finish. ``contextvars`` propagates the binding into tasks spawned
# via ``asyncio.create_task`` so background workers can push too.
# ---------------------------------------------------------------------------


_sse_event_sink: ContextVar[asyncio.Queue[tuple[str, dict[str, Any]]] | None] = ContextVar(
    "sse_event_sink", default=None
)


# Per-stream identity used by in-process MCP write tools that can't
# reach the FastAPI request scope. ``create_pocket`` reads these to
# stamp the ``Pocket.workspace`` / ``Pocket.owner`` fields. Set in
# ``agent_router._run_agent_stream`` and propagated into spawned tasks
# automatically via ``contextvars``.
_active_workspace_id: ContextVar[str | None] = ContextVar("agent_workspace_id", default=None)
_active_user_id: ContextVar[str | None] = ContextVar("agent_user_id", default=None)
_active_session_mongo_id: ContextVar[str | None] = ContextVar(
    "agent_session_mongo_id", default=None
)
# The Mongo ``Pocket._id`` this stream is anchored to, if any. Set alongside
# workspace/user so in-process MCP tools that act on the CURRENT pocket — the
# connector-execution server (``mcp_servers/connectors.py``) being the first —
# can resolve which room they're in without a request scope. ``None`` when the
# chat isn't bound to a pocket (a plain DM / group thread).
_active_pocket_id: ContextVar[str | None] = ContextVar("agent_pocket_id", default=None)


def attach_agent_identity(
    *,
    workspace_id: str,
    user_id: str,
    session_mongo_id: str | None = None,
    pocket_id: str | None = None,
) -> tuple[Token, Token, Token, Token]:
    """Bind workspace / user / session / pocket identity for the active
    stream's MCP tools. ``session_mongo_id`` is the ``Session._id`` the chat
    is streaming through — used by ``create_pocket`` to link the active
    session to the freshly-created pocket. ``pocket_id`` is the room the chat
    is anchored to (when any) — read by the connector-execution MCP server to
    scope ``list_connector_actions`` / ``connector_execute`` to this pocket."""
    return (
        _active_workspace_id.set(workspace_id),
        _active_user_id.set(user_id),
        _active_session_mongo_id.set(session_mongo_id),
        _active_pocket_id.set(pocket_id),
    )


def detach_agent_identity(tokens: tuple[Token, Token, Token, Token]) -> None:
    ws_token, user_token, session_token, pocket_token = tokens
    _active_workspace_id.reset(ws_token)
    _active_user_id.reset(user_token)
    _active_session_mongo_id.reset(session_token)
    _active_pocket_id.reset(pocket_token)


def current_workspace_id() -> str | None:
    return _active_workspace_id.get()


def current_user_id() -> str | None:
    return _active_user_id.get()


def current_session_mongo_id() -> str | None:
    return _active_session_mongo_id.get()


def current_pocket_id() -> str | None:
    return _active_pocket_id.get()


def push_sse_event(name: str, data: dict[str, Any]) -> None:
    """Send a named SSE event to the active stream's sink, if any.

    No-op when there's no sink in scope (e.g. invoked from a unit test or
    a CLI handler that isn't part of an SSE stream).
    """
    sink = _sse_event_sink.get()
    if sink is None:
        return
    try:
        sink.put_nowait((name, data))
    except Exception:
        logger.debug("sse sink rejected %s payload", name, exc_info=True)


def push_pocket_mutation(payload: dict[str, Any]) -> None:
    """Compatibility wrapper — historic call site for pocket-mutation pushes."""
    push_sse_event("pocket_mutation", payload)


def push_pocket_execution(payload: dict[str, Any]) -> None:
    """Push a ``pocket_execution`` SSE frame — the execution router's
    per-request observability readout (which tier ran, the stage
    timeline, total latency, token spend). Sibling of
    ``push_pocket_mutation``; emitted once per ``classify_and_route``
    call. No-op outside an SSE stream."""
    push_sse_event("pocket_execution", payload)


def attach_sse_event_sink(queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> Token:
    """Bind ``queue`` as the sink for the current async context."""
    return _sse_event_sink.set(queue)


def detach_sse_event_sink(token: Token) -> None:
    """Restore the previous sink binding."""
    _sse_event_sink.reset(token)


# Legacy aliases retained for callers that were written against the
# pocket-specific names. Both pairs operate on the same underlying sink.
attach_pocket_event_sink = attach_sse_event_sink
detach_pocket_event_sink = detach_sse_event_sink


class ScopeKind(StrEnum):
    DM = "dm"
    GROUP = "group"
    POCKET = "pocket"
    SESSION = "session"


class InvalidScope(ValueError):
    """Raised when the URL's ``scope`` path param is not one of the known kinds."""


@dataclass
class ScopeContext:
    kind: ScopeKind
    scope_id: str
    workspace_id: str
    user_id: str
    members: list[str]
    target_agent_id: str
    agent_ids_in_scope: list[str] = field(default_factory=list)
    pocket_tool_specs: list[dict[str, Any]] = field(default_factory=list)
    # The ``Session.sessionId`` that surfaces this scope+agent pair in the
    # sidebar. Populated by the router before the SSE stream begins so the
    # ``message.persisted`` / ``stream_start`` events can carry it early —
    # which lets a mid-stream refresh still find the thread in the sidebar.
    session_id: str | None = None
    # The Mongo ``Pocket._id`` this conversation is anchored to, if any.
    # Populated for ``pocket`` scope (= scope_id) and for ``session`` scope
    # when the underlying ``Session.pocket`` is set. The system prompt uses
    # it to tell the agent which pocket it can edit via the write MCP tools.
    pocket_id: str | None = None
    # The anchored pocket's free-form ``Pocket.type`` (``custom``, ``home``,
    # …), populated alongside ``pocket_id``. ``build_behavior_instructions``
    # uses it to inject ``HOME_POCKET_PROMPT`` when the chat is scoped to the
    # per-user ``type="home"`` pocket that backs the home page.
    pocket_type: str | None = None
    # Optional client-supplied intent hint that swaps which system-prompt
    # block ``build_context_block`` emits. ``pocket_create`` makes the
    # agent reach for the ``create_pocket`` MCP tool instead of rendering
    # an inline ``ui-spec`` chat reply.
    intent: str | None = None
    # Resolved per-turn surface context from
    # ``surface_context.resolve_surface_context``. ``None`` when the
    # client didn't stamp a hint or the surface module is unreachable —
    # ``build_dynamic_context`` falls back to the legacy three-line
    # shape in that case.
    surface_context: SurfaceContext | None = None
    # The ENTITY-AWARE ``SurfaceProfile`` resolved ONCE per run (entity-rooms
    # chunk ①). The run-driver (``run_core.execute_run``) resolves the base
    # profile from ``surface_context`` (the pure ``resolve_profile`` lookup),
    # then — when this chat is bound to a pocket-entity (``pocket_id`` set) and
    # that pocket carries a ``surface_profile`` override — folds the override
    # OVER the base via ``compose_entity_profile`` and stashes the result here.
    # BOTH profile consumers read THIS pre-resolved object instead of each
    # calling ``resolve_profile`` again: ``build_behavior_instructions`` (the
    # ripple-omit gate, stays sync) and ``run_core`` tool-deny / tool-allow.
    # ``None`` on the legacy / non-entity path — consumers then fall back to
    # today's behavior (ripple ON, no deny), byte-identical to before.
    resolved_profile: SurfaceProfile | None = None
    # The anchored pocket's NON-SECRET backend summary ({base_url,
    # auth_type, configured}) — the same shape ``get_pocket_backend``
    # returns, never the token. Populated by the resolvers ONLY for a
    # ``type="home"`` pocket (the home agent inlines it into the static
    # HOME_POCKET_PROMPT so it can SEE a configured backend and author a
    # ``sources`` block — mirroring how the pocket_specialist gets the
    # summary via ``get_pocket_backend``). ``None`` for non-home scopes,
    # which read the summary lazily through ``get_pocket`` when needed.
    backend_summary: dict[str, Any] | None = None
    # A concise, token-capped "about this member" block (name · role · team ·
    # one-line focus) rendered from the member's Fabric ``Person``. The
    # resolvers pre-resolve it (async, via ``_resolve_about_member``) and stash
    # it here; ``build_behavior_instructions`` APPENDS it to the base system
    # message (additive — never a persona override) so the agent greets the
    # member by name from turn one. ``None`` when the member has no Person yet
    # (a pre-existing / non-invited user, or the people read failed) — the
    # agent then behaves exactly as before, no block. Stays a pre-rendered
    # string so the sync ``build_behavior_instructions`` never has to await.
    about_member_block: str | None = None
    # The anchored pocket's orientation data: {name, description, type,
    # template_slug, pattern, ripple} where ``ripple`` is
    # ``spec_ops.summarize_ripple_spec`` over the pocket's rippleSpec
    # (top-level ui node count/types, capped state keys, source summaries,
    # action keys, legacy widgets count). Populated for ALL pocket-anchored
    # scopes by ``_pocket_summary_data`` — built from the Pocket doc the
    # resolvers already fetched, so it costs no extra DB read.
    # ``build_behavior_instructions`` renders it as the ``<pocket-summary>``
    # block so the agent knows what the pocket contains WITHOUT having to
    # call get_pocket (and without misreading the empty legacy ``widgets[]``
    # array as "this pocket is an empty shell"). ``None`` when the chat
    # isn't anchored to a pocket — no block, behavior unchanged.
    pocket_summary: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Beanie accessors (thin wrappers so tests can patch them)
# ---------------------------------------------------------------------------


async def _get_group(group_id: str) -> Any:
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.group import Group

    try:
        return await Group.get(PydanticObjectId(group_id))
    except Exception:
        return None


async def _get_pocket(pocket_id: str) -> Any:
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.pocket import Pocket

    try:
        return await Pocket.get(PydanticObjectId(pocket_id))
    except Exception:
        return None


async def _get_session(session_id: str) -> Any:
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.session import Session

    try:
        return await Session.get(PydanticObjectId(session_id))
    except Exception:
        return None


async def _home_backend_summary(
    pocket_type: str | None, workspace_id: str, pocket_id: str
) -> dict[str, Any] | None:
    """Fetch the NON-SECRET backend summary for a HOME pocket, or ``None``.

    Only the home agent inlines the summary into its static prompt (so it can
    SEE a configured backend before authoring a ``sources`` block — the same
    summary the pocket_specialist gets via ``get_pocket_backend``). Non-home
    scopes return ``None`` and never pay the read — they fetch the summary
    lazily via ``get_pocket`` when the specialist needs it. The token is
    NEVER returned; a fetch failure degrades to ``None`` so a transient
    backend-collection hiccup never blocks scope resolution."""
    if pocket_type != "home" or not workspace_id or not pocket_id:
        return None
    try:
        from pocketpaw_ee.cloud.pockets import service as _pockets_service

        summary = await _pockets_service.get_pocket_backend(workspace_id, pocket_id)
    except Exception:  # noqa: BLE001 — a backend-read failure must not block resolution
        logger.debug("home backend summary fetch failed for pocket %s", pocket_id, exc_info=True)
        return None
    return summary


def _pocket_summary_data(pocket: Any) -> dict[str, Any] | None:
    """Build the ``ScopeContext.pocket_summary`` dict from an already-fetched
    Pocket doc, or ``None``.

    Pure read over the in-hand document — NO DB round-trip. Runs for ALL
    pocket types (home included; there the summary is additive next to the
    existing backend_summary flow). A malformed doc degrades to ``None`` so
    a summary failure can never block scope resolution — the agent then
    simply behaves as before the block existed.
    """
    if pocket is None:
        return None
    try:
        # Lazy import, mirroring ``_home_backend_summary`` — chat paths that
        # never anchor to a pocket shouldn't pull the pockets package in.
        from pocketpaw_ee.cloud.pockets.spec_ops import summarize_ripple_spec

        ripple = summarize_ripple_spec(
            getattr(pocket, "rippleSpec", None),
            widgets_count=len(getattr(pocket, "widgets", None) or []),
        )
        return {
            "name": str(getattr(pocket, "name", "") or ""),
            "description": str(getattr(pocket, "description", "") or ""),
            "type": getattr(pocket, "type", None),
            "template_slug": getattr(pocket, "template_slug", None),
            "pattern": getattr(pocket, "pattern", None),
            "ripple": ripple,
        }
    except Exception:  # noqa: BLE001 — a summary failure must not block resolution
        logger.debug("pocket summary build failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# "About this member" block (agent orientation, pp#1367)
# ---------------------------------------------------------------------------

# HARD character cap on the rendered about-block. The known failure mode is
# prompt bloat — a free-text focus line (or a degenerate name) ballooning the
# always-on system prompt toward the ~20K-char tail we've been bitten by. The
# block is name + role + team + a one-line focus, so it's tiny by design; this
# is the backstop. ~4 chars/token ⇒ 1500 chars ≈ ~375 tokens, under the
# ~400-token budget. We truncate the WHOLE rendered block (not just focus) so
# no combination of fields can exceed it.
_ABOUT_MEMBER_CHAR_CAP = 1500
# The focus line is the only unbounded, member/admin-authored field, so it gets
# its own tighter per-field clamp before assembly — keeps the block readable
# instead of letting one long sentence eat the whole budget.
_ABOUT_MEMBER_FOCUS_CHAR_CAP = 280


def _render_about_member_block(person: Person) -> str:
    """Render the concise, token-capped "about this member" block.

    Shape: an ``<about-member>`` tag carrying ``name · role · team`` and an
    optional one-line ``focus``. Only fields the Person actually has are
    emitted (no empty ``team:`` / ``focus:`` lines). The whole block is HARD
    truncated at ``_ABOUT_MEMBER_CHAR_CAP`` so it can never bloat the system
    prompt, regardless of field contents.

    Returns an empty string when the Person carries no usable identity (no
    name) — the caller treats that the same as "no Person": no block.
    """

    name = (person.name or "").strip()
    if not name:
        # Nothing to orient the agent with — skip the block entirely rather
        # than emit a nameless "you are talking to ." line.
        return ""

    role = (person.role or "").strip()
    team = (person.group or "").strip()
    focus = " ".join((person.focus or "").split())  # collapse whitespace/newlines
    if len(focus) > _ABOUT_MEMBER_FOCUS_CHAR_CAP:
        focus = focus[:_ABOUT_MEMBER_FOCUS_CHAR_CAP].rstrip() + "…"

    # Identity line: name, then role/team as available, joined with " · ".
    bits = [name]
    if role:
        bits.append(role)
    if team:
        bits.append(f"team {team}")
    identity = " · ".join(bits)

    lines = [
        "<about-member>",
        "You are talking to a member of this workspace. Greet them by name and",
        "tailor your help to their role and focus.",
        f"  who: {identity}",
    ]
    if focus:
        lines.append(f"  focus: {focus}")
    lines.append("</about-member>")
    block = "\n".join(lines)

    if len(block) > _ABOUT_MEMBER_CHAR_CAP:
        # Backstop hard cap. Keep the closing tag readable by appending it
        # after the truncation so the block stays well-formed.
        block = block[:_ABOUT_MEMBER_CHAR_CAP].rstrip() + "…\n</about-member>"
    return block


async def _resolve_about_member(workspace_id: str, user_id: str) -> str | None:
    """Fetch the member's Fabric ``Person`` and render the about-block, or ``None``.

    Pre-resolved (async) by every scope resolver and stashed on
    ``ScopeContext.about_member_block`` so the sync
    ``build_behavior_instructions`` can append it without awaiting. Returns
    ``None`` — meaning "no block, behave as today" — when:

    * the member has no materialized Person (a pre-existing / non-invited user);
    * the Person carries no usable name (render returns "");
    * the people read raised (degrades gracefully — a Fabric hiccup must never
      block scope resolution or change the agent's behavior).
    """

    if not workspace_id or not user_id:
        return None
    try:
        from pocketpaw_ee.cloud.people.service import get_person

        person = await get_person(workspace_id, user_id)
    except Exception:  # noqa: BLE001 — a people-read failure must not block resolution
        logger.debug(
            "about-member person read failed for %s/%s", workspace_id, user_id, exc_info=True
        )
        return None
    if person is None:
        return None
    block = _render_about_member_block(person)
    return block or None


# ---------------------------------------------------------------------------
# "<pocket-summary>" block (agent orientation, fix/pocket-anchored-chat-context)
# ---------------------------------------------------------------------------

# Same prompt-bloat discipline as the about-member block: the description is
# the only unbounded user-authored field, so it gets a per-field clamp; the
# whole rendered block gets a hard backstop cap. ~4 chars/token ⇒ 2000 chars
# ≈ ~500 tokens for a fully composed pocket — cheap orientation that saves
# the get_pocket round-trip (and the "widgets: 0 ⇒ empty shell" misread).
_POCKET_SUMMARY_DESC_CHAR_CAP = 280
_POCKET_SUMMARY_CHAR_CAP = 2000


def _render_pocket_summary_block(summary: dict[str, Any]) -> str:
    """Render ``ScopeContext.pocket_summary`` as the ``<pocket-summary>``
    system-prompt block.

    Sync and pure — the data was resolved (and the ripple summary computed)
    in the scope resolvers, mirroring the about-member flow. Only lines with
    content are emitted. Ends with the one-line get_pocket hint so the agent
    knows where the full spec lives.
    """
    name = " ".join(str(summary.get("name") or "").split())
    desc = " ".join(str(summary.get("description") or "").split())
    if len(desc) > _POCKET_SUMMARY_DESC_CHAR_CAP:
        desc = desc[:_POCKET_SUMMARY_DESC_CHAR_CAP].rstrip() + "…"
    ripple = summary.get("ripple") or {}

    lines = [
        "<pocket-summary>",
        "This chat is anchored to the pocket below. Orient from this summary",
        "when asked what the pocket is or contains.",
        f"  name: {name or '(unnamed)'}",
    ]
    if desc:
        lines.append(f"  description: {desc}")
    meta_bits = []
    if summary.get("type"):
        lines.append(f"  type: {summary['type']}")
    if summary.get("template_slug"):
        meta_bits.append(f"template: {summary['template_slug']}")
    if summary.get("pattern"):
        meta_bits.append(f"pattern: {summary['pattern']}")
    if meta_bits:
        lines.append("  " + " · ".join(meta_bits))

    if ripple.get("has_ripple_spec"):
        types = ", ".join(ripple.get("ui_node_types") or []) or "(untyped)"
        lines.append(
            f"  layout: rippleSpec.ui — {ripple.get('ui_node_count', 0)} top-level node(s): {types}"
        )
        state_keys = ripple.get("state_keys") or []
        if state_keys:
            omitted = ripple.get("state_keys_omitted") or 0
            suffix = f" (+{omitted} more)" if omitted else ""
            lines.append(
                f"  state keys ({len(state_keys) + omitted}): {', '.join(state_keys)}{suffix}"
            )
        sources = ripple.get("sources") or []
        if sources:
            rendered = "; ".join(
                f"{s.get('key')} — {s.get('method') or '?'} {s.get('path') or '?'}"
                f" → {s.get('bind') or '?'}"
                for s in sources
            )
            lines.append(f"  sources ({len(sources)}): {rendered}")
        action_keys = ripple.get("action_keys") or []
        if action_keys:
            lines.append(f"  actions ({len(action_keys)}): {', '.join(action_keys)}")
        # The misread this block exists to kill: an empty top-level widgets[]
        # does NOT mean the pocket is empty — the real layout is the spec.
        lines.append(
            f"  widgets[]: {ripple.get('widgets_count', 0)} entries — this is a "
            "legacy array; the real layout lives in rippleSpec.ui above"
        )
    else:
        lines.append(
            f"  layout: no rippleSpec — {ripple.get('widgets_count', 0)} legacy widgets[] entries"
        )
    lines.append("Call get_pocket for the full rippleSpec, state, and sources.")
    lines.append("</pocket-summary>")
    block = "\n".join(lines)

    if len(block) > _POCKET_SUMMARY_CHAR_CAP:
        # Backstop hard cap — keep the block well-formed by re-closing it.
        block = block[:_POCKET_SUMMARY_CHAR_CAP].rstrip() + "…\n</pocket-summary>"
    return block


async def _get_default_workspace_agent_id(workspace_id: str) -> str | None:
    """Resolve the workspace's default ``pocketpaw`` agent id, or ``None``.

    Mirrors the slug used by ``seed_default_agent`` in ``auth/core.py``. Pockets
    that haven't had an agent explicitly attached still chat against this
    workspace-default agent.
    """
    if not workspace_id:
        return None
    try:
        from pocketpaw_ee.cloud.models.agent import Agent

        agent = await Agent.find_one(Agent.workspace == workspace_id, Agent.slug == "pocketpaw")
        return str(agent.id) if agent is not None else None
    except Exception:
        logger.exception("default workspace agent lookup failed for ws=%s", workspace_id)
        return None


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


async def resolve_scope_context(
    *, scope: str, scope_id: str, user_id: str, agent_id_hint: str | None
) -> ScopeContext:
    """Resolve a ``ScopeContext`` for a cloud agent chat request.

    Raises:
        InvalidScope: ``scope`` is not one of dm/group/pocket/session.
        NotFound: the group, pocket, or session doesn't exist.
        CloudError: caller is not a member, no agent is in scope, or the
            caller must disambiguate ``agent_id`` for a multi-agent group.
    """
    try:
        kind = ScopeKind(scope)
    except ValueError as e:
        raise InvalidScope(scope) from e

    if kind is ScopeKind.POCKET:
        return await _resolve_pocket(scope_id, user_id, agent_id_hint)
    if kind is ScopeKind.SESSION:
        return await _resolve_session(scope_id, user_id, agent_id_hint)
    return await _resolve_group_like(kind, scope_id, user_id, agent_id_hint)


async def _resolve_session(scope_id: str, user_id: str, agent_id_hint: str | None) -> ScopeContext:
    session = await _get_session(scope_id)
    if session is None or getattr(session, "deleted_at", None) is not None:
        raise NotFound("session", scope_id)

    if getattr(session, "owner", None) != user_id:
        raise CloudError(403, "session.forbidden", "Caller does not own this session")

    # When the session lives inside a pocket, hydrate the pocket's tool specs
    # so a chat routed through ``session`` scope still gets the pocket-scoped
    # tools the agent would see under ``pocket`` scope. The frontend prefers
    # session scope for pocket chats so the active session id is honored
    # (pocket scope keys all sessions under one stream); without this lookup
    # those chats would silently lose pocket tools.
    pocket_tool_specs: list[dict[str, Any]] = []
    pocket_type: str | None = None
    backend_summary: dict[str, Any] | None = None
    pocket_summary: dict[str, Any] | None = None
    pocket_id = getattr(session, "pocket", None)
    if pocket_id:
        pocket = await _get_pocket(str(pocket_id))
        if pocket is not None:
            pocket_tool_specs = list(getattr(pocket, "tool_specs", []) or [])
            pocket_type = getattr(pocket, "type", None)
            # The home page commonly chats through session scope (so the
            # active session id is honored). Surface the home pocket's
            # backend summary here too so the home agent inlines it whether
            # the chat routed through pocket or session scope.
            backend_summary = await _home_backend_summary(
                pocket_type, str(getattr(session, "workspace", "")), str(pocket_id)
            )
            # Pocket orientation for ALL pocket types — built from the doc
            # we already fetched, no extra read. Mirrors the pocket-scope
            # path so the agent sees the same <pocket-summary> block
            # whichever scope the frontend routed the chat through.
            pocket_summary = _pocket_summary_data(pocket)

    workspace_id = str(getattr(session, "workspace", ""))
    target = agent_id_hint or getattr(session, "agent", None)
    if not target:
        # Sessions created via ``createPocketSession`` don't yet pin an agent
        # — fall back to the workspace's default ``pocketpaw`` agent (same
        # rule ``_resolve_pocket`` applies). Keeps cold-start chats in a
        # newly-created pocket session working without the caller having to
        # explicitly pass ``agent_id``.
        target = await _get_default_workspace_agent_id(workspace_id)
        if not target:
            raise CloudError(400, "session.no_agent", "Session has no agent")

    # Orient the agent to who's chatting (pp#1367) — pre-render the capped
    # about-block here (async) so the sync system-message assembly can append
    # it. ``None`` when the member has no Person → no block, behavior unchanged.
    about_member_block = await _resolve_about_member(workspace_id, user_id)

    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id=scope_id,
        workspace_id=workspace_id,
        user_id=user_id,
        members=[user_id],
        target_agent_id=target,
        agent_ids_in_scope=[target],
        pocket_tool_specs=pocket_tool_specs,
        pocket_id=str(pocket_id) if pocket_id else None,
        pocket_type=pocket_type,
        backend_summary=backend_summary,
        about_member_block=about_member_block,
        pocket_summary=pocket_summary,
    )


async def _resolve_group_like(
    kind: ScopeKind, scope_id: str, user_id: str, agent_id_hint: str | None
) -> ScopeContext:
    group = await _get_group(scope_id)
    if group is None:
        raise NotFound("group", scope_id)
    if getattr(group, "archived", False):
        raise CloudError(409, "group.archived", "Group is archived")
    members = list(getattr(group, "members", []) or [])
    if user_id not in members:
        raise CloudError(403, "group.not_member", "Caller is not a group member")

    # DM kind must actually be a dm on the document, and vice versa — prevents
    # a caller from driving a normal group through the /dm/ route to bypass
    # multi-agent disambiguation.
    if kind is ScopeKind.DM and getattr(group, "type", "") != "dm":
        raise CloudError(400, "scope.mismatch", "Group is not a DM")
    if kind is ScopeKind.GROUP and getattr(group, "type", "") == "dm":
        raise CloudError(400, "scope.mismatch", "DM must use /dm/ scope")

    agents = list(getattr(group, "agents", []) or [])
    agent_ids = [getattr(a, "agent", None) for a in agents if getattr(a, "agent", None)]
    if not agent_ids:
        raise CloudError(400, "group.no_agent", "No agent in scope")

    target = _pick_target_agent(agent_ids, agent_id_hint)

    workspace_id = str(getattr(group, "workspace", ""))
    # Orient the agent to the calling member (pp#1367) — pre-rendered capped
    # block, ``None`` when the member has no Person.
    about_member_block = await _resolve_about_member(workspace_id, user_id)

    return ScopeContext(
        kind=kind,
        scope_id=scope_id,
        workspace_id=workspace_id,
        user_id=user_id,
        members=members,
        target_agent_id=target,
        agent_ids_in_scope=agent_ids,
        about_member_block=about_member_block,
    )


async def _resolve_pocket(scope_id: str, user_id: str, agent_id_hint: str | None) -> ScopeContext:
    pocket = await _get_pocket(scope_id)
    if pocket is None:
        raise NotFound("pocket", scope_id)

    team = list(getattr(pocket, "team", []) or [])
    shared = list(getattr(pocket, "shared_with", []) or [])
    owner = getattr(pocket, "owner", None)
    visibility = getattr(pocket, "visibility", "workspace")
    is_member = user_id == owner or user_id in team or user_id in shared
    if visibility == "private" and not is_member:
        raise CloudError(403, "pocket.forbidden", "No access to pocket")
    # For workspace/public we still require the caller be a workspace member;
    # the route-level dependency ``current_workspace_id`` already enforced that.

    workspace_id = str(getattr(pocket, "workspace", ""))

    agents = list(getattr(pocket, "agents", []) or [])
    agent_ids = [a if isinstance(a, str) else getattr(a, "id", None) for a in agents]
    agent_ids = [a for a in agent_ids if a]
    if not agent_ids:
        # Pockets don't have to declare their own agents — fall back to the
        # workspace's default ``pocketpaw`` agent (seeded per workspace at
        # provision time) so chats work before any explicit agent is attached.
        default_id = await _get_default_workspace_agent_id(workspace_id)
        if not default_id:
            raise CloudError(400, "pocket.no_agent", "Pocket has no agent")
        agent_ids = [default_id]

    # Pockets default to the first listed agent when no hint is given (unlike
    # groups, which require explicit disambiguation for multi-agent scopes).
    if agent_id_hint is not None:
        if agent_id_hint not in agent_ids:
            raise CloudError(400, "agent.not_in_scope", "agent_id not in scope")
        target = agent_id_hint
    else:
        target = agent_ids[0]

    # Build the participant list: owner first, then team, then shared-with,
    # deduped. Pocket.owner is a required field on the model, so the falsy
    # branch is defensive only. Note: Pocket has no ``archived`` field today,
    # so there's no archived check here (intentional, not a parity gap with
    # the group path).
    seen: set[str] = set()
    members: list[str] = []
    for m in [owner, *team, *shared]:
        if m is None or m in seen:
            continue
        seen.add(m)
        members.append(m)

    pocket_type = getattr(pocket, "type", None)
    backend_summary = await _home_backend_summary(pocket_type, workspace_id, scope_id)
    # Pocket orientation for ALL pocket types (fix/pocket-anchored-chat-
    # context) — built from the doc we already fetched, no extra read.
    pocket_summary = _pocket_summary_data(pocket)
    # Orient the agent to the calling member (pp#1367) — pre-rendered capped
    # block, ``None`` when the member has no Person.
    about_member_block = await _resolve_about_member(workspace_id, user_id)

    return ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id=scope_id,
        workspace_id=workspace_id,
        user_id=user_id,
        members=members,
        target_agent_id=target,
        agent_ids_in_scope=agent_ids,
        pocket_tool_specs=list(getattr(pocket, "tool_specs", []) or []),
        pocket_id=scope_id,
        pocket_type=pocket_type,
        backend_summary=backend_summary,
        about_member_block=about_member_block,
        pocket_summary=pocket_summary,
    )


def _pick_target_agent(agent_ids: list[str], hint: str | None) -> str:
    if hint is not None:
        if hint not in agent_ids:
            raise CloudError(400, "agent.not_in_scope", "agent_id not in scope")
        return hint
    if len(agent_ids) == 1:
        return agent_ids[0]
    raise CloudError(
        400,
        "agent.ambiguous",
        "Multiple agents in scope — pass agent_id",
    )


# ---------------------------------------------------------------------------
# Toolset assembly
# ---------------------------------------------------------------------------


def _tool_identity(spec: dict[str, Any]) -> tuple:
    """Stable tuple for deduping tool specs of different kinds."""
    kind = spec.get("kind", "")
    if kind == "builtin":
        return ("builtin", spec.get("id", ""))
    if kind == "mcp":
        return ("mcp", spec.get("server", ""), spec.get("name", ""))
    if kind == "inline":
        return ("inline", spec.get("name", ""))
    return (kind, repr(sorted(spec.items())))


def assemble_toolset(ctx: ScopeContext, *, base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge base + pocket-scoped tools. Dedupes by identity, base wins.

    Pocket tools come along whenever ``ctx.pocket_tool_specs`` is populated,
    not just under ``pocket`` scope — sessions that live inside a pocket
    (resolved via ``_resolve_session``) carry the same specs so the agent
    sees the same toolset whether the chat was routed through pocket or
    session scope.
    """
    if not ctx.pocket_tool_specs:
        return list(base)
    seen: set[tuple] = {_tool_identity(t) for t in base}
    merged = list(base)
    for spec in ctx.pocket_tool_specs:
        ident = _tool_identity(spec)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(spec)
    return merged


# ---------------------------------------------------------------------------
# Context block for system prompt
# ---------------------------------------------------------------------------


def build_behavior_instructions(ctx: ScopeContext, *, backend_name: str | None = None) -> str:
    """Return the STATIC behavioral rules for this scope/backend.

    These are direct authoritative instructions the model must follow —
    ripple UI conventions, pocket delegation rule, etc. They are
    intentionally separated from ``build_dynamic_context`` so the caller
    can inject them as top-level ``instructions`` to the agent backend
    (where they read as rules) rather than burying them inside the
    ``knowledge_context`` wrapper (where they read as reference data and
    the model often ignores them).

    Backend gating mirrors ``build_context_block``: MCP-capable backends
    get ``INLINE_RIPPLE_SYSTEM_PROMPT + POCKET_DELEGATION_RULE``;
    others get the heavy inline pocket prompt. A ``type="home"`` scope is
    the exception — it gets ``INLINE_RIPPLE_SYSTEM_PROMPT +
    HOME_POCKET_PROMPT`` and NOT the delegation rule, because the home
    agent mutates widgets directly via the ``add_widget`` MCP tool rather
    than delegating to the pocket specialist.

    Surface gating ("ripple-default bias" fix): the resolved per-request
    ``SurfaceProfile`` decides whether the ripple LAW applies at all. On the
    /sites surface the agent hand-authors a Svelte Paw Site, so its profile
    has ``ripple_mode="off"`` and we OMIT INLINE_RIPPLE_SYSTEM_PROMPT,
    POCKET_DELEGATION_RULE, and the inline ripple-creation prompt — otherwise
    the "default to ui-spec" LAW biases the agent toward emitting a ripple
    ui-spec instead of Svelte. Every other surface (and the legacy
    ``resolved_profile is None`` path) keeps ``ripple_mode="on"`` — unchanged.
    The profile is the single source of truth; we never branch on a bare
    ``kind == SITES`` check here.

    Entity-rooms (chunk ①): we read the PRE-RESOLVED ``ctx.resolved_profile``
    rather than calling ``resolve_profile`` ourselves. The run-driver resolves
    it ONCE (entity-aware — a pocket-entity's ``surface_profile.ripple_mode``
    override composes over the surface base), so a pocket bound to a room can
    flip ripple off/on for that room and this gate honors it. ``None`` is the
    legacy path → ripple ON, exactly today's behavior. This stays SYNC — it only
    reads the already-resolved object.
    """
    # Read the once-per-run resolved profile. ``resolved_profile is None`` is
    # the legacy / non-entity path — default to ripple ON (do NOT omit) so
    # surface-less clients keep today's behavior exactly. Only an explicit
    # ``ripple_mode="off"`` profile (the /sites row, OR a pocket-entity that set
    # ``surface_profile.ripple_mode="off"``) suppresses the ripple block.
    ripple_off = ctx.resolved_profile is not None and ctx.resolved_profile.ripple_mode == "off"

    parts: list[str] = []
    parts.append(_RUNTIME_IDENTITY_RULE)
    # Composio auth/search guidance is injected whenever Composio is
    # enabled. An enabled deployment ALWAYS surfaces at least the
    # discovery meta-tools — ``providers.py`` falls back to them when no
    # toolkit is allow-listed — and the search-fallback rule matters MOST
    # in that meta-tools-only mode. So gate on credentials (is_enabled),
    # not on the toolkit allow-list: the prompt and the real tool list
    # agree because enabled ⇒ tools present.
    from pocketpaw_ee.cloud.composio import service as _composio_service

    if _composio_service.is_enabled():
        parts.append(_COMPOSIO_AUTH_FLOW_RULE)
        parts.append(_COMPOSIO_SEARCH_FALLBACK_RULE)
    # The home pocket is a special case: its agent mutates widgets directly
    # via the ``add_widget`` MCP tool — it does NOT delegate to the pocket
    # specialist. ``POCKET_DELEGATION_RULE`` ("never call add_widget,
    # delegate to the specialist") and ``HOME_POCKET_PROMPT`` ("call
    # add_widget") flatly contradict each other, so the delegation rule is
    # dropped for a ``type="home"`` scope. The home agent then gets exactly
    # one consistent widget-creation instruction.
    is_home = ctx.pocket_type == "home"
    if is_home:
        # The home agent's only widget-creation instruction is
        # HOME_POCKET_PROMPT (appended below). It must not also receive the
        # specialist-delegation rule (MCP backends) or the heavy
        # interaction prompt (CLI backends) — both carry the contradicting
        # "delegate, don't call add_widget" framing. Just the base ripple
        # conventions, then HOME_POCKET_PROMPT. (No /sites home pocket exists,
        # so the surface gate doesn't apply here.)
        parts.append(INLINE_RIPPLE_SYSTEM_PROMPT)
    elif backend_name in _MCP_POCKET_BACKENDS:
        # Surface gate: omit the ripple LAW + delegation rule on a
        # ripple-off surface (/sites). The agent there hand-authors Svelte —
        # the "default to ui-spec" LAW would bias it away from that.
        if not ripple_off:
            parts.append(INLINE_RIPPLE_SYSTEM_PROMPT)
            parts.append(POCKET_DELEGATION_RULE)
    else:
        creation_prompt, interaction_prompt = get_pocket_prompts(backend_name=backend_name)
        if ctx.intent == "pocket_create":
            # The creation prompt is the ripple-authoring LAW for CLI
            # backends — omit it on a ripple-off surface for the same reason.
            if not ripple_off:
                parts.append(creation_prompt)
        elif ctx.pocket_id:
            # build_behavior_instructions is sync — it cannot await the
            # backend-summary read. The main chat agent delegates edits
            # to the specialist anyway, so pass None: the prompt renders
            # "configured state unknown — call get_pocket to check",
            # and get_pocket now carries the real backend summary.
            parts.append(fill_current_pocket(interaction_prompt, ctx.pocket_id, None))
        elif not ripple_off:
            parts.append(INLINE_RIPPLE_SYSTEM_PROMPT)
    # Home surface: append the home-surface prompt so the agent calls
    # add_widget for an explicit widget request and answers directly
    # otherwise. Backend-agnostic — the discriminator is the pocket type.
    # HOME_POCKET_PROMPT carries the __BACKEND_SUMMARY__ token: fill it with
    # the resolved non-secret summary so the home agent can SEE whether a
    # backend is configured (and its base_url) before it authors a sources
    # block — fixing the smoke-test finding where the agent claimed "no
    # integration wired up" despite a configured backend. ``None`` renders as
    # "configured state unknown — call get_pocket to check". The literal
    # token never leaks because ``fill_current_pocket`` always replaces it.
    if is_home:
        parts.append(
            fill_current_pocket(HOME_POCKET_PROMPT, ctx.pocket_id or "", ctx.backend_summary)
        )
    # ADDITIVE pocket orientation (fix/pocket-anchored-chat-context): every
    # pocket-anchored scope — home included — gets a <pocket-summary> block
    # (name, description, template/pattern, ui node types, state keys,
    # sources, legacy widgets count) so the agent can answer "what's this
    # pocket about?" without misreading the empty legacy widgets[] array as
    # "an empty shell". Resolved on the ScopeContext from the already-fetched
    # Pocket doc; rendered sync here (the about-member pattern). Gated off
    # for intent="pocket_create" — that flow is about a NEW pocket, so the
    # anchor's summary would mislead (mirrors the <current-pocket> tag gate
    # in build_dynamic_context). ``None`` ⇒ no block, byte-identical prompt.
    if ctx.pocket_summary and ctx.intent != "pocket_create":
        parts.append(_render_pocket_summary_block(ctx.pocket_summary))
    # ADDITIVE member orientation (pp#1367): append the pre-rendered, capped
    # "about this member" block LAST so the agent greets the caller by name and
    # tailors to their role/focus from turn one. It is pre-resolved on the
    # ScopeContext (async, in the resolver) and APPENDED here — never injected
    # via ``system_message_override`` (that field REPLACES the base persona).
    # ``None`` for a member with no Person (pre-existing / non-invited user) ⇒
    # nothing appended ⇒ behavior identical to before. The string is already
    # hard-capped at render time, so this can't bloat the prompt.
    if ctx.about_member_block:
        parts.append(ctx.about_member_block)
    return "\n".join(parts)


# Authoritative runtime-identity rule. Models trained on Claude Code
# (and other CLI agents) frequently hallucinate environment-specific
# guidance — telling users to "run /mcp" to authenticate an integration,
# referencing tools by their Claude.ai-hosted names ("claude.ai Gmail"),
# suggesting `/help`, `/clear`, and other slash commands. None of that
# exists in the PocketPaw chat surface. When a tool is missing the
# correct behavior is to call the tools that DO exist (e.g. Composio's
# meta-tools or concrete GMAIL_* tools), not to invent a Claude Code
# command for the user to run.
_RUNTIME_IDENTITY_RULE = """\
<runtime-identity>
You are PocketPaw — an AI assistant embedded in the paw-enterprise chat
interface. You are NOT Claude Code, NOT the Claude.ai web UI, NOT a CLI
agent, and NOT inside the paw-enterprise Settings/admin panel. The user
is in a graphical chat with you over a web/desktop surface.

The ONLY integration path available to you for third-party services
(Gmail, Slack, GitHub, Calendar, Drive, Linear, …) is the Composio
tools. Every other integration affordance you may have seen in training
DOES NOT EXIST in this environment:

- Slash commands DO NOT EXIST here. Never tell the user to run "/mcp",
  "/help", "/clear", "/login", "/auth", or any other slash-prefixed
  command. They have no way to type or execute these.
- "claude.ai Gmail", "claude.ai Google Calendar", and similar
  Anthropic-hosted MCP names DO NOT exist here.
- There is NO "Settings → Google OAuth", "Settings → Integrations", or
  any other Settings-panel OAuth flow you can point the user at. Do NOT
  fabricate instructions like "go to Settings → Google OAuth → Authorize
  Gmail". The user authorizes integrations through Composio's Connect
  Links, which YOU obtain by calling the relevant tool.
- For ANY Gmail/Slack/Calendar/Drive/etc. operation, use the
  Composio-prefixed tools you have (e.g. ``GMAIL_FETCH_EMAILS``,
  ``GMAIL_SEND_EMAIL``, ``SLACK_SEND_MESSAGE``, ``GOOGLECALENDAR_*``).
  When a tool returns a "needs auth / Connect Link" response, pass that
  URL to the user verbatim — do NOT translate it into Settings-panel
  instructions.
- If you genuinely don't have a tool for what the user asked, say so
  plainly. Don't fabricate instructions for a different environment.
</runtime-identity>"""


# Composio's direct-tools surface caps each toolkit at a fixed limit
# (50 actions/toolkit by default in ``pocketpaw_ee.cloud.composio.providers``)
# and paginates alphabetically. For big toolkits (github has 50+ actions),
# a specific action the user asked about may not be in your tool list
# even though Composio supports it. The 3 meta-tools below give you a
# discovery fallback that asks Composio's own search index, which is
# more reliable than the LLM-side tool-list lookup.
_COMPOSIO_AUTH_FLOW_RULE = """\
<composio-auth-flow>
When a Composio tool returns "needs connection" / ``ConnectedAccountNotFound``
/ any "not authorized" error, the auth sequence is:

  1. Call ``initiate_connection(toolkit="<slug>")``. It returns a
     ``redirect_url``. Surface that URL to the user EXACTLY as you got
     it — do NOT translate it to "go to Settings" instructions; those
     do not exist here.
  2. After the user opens the URL, authorizes, and returns to chat,
     call ``verify_connection(toolkit="<slug>")``. This probes the
     toolkit's "who am I" action and returns the external identity
     they connected as (GitHub login, Gmail address, etc.).
  3. Surface the verified identity to the user verbatim:
     "Connected as <external_identity>. Continue?". DO NOT retry the
     original tool until the user confirms.
  4. If ``verify_connection`` returns ``status: "mismatch"``, the user
     re-authorized as a DIFFERENT account than the one previously
     stored. Show both identities, ask which one they want, and do
     NOT retry the original tool until the user confirms the change.
  5. If ``verify_connection`` returns ``status: "unverified"``, the
     toolkit doesn't expose a probe — surface "Connected to <toolkit>
     (identity verification unavailable)" and proceed cautiously.

Never skip step 2. Without it, the agent silently operates as whatever
account the user picked, which can be the wrong one (personal Gmail
instead of work, shared mailbox instead of personal).
</composio-auth-flow>"""


_COMPOSIO_SEARCH_FALLBACK_RULE = """\
<composio-search-fallback>
You have access to three Composio meta-tools for discovering actions
that aren't loaded directly into your tool list:

  COMPOSIO_SEARCH_TOOLS(query)  — keyword search across all Composio
                                  actions you're permitted to use.
                                  Returns matching tool names.
  COMPOSIO_GET_TOOL_SCHEMAS([tool_names])
                                — fetch the input schemas for the
                                  tool names you picked.
  COMPOSIO_MULTI_EXECUTE_TOOL(...)
                                — execute one or more discovered tools
                                  with their resolved arguments.

Use these ONLY as a fallback. If the action you need is already in
your direct tool list (e.g. ``GMAIL_FETCH_EMAILS``, ``GITHUB_LIST_ISSUES_FOR_REPOSITORY``),
call it directly — don't round-trip through search. When you DO need
search, the sequence is: SEARCH → pick a name → GET_SCHEMAS → EXECUTE.
</composio-search-fallback>"""


def build_dynamic_context(ctx: ScopeContext) -> str:
    """Return only the per-turn dynamic context tags — scope,
    participants, current-pocket-id. Pairs with
    ``build_behavior_instructions``: the dynamic context is reference
    data and lives inside the ``knowledge_context`` wrapper; the
    behavioral instructions live at the top level.

    When a ``surface_context`` is attached, its preamble is prepended
    FIRST — surface state (pinned widgets, snapshot, available tools)
    is more informationally dense than the bare scope tags and the
    agent should see it before anything else. ``surface_context is None``
    keeps the legacy three-line shape (clients that don't stamp a
    surface hint, or surfaces that fell back to GENERIC with an empty
    preamble).
    """
    parts: list[str] = []
    if ctx.surface_context and ctx.surface_context.preamble:
        parts.append(ctx.surface_context.preamble)
    member_list = ", ".join(ctx.members) if ctx.members else "(none)"
    parts.append(f"<scope>{ctx.kind.value} {ctx.scope_id}</scope>")
    parts.append(f"<participants>{member_list}</participants>")
    if ctx.pocket_id and ctx.intent != "pocket_create":
        parts.append(f'<current-pocket id="{ctx.pocket_id}" />')
    return "\n".join(parts)


def build_context_block(ctx: ScopeContext, *, backend_name: str | None = None) -> str:
    """Compact string the agent prompt embeds so the model knows who is
    here and how to render rich UI back to the client.

    ORDER MATTERS: the static ripple/pocket prompt content goes FIRST
    so Anthropic prompt caching can hit on it; per-turn dynamic tags
    (scope, participants, current pocket id) go LAST.

    Combined ``build_behavior_instructions`` + ``build_dynamic_context``.
    Kept for callers that want the full assembled block (tests, legacy
    pre-Phase-3 fallback paths). The cloud chat router now uses the two
    helpers separately so behavioral rules can be hoisted out of the
    ``knowledge_context`` framing — see comments on the helpers.

    Backend gating: claude_agent_sdk supports the pocket_specialist
    subagent, so the main chat agent ships only INLINE_RIPPLE_SYSTEM_PROMPT
    + POCKET_DELEGATION_RULE — heavy POCKET_*_PROMPT_MCP text lives on
    the specialist. Other backends (codex_cli, opencode, openai_agents,
    google_adk, deep_agents, copilot_sdk) don't have a native subagent
    integration today, so they fall back to the pre-Phase-3 path:
    full pocket prompt inline. Universal Option-A (MCP-based specialist)
    is the planned follow-up.
    """
    behavior = build_behavior_instructions(ctx, backend_name=backend_name)
    dynamic = build_dynamic_context(ctx)
    return f"{behavior}\n{dynamic}" if behavior else dynamic


_FILE_MENTION_TYPES = {"file", "upload", "attachment", "document", "image"}


def _file_reference_terms(
    *,
    attachments: list[dict[str, Any]] | None,
    mentions: list[dict[str, Any]] | None,
) -> list[str]:
    """Collect filename-like terms to steer KB retrieval for upload mentions."""
    terms: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(text)

    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        _add(att.get("name"))
        _add(att.get("filename"))
        _add(att.get("url"))
        meta = att.get("meta")
        if isinstance(meta, dict):
            _add(meta.get("file_id"))
            _add(meta.get("upload_id"))

    for mention in mentions or []:
        if not isinstance(mention, dict):
            continue
        mtype = str(mention.get("type") or "").strip().lower()
        if mtype and mtype not in _FILE_MENTION_TYPES:
            continue
        _add(mention.get("display_name"))
        _add(mention.get("name"))
        _add(mention.get("id"))
        _add(mention.get("url"))

    return terms


def _member_private_user_scope(ctx: ScopeContext) -> str | None:
    """The session-user isolation gate (VIP Onboarding Phase B).

    Returns the member-private ``user:{member_id}`` KB scope ONLY when this
    chat is the member's OWN solo context, and ``None`` otherwise. The single
    airtight rule:

        emit ``user:{ctx.user_id}``  ⟺  ctx.members == [ctx.user_id]

    i.e. exactly one member AND it is the authenticated session principal.
    This is the tightest possible test and it closes every leak path:

    * a shared / multi-member room (``len(members) > 1``) → ``None``. A
      member's private Gmail/calendar KB is NEVER injected where another
      member can see the agent's context.
    * a room whose sole member is NOT the caller (stale membership, a route
      that resolved a different principal) → ``None``. We never emit a
      ``user:`` scope for anyone but the proven-solo authenticated member.
    * a member's own solo SESSION (``members == [user_id]``, the shape
      ``_resolve_session`` always builds) or a private solo POCKET/DM →
      ``user:{user_id}``.

    ``ctx.user_id`` is the authenticated cloud user id (opaque Mongo
    ObjectId / uuid), so it is safe to use verbatim as a kb-go scope id —
    kb-go's on-disk ``:``→``_`` sanitize can't alias two opaque ids the way
    it could alias two emails.
    """
    uid = ctx.user_id
    if not uid:
        return None
    if list(ctx.members) != [uid]:
        return None
    return f"user:{uid}"


# Hard cap on the "your day" briefing block. The block shares the
# system-prompt budget, so it must never grow unbounded with a busy day.
# ~400 tokens ≈ 1600 chars (English ≈ 4 chars/token); we cap on chars (a
# cheap, deterministic proxy — no tokenizer dependency) and truncate with an
# ellipsis if a rendered block would exceed it.
_BRIEFING_MAX_CHARS = 1600


async def _member_briefing_block(
    ctx: ScopeContext,
    *,
    digest_fn: Callable[[str, str], Awaitable[Any]] | None = None,
) -> str:
    """Return the concise, capped "your day" briefing for the member's OWN
    solo session, or ``""`` when it must be absent.

    GATED EXACTLY like the member-private ``user:`` KB scope: we reuse
    ``_member_private_user_scope`` as the single source of truth for the
    "is this the member's solo session?" decision. When it returns ``None``
    (a shared / multi-member room, or a room whose sole member is not the
    authenticated principal) we emit NOTHING and never even pull the digest —
    so one member's mail/calendar is never read or surfaced in a context
    another member can see. This is the same airtight rule that keeps the
    private KB scope out of shared rooms.

    The block is built from the structured ``MemberDayDigest`` (the per-member
    live pull, keyed on ``ctx.user_id`` — the authenticated principal, NEVER a
    caller-supplied id) and rendered down to a capped string. Graceful: an
    EMPTY digest (no connected accounts) → ``""`` (the agent behaves as
    today); a digest that RAISES → ``""`` (a flaky mail/calendar pull never
    sinks the stream).

    ``digest_fn`` defaults to ``member_day_digest.service.member_day_digest``;
    tests inject a fake so the suite needs no OAuth/network.
    """
    # The gate: identical decision to the private ``user:`` KB scope. A
    # ``None`` here means "not the member's solo session" → no block, no pull.
    scope = _member_private_user_scope(ctx)
    if scope is None:
        return ""
    member_id = ctx.user_id  # proven == the sole member by the gate above

    pull: Callable[[str, str], Awaitable[Any]]
    if digest_fn is not None:
        pull = digest_fn
    else:
        try:
            from pocketpaw_ee.cloud.member_day_digest.service import member_day_digest

            pull = member_day_digest
        except Exception:
            logger.debug("member_day_digest unavailable; skipping briefing", exc_info=True)
            return ""

    try:
        digest = await pull(ctx.workspace_id, member_id)
    except Exception:
        # A briefing is a nicety — a failed mail/calendar pull must never
        # break the chat. Degrade silently to no block.
        logger.debug("member day digest failed; skipping briefing", exc_info=True)
        return ""

    if digest is None or digest.empty:
        return ""

    return _render_briefing(digest)


def _render_briefing(digest: Any) -> str:
    """Render a ``MemberDayDigest`` into the capped <your-day> system block.

    Concise by construction: a short heading, the upcoming events, and a mail
    summary line + top subjects. The whole thing is truncated to
    ``_BRIEFING_MAX_CHARS`` so it can never eat the prompt budget on a busy
    day. The framing tells the agent this is proactive context to weave in
    naturally, not a list to read back verbatim.
    """
    lines: list[str] = [
        "<your-day>",
        "Proactive briefing for the member you're helping — their day at a "
        "glance. Use it to be helpful and anticipatory; don't read it back "
        "verbatim unless asked.",
    ]

    if digest.events:
        lines.append("")
        lines.append("Upcoming (next 7 days):")
        for ev in digest.events:
            when = ev.start or "(time TBD)"
            where = f" @ {ev.location}" if ev.location else ""
            lines.append(f"- {when}: {ev.summary}{where}")

    if digest.unread_mail_count or digest.top_mail:
        lines.append("")
        lines.append(f"Unread mail: {digest.unread_mail_count}")
        for m in digest.top_mail:
            sender = f" — from {m.sender}" if m.sender else ""
            lines.append(f"- {m.subject}{sender}")

    lines.append("</your-day>")
    block = "\n".join(lines)

    # Hard cap. Truncate mid-block and re-close the tag so the agent never
    # sees a dangling open tag, and the budget is respected exactly.
    if len(block) > _BRIEFING_MAX_CHARS:
        closing = "\n…\n</your-day>"
        budget = _BRIEFING_MAX_CHARS - len(closing)
        block = block[:budget].rstrip() + closing
    return block


def _kb_scopes_for_context(ctx: ScopeContext) -> list[str]:
    """Return KB scopes to search for cloud-agent prompt context.

    Ordered most-specific-first (user > pocket > agent > workspace) so that
    the limited KB budget is allocated to the most relevant scope first.

    The leading member-private ``user:`` scope is GATED by
    ``_member_private_user_scope`` — it is present only in a member's own
    solo session and is suppressed in every shared / multi-member room, so
    one member's private mail/calendar KB never bleeds into another member's
    agent context. Mirrors the OSS ``_resolve_kb_scopes`` priority; the gate
    is the cloud-side decision (the OSS resolver only honors the field it is
    handed).
    """
    scopes: list[str] = []
    seen: set[str] = set()
    for candidate in (
        _member_private_user_scope(ctx),
        f"pocket:{ctx.pocket_id}" if ctx.pocket_id else None,
        f"agent:{ctx.target_agent_id}" if ctx.target_agent_id else None,
        f"workspace:{ctx.workspace_id}" if ctx.workspace_id else None,
    ):
        if not candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        scopes.append(candidate)
    return scopes


async def build_knowledge_context(
    ctx: ScopeContext,
    *,
    user_message: str,
    attachments: list[dict[str, Any]] | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> str:
    """Build the per-turn knowledge context — dynamic scope/participants
    tags + KB hits + inlined attachment text. Static behavioral rules are
    NOT included here; the caller must inject them via
    ``pool.run(instructions=...)`` so they land outside the "Your Knowledge
    Base" framing that makes the model treat them as reference data
    instead of rules."""
    scope_block = build_dynamic_context(ctx)
    query = (user_message or "").strip()
    refs = _file_reference_terms(attachments=attachments, mentions=mentions)
    if refs:
        if len(refs) > 12:
            logger.warning(
                "_file_reference_terms returned %d terms; truncating to first 12",
                len(refs),
            )
        ref_line = ", ".join(refs[:12])
        query = f"{query}\nReferenced uploads: {ref_line}" if query else ref_line

    sections: list[str] = []

    # The proactive "your day" briefing — FIRST, so the agent is oriented to
    # the member's day before any retrieval. GATED to the member's OWN solo
    # session (the same ``members == [user_id]`` rule as the private ``user:``
    # KB scope); ``""`` in a shared/multi-member room and when the member has
    # no connected accounts, so non-solo and unconnected sessions are
    # byte-identical to before.
    briefing = await _member_briefing_block(ctx)
    if briefing:
        sections.append(briefing)

    attachments_block = await _build_attachments_block(ctx, attachments)
    if attachments_block:
        sections.append(attachments_block)

    if query:
        kb_block = await _build_kb_snippets_block(ctx, query)
        if kb_block:
            sections.append(kb_block)

    if not sections:
        return scope_block
    return f"{scope_block}\n\n" + "\n\n".join(sections)


async def _build_kb_snippets_block(ctx: ScopeContext, query: str) -> str:
    """Search KB scopes for ``query`` and return a ``<knowledge-base>`` block."""
    scopes = _kb_scopes_for_context(ctx)
    if not scopes:
        return ""

    try:
        from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService
    except Exception:
        logger.debug("KnowledgeService unavailable; skipping KB block", exc_info=True)
        return ""

    snippets: list[tuple[str, str]] = []
    for scope in scopes:
        try:
            text = await KnowledgeService.search_context_for_scope(scope, query, limit=3)
        except Exception:
            logger.warning("knowledge search failed for scope %s", scope, exc_info=True)
            continue
        cleaned = text.strip()
        if cleaned:
            snippets.append((scope, cleaned))

    if not snippets:
        return ""

    kb_lines = [
        "<knowledge-base>",
        "Use relevant snippets below before reaching for extra tools.",
    ]
    for scope, text in snippets:
        kb_lines.append(f"### {scope}\n{text}")
    kb_lines.append("</knowledge-base>")
    return "\n".join(kb_lines)


# Bounds for inlined attachment text. Per-file cap keeps a single huge PDF
# from eating the budget; total cap keeps a batch of files from blowing the
# context window. Image/binary attachments typically yield empty text and
# get a stub entry so the agent at least knows they exist.
_ATTACHMENT_MAX_FILES = 5
_ATTACHMENT_PER_FILE_CHARS = 8000
_ATTACHMENT_TOTAL_CHARS = 30000


async def _build_attachments_block(
    ctx: ScopeContext,
    attachments: list[dict[str, Any]] | None,
) -> str:
    """Inline extracted text from each upload URL in ``attachments``.

    Resolves each attachment via :class:`EEUploadResolver`, runs the
    configured extraction chain on the resulting local path (streaming
    from S3 into a temp file when needed), and emits an
    ``<uploaded-files>`` block the model can read directly. Failures are
    isolated per-file: a single broken extraction or unresolvable URL
    drops only that entry.
    """
    if not attachments or not ctx.workspace_id:
        return ""

    try:
        from pocketpaw.config import get_settings
        from pocketpaw_ee.cloud.extraction import build_chain
        from pocketpaw_ee.cloud.uploads.resolver import default_resolver
    except Exception:
        logger.debug("attachment extraction deps unavailable", exc_info=True)
        return ""

    try:
        resolver = default_resolver()
    except Exception:
        logger.debug("default_resolver unavailable; skipping attachments", exc_info=True)
        return ""

    chain = None
    entries: list[str] = []
    used_chars = 0
    processed = 0

    for att in attachments:
        if processed >= _ATTACHMENT_MAX_FILES:
            break
        if used_chars >= _ATTACHMENT_TOTAL_CHARS:
            break
        if not isinstance(att, dict):
            continue
        url = att.get("url")
        if not isinstance(url, str) or not url:
            continue

        try:
            cm = resolver.open_local_for_url(url, workspace=ctx.workspace_id)
            async with cm as resolved:
                if resolved is None:
                    continue
                rec, path = resolved
                if chain is None:
                    try:
                        chain = build_chain(get_settings())
                    except Exception:
                        logger.exception("build_chain failed; skipping attachments")
                        return ""
                try:
                    result = await chain.run(path, rec.mime)
                except Exception:
                    logger.warning(
                        "extraction failed for attachment %s (file_id=%s)",
                        rec.filename,
                        rec.id,
                        exc_info=True,
                    )
                    continue

                text = (result.text or "").strip()
                header = f"### {rec.filename} ({rec.mime}, {rec.size} bytes)"
                if not text:
                    entries.append(f"{header}\n(no text extracted)")
                    processed += 1
                    continue

                remaining = _ATTACHMENT_TOTAL_CHARS - used_chars
                per_file_cap = min(_ATTACHMENT_PER_FILE_CHARS, remaining)
                if len(text) > per_file_cap:
                    text = text[:per_file_cap].rstrip() + "\n…[truncated]"
                entries.append(f"{header}\n{text}")
                used_chars += len(text)
                processed += 1
        except Exception:
            logger.warning("attachment resolve failed for url=%s", url, exc_info=True)
            continue

    if not entries:
        return ""

    lines = [
        "<uploaded-files>",
        "The user attached the following file(s) to this turn. "
        "Their extracted contents are inlined below — treat them as part "
        "of the user's message, not as external reference.",
    ]
    lines.extend(entries)
    lines.append("</uploaded-files>")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# History rehydration
# ---------------------------------------------------------------------------


def session_key_for(ctx: ScopeContext) -> str:
    """Stable session key for pocket- and session-scope agent runs.

    Mirrors the Mongo ``Message.session_key`` written by the router's
    persist helpers. Keeping the formula in one place lets history
    rehydration use the same key the persist path writes with.
    """
    return f"cloud:{ctx.kind.value}:{ctx.scope_id}:{ctx.target_agent_id}"


async def load_history_for_scope(ctx: ScopeContext, *, limit: int = 50) -> list[dict[str, str]]:
    """Return prior turns as ``[{"role", "content"}]``, oldest first.

    Why: the agent backend keeps conversation state in an in-process SDK
    subprocess keyed by ``session_key``. That state is wiped by any
    backend restart or ``AgentPool`` eviction, at which point the agent
    would otherwise forget every prior message in the thread. Reading
    from the persisted ``Message`` collection restores context.

    Swallows errors (empty list) so a transient Mongo hiccup degrades
    the reply rather than killing the stream.
    """
    try:
        from pocketpaw_ee.cloud.models.message import Message
    except Exception:
        logger.debug("Message model unavailable; returning empty history", exc_info=True)
        return []

    try:
        if ctx.kind in (ScopeKind.POCKET, ScopeKind.SESSION):
            query: dict[str, Any] = {
                "context_type": ctx.kind.value,
                "session_key": session_key_for(ctx),
            }
        else:  # GROUP, DM — both land in a group row
            query = {
                "context_type": "group",
                "group": ctx.scope_id,
                "deleted": False,
            }
        msgs = await Message.find(query).sort("createdAt").limit(limit).to_list()
    except Exception:
        logger.exception("load_history_for_scope failed for %s/%s", ctx.kind.value, ctx.scope_id)
        return []

    out: list[dict[str, str]] = []
    for m in msgs:
        role = getattr(m, "role", None)
        if role not in ("user", "assistant", "system"):
            role = "assistant" if getattr(m, "sender_type", "") == "agent" else "user"
        content = getattr(m, "content", "") or ""
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out
