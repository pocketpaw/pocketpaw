"""Cloud agent chat service — scope resolution, toolset assembly, context.

Keeps the router thin: the router handles HTTP + SSE plumbing; this module
handles *what the agent sees*:

* ``resolve_scope_context`` turns (scope, scope_id, user_id) into a
  ``ScopeContext`` including the target agent id, members, and
  pocket-scoped tool specs where applicable.
* ``load_history_for_scope`` rehydrates prior chat turns from Mongo so the
  agent carries context across backend restarts and pool evictions.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ee.cloud.shared.errors import CloudError, NotFound
from ee.ripple import (
    INLINE_RIPPLE_SYSTEM_PROMPT,
    POCKET_DELEGATION_RULE,
    POCKET_ID_TOKEN,
    get_pocket_prompts,
)
from ee.ripple._pockets import _MCP_POCKET_BACKENDS

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


def attach_agent_identity(
    *, workspace_id: str, user_id: str, session_mongo_id: str | None = None
) -> tuple[Token, Token, Token]:
    """Bind workspace / user / session identity for the active stream's
    MCP tools. ``session_mongo_id`` is the ``Session._id`` the chat is
    streaming through — used by ``create_pocket`` to link the active
    session to the freshly-created pocket."""
    return (
        _active_workspace_id.set(workspace_id),
        _active_user_id.set(user_id),
        _active_session_mongo_id.set(session_mongo_id),
    )


def detach_agent_identity(tokens: tuple[Token, Token, Token]) -> None:
    ws_token, user_token, session_token = tokens
    _active_workspace_id.reset(ws_token)
    _active_user_id.reset(user_token)
    _active_session_mongo_id.reset(session_token)


def current_workspace_id() -> str | None:
    return _active_workspace_id.get()


def current_user_id() -> str | None:
    return _active_user_id.get()


def current_session_mongo_id() -> str | None:
    return _active_session_mongo_id.get()


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
    # Optional client-supplied intent hint that swaps which system-prompt
    # block ``build_context_block`` emits. ``pocket_create`` makes the
    # agent reach for the ``create_pocket`` MCP tool instead of rendering
    # an inline ``ui-spec`` chat reply.
    intent: str | None = None


# ---------------------------------------------------------------------------
# Beanie accessors (thin wrappers so tests can patch them)
# ---------------------------------------------------------------------------


async def _get_group(group_id: str) -> Any:
    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    try:
        return await Group.get(PydanticObjectId(group_id))
    except Exception:
        return None


async def _get_pocket(pocket_id: str) -> Any:
    from beanie import PydanticObjectId

    from ee.cloud.models.pocket import Pocket

    try:
        return await Pocket.get(PydanticObjectId(pocket_id))
    except Exception:
        return None


async def _get_session(session_id: str) -> Any:
    from beanie import PydanticObjectId

    from ee.cloud.models.session import Session

    try:
        return await Session.get(PydanticObjectId(session_id))
    except Exception:
        return None


async def _get_default_workspace_agent_id(workspace_id: str) -> str | None:
    """Resolve the workspace's default ``pocketpaw`` agent id, or ``None``.

    Mirrors the slug used by ``seed_default_agent`` in ``auth/core.py``. Pockets
    that haven't had an agent explicitly attached still chat against this
    workspace-default agent.
    """
    if not workspace_id:
        return None
    try:
        from ee.cloud.models.agent import Agent

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
    pocket_id = getattr(session, "pocket", None)
    if pocket_id:
        pocket = await _get_pocket(str(pocket_id))
        if pocket is not None:
            pocket_tool_specs = list(getattr(pocket, "tool_specs", []) or [])

    target = agent_id_hint or getattr(session, "agent", None)
    if not target:
        # Sessions created via ``createPocketSession`` don't yet pin an agent
        # — fall back to the workspace's default ``pocketpaw`` agent (same
        # rule ``_resolve_pocket`` applies). Keeps cold-start chats in a
        # newly-created pocket session working without the caller having to
        # explicitly pass ``agent_id``.
        workspace_id = str(getattr(session, "workspace", ""))
        target = await _get_default_workspace_agent_id(workspace_id)
        if not target:
            raise CloudError(400, "session.no_agent", "Session has no agent")

    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id=scope_id,
        workspace_id=str(getattr(session, "workspace", "")),
        user_id=user_id,
        members=[user_id],
        target_agent_id=target,
        agent_ids_in_scope=[target],
        pocket_tool_specs=pocket_tool_specs,
        pocket_id=str(pocket_id) if pocket_id else None,
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

    return ScopeContext(
        kind=kind,
        scope_id=scope_id,
        workspace_id=str(getattr(group, "workspace", "")),
        user_id=user_id,
        members=members,
        target_agent_id=target,
        agent_ids_in_scope=agent_ids,
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
    others get the heavy inline pocket prompt.
    """
    parts: list[str] = []
    if backend_name in _MCP_POCKET_BACKENDS:
        parts.append(INLINE_RIPPLE_SYSTEM_PROMPT)
        parts.append(POCKET_DELEGATION_RULE)
    else:
        creation_prompt, interaction_prompt = get_pocket_prompts(backend_name=backend_name)
        if ctx.intent == "pocket_create":
            parts.append(creation_prompt)
        elif ctx.pocket_id:
            parts.append(interaction_prompt.replace(POCKET_ID_TOKEN, ctx.pocket_id))
        else:
            parts.append(INLINE_RIPPLE_SYSTEM_PROMPT)
    return "\n".join(parts)


def build_dynamic_context(ctx: ScopeContext) -> str:
    """Return only the per-turn dynamic context tags — scope,
    participants, current-pocket-id. Pairs with
    ``build_behavior_instructions``: the dynamic context is reference
    data and lives inside the ``knowledge_context`` wrapper; the
    behavioral instructions live at the top level."""
    member_list = ", ".join(ctx.members) if ctx.members else "(none)"
    parts = [
        f"<scope>{ctx.kind.value} {ctx.scope_id}</scope>",
        f"<participants>{member_list}</participants>",
    ]
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


def _kb_scopes_for_context(ctx: ScopeContext) -> list[str]:
    """Return KB scopes to search for cloud-agent prompt context.

    Ordered most-specific-first (pocket > agent > workspace) so that
    the limited KB budget is allocated to the most relevant scope first.
    """
    scopes: list[str] = []
    seen: set[str] = set()
    for candidate in (
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
    tags + KB hits. Static behavioral rules are NOT included here; the
    caller must inject them via ``pool.run(instructions=...)`` so they
    land outside the "Your Knowledge Base" framing that makes the model
    treat them as reference data instead of rules."""
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
    if not query:
        return scope_block

    scopes = _kb_scopes_for_context(ctx)
    if not scopes:
        return scope_block

    try:
        from ee.cloud.agents.knowledge import KnowledgeService
    except Exception:
        logger.debug("KnowledgeService unavailable; using scope block only", exc_info=True)
        return scope_block

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
        return scope_block

    kb_lines = [
        "<knowledge-base>",
        "Use relevant snippets below before reaching for extra tools.",
    ]
    for scope, text in snippets:
        kb_lines.append(f"### {scope}\n{text}")
    kb_lines.append("</knowledge-base>")
    return f"{scope_block}\n\n" + "\n\n".join(kb_lines)


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
        from ee.cloud.models.message import Message
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
