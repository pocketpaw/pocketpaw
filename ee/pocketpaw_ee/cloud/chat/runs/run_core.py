"""Agent-run core — the loop the executor invokes for every chat run.

Changes:
- 2026-06-13 (feat/claude-sdk-prewarm) — ``execute_run`` now fires
  ``_prewarm_session(ctx)`` as a fire-and-forget ``asyncio.create_task`` right
  after the entity-aware profile is resolved, so the agent's Claude CLI
  subprocess warms CONCURRENTLY with the remaining pre-turn work (knowledge
  context, soul recall, SSE setup, prompt assembly) and turn 1 reuses it instead
  of paying the ~12s cold ``connect()``. ``_prewarm_session`` resolves the SAME
  inputs ``_drive_agent_loop`` will (instructions, the entity-aware
  deny/allow/skills/override, session_key) and calls ``AgentPool.prewarm`` so the
  prewarmed client's cache key matches the first turn's. It is gated to
  smart-routing-OFF (the model is message-derived when routing is on, so a
  message-less prewarm could warm the wrong tier and churn) and swallows every
  error. Skill sessions on smart-routing-ON deployments keep today's cold turn-1.
- 2026-06-10 (sov/w3a-igw — per-run token metering) — real token usage is now
  threaded through the run instead of being dropped. Every backend emits a
  ``token_usage`` ``AgentEvent`` (input / output / cached token counts +
  total_cost_usd + model + backend), but ``_drive_agent_loop`` had no handler for
  it, so it was silently discarded and the ``stream_end`` frame's ``usage`` was a
  hardcoded ``{}``. ``_drive_agent_loop`` now surfaces ``token_usage`` as a
  ``("token_usage", {...})`` tuple; ``execute_run`` keeps the LATEST one, writes it
  to the stream, folds it into BOTH ``stream_end`` frames (success + cancelled /
  empty-text), and persists it onto ``ChatRunDoc.usage`` via
  ``mark_completed`` / ``mark_terminal`` (the durable metering sink for
  outcome-based pricing). Empty for backends / runs that report nothing, so
  existing runs are unchanged.
- 2026-06-08 (feat/connector-mcp-execution / keystone) — the per-stream
  identity binding now also passes ``pocket_id=ctx.pocket_id`` into
  ``attach_agent_identity``, publishing the room's ``Pocket._id`` on the new
  ``agent_service._active_pocket_id`` ContextVar. The connector-execution MCP
  server reads it to scope ``list_connector_actions`` / ``connector_execute``
  to the current pocket. ``None`` for non-pocket (DM/group) threads.
- 2026-05-31 (RFC 13 M0 — inline-spec contract unification). The inline
  Ripple extractor now treats the ``ui-spec`` fence + ``{version, ui}`` envelope
  as the canonical path — the same contract the prompt (``pocketpaw.ripple._inline``)
  tells the agent to emit and the one paw-enterprise ``MarkdownRenderer`` tokenizes
  as a ``ui-spec`` segment. The legacy ``json`` fence + ``{widgets, lifecycle}``
  shape is still accepted via a transitional branch so in-flight conversations
  don't break; that branch is deprecated and slated for removal once no active
  conversation references the old shape (cutover window is RFC 13 open question #6).
- 2026-06-05 (feat/sites-svelte-engine) — ``_drive_agent_loop`` now
  resolves the per-request ``SurfaceProfile`` and threads its
  ``deny_mcp_tool_ids`` (a plain ``frozenset[str]``) into ``AgentPool.run`` →
  ``ClaudeSDKBackend.run``, which subtracts the denied ids from the SDK
  allowlist before launch. This replaces the deleted prompt-sniffing tool gate
  in ``claude_sdk.py``: the /sites svelte-create surface forbids the two
  ripple-create tools via typed policy (resolved from ``meta``) instead of the
  backend string-matching ``engine="svelte"`` in the system prompt. The set is
  empty for every other surface, so the call is a no-op outside /sites
  svelte-create.
- 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①) —
  the per-run ``SurfaceProfile`` is now ENTITY-AWARE and resolved ONCE per run.
  ``execute_run`` calls ``_resolve_entity_profile(ctx)`` right after it resolves
  ``ctx.surface_context``: it takes the pure surface-kind base
  (``resolve_profile``), and — when the chat is bound to a pocket-entity
  (``meta.pocket_id`` set) whose pocket carries a ``surface_profile`` override —
  loads that pocket TENANT-SCOPED (cloud Rule 7) and folds the override OVER the
  base via ``compose_entity_profile`` (ripple entity-wins-if-set; deny / allow /
  skill UNION; system-message entity-wins). The result is stashed on
  ``ctx.resolved_profile``. BOTH profile consumers now read that pre-resolved
  object instead of each calling ``resolve_profile``: ``build_behavior_instructions``
  (ripple-omit, stays sync) and ``_drive_agent_loop`` (tool-deny + tool-allow).
- 2026-06-07 (feat/entity-pocket-profile-field) — ``_drive_agent_loop`` also
  reads ``ctx.resolved_profile.skill_names`` and ``.system_message_override`` and
  forwards them as plain data into ``AgentPool.run`` (withhold-when-empty/None):
  ``skill_names`` drives per-run skill materialization (SDK plugin) +
  non-SDK skill filtering; ``system_message_override`` swaps the agent's base
  system message while keeping the instruction / soul-memory / knowledge layers.
  Net effect: ``ripple_mode``, ``deny_mcp_tool_ids``, ``allowed_sdk_tools``,
  ``skill_names`` and ``system_message_override`` are all PER-ENTITY immediately.
  ``meta.pocket_id`` unset OR no ``pocket.surface_profile`` → resolved profile
  equals the surface base → behavior byte-identical to today (zero regression).
  ``resolve_profile`` itself stays PURE/no-I/O; all entity I/O lives in the
  once-per-run async ``_resolve_entity_profile``.
- 2026-06-08 (feat/agent-plugin-fields, M2) — the AGENT now carries its own
  skill set, folded into the per-run skill materialization so it applies on
  EVERY run the agent does, regardless of surface. ``_agent_skill_set(instance)``
  reads the agent's ``skill_refs`` (direct) UNION the skills of its enabled
  ``plugins`` (resolved via the OSS ``PluginInstaller`` registry — plain read,
  no clone, try/except → empty on failure so a missing registry never breaks a
  run). ``_drive_agent_loop`` UNIONs that set into ``surface_skills`` BEFORE the
  withhold-when-empty forward — including on the legacy ``resolved_profile is
  None`` path — so agent skills materialize even on non-entity / non-profile
  runs. The union still crosses into ``AgentPool.run`` as a plain
  ``frozenset[str]`` (no EE symbol crosses into OSS). Per-agent MCP is DEFERRED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from pocketpaw.agents.pool import (  # type: ignore[import-untyped]
    get_agent_pool,
)
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    attach_agent_identity,
    attach_sse_event_sink,
    build_behavior_instructions,
    build_knowledge_context,
    detach_agent_identity,
    detach_sse_event_sink,
    push_sse_event,
    session_key_for,
)
from pocketpaw_ee.cloud.chat.agent_service import (
    resolve_scope_context as resolve_scope_context,
)
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.surface import (
    SurfaceKind,
    SurfaceMeta,
    SurfaceProfile,
    compose_entity_profile,
    resolve_profile,
    resolve_surface_context,
)

logger = logging.getLogger(__name__)


# Canonical inline-spec fence (RFC 13 M0). The prompt mandates a ``ui-spec``
# fence carrying a ``{version, ui}`` envelope, and paw-enterprise's
# ``MarkdownRenderer`` tokenizes exactly this fence as a ``ui-spec`` segment.
# This is the path every new reply takes.
RIPPLE_UISPEC_RE = re.compile(r"```ui-spec\s*(\{.*?\})\s*```", re.DOTALL)

# DEPRECATED (RFC 13 M0): the legacy fence the cloud extractor used to be the
# only path it knew. It pairs a ``json`` fence with a ``{widgets, lifecycle}``
# shape — the contract before unification. Kept ONLY as a transitional accept
# branch so conversations already holding a legacy block keep rendering. Remove
# once no active conversation references the old shape; the cutover timeline is
# RFC 13 open question #6 ("how long do we keep the legacy json accept-branch").
RIPPLE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _looks_like_ripple_spec(candidate: Any) -> bool:
    """True when ``candidate`` is the canonical inline ``ui-spec`` envelope.

    Recognizes the shapes ``normalize_ripple_spec`` accepts as a UISpec: the
    canonical ``{version, ui}`` doc, a multi-pane ``{panes}`` doc, a spec whose
    UI tree hides under a misnamed top-level key (``root`` / ``tree`` / ``view``
    / ``body`` / ``content``), or a raw root node (``{type, props|children}``).
    A plain ``json`` object that merely happens to sit in a ``ui-spec`` fence
    (no ``ui``/``panes``/node shape) is rejected so we don't attach non-specs.
    """
    if not isinstance(candidate, dict):
        return False
    if isinstance(candidate.get("ui"), dict):
        return True
    if isinstance(candidate.get("panes"), dict):
        return True
    for alias in ("root", "tree", "view", "body", "content"):
        node = candidate.get(alias)
        if isinstance(node, dict) and isinstance(node.get("type"), str):
            return True
    # Raw root node: ``{type: "flex", props|children, ...}`` with no ``ui`` wrap.
    if isinstance(candidate.get("type"), str) and ("props" in candidate or "children" in candidate):
        return True
    return False


def _looks_like_legacy_ripple_spec(candidate: Any) -> bool:
    """True for the DEPRECATED legacy ``{widgets, lifecycle}`` inline shape.

    Transitional gate (RFC 13 M0). Mirrors the original ``_extract_ripple_attachment``
    check. Slated for removal alongside ``RIPPLE_JSON_RE`` per RFC 13 open
    question #6.
    """
    return isinstance(candidate, dict) and ("lifecycle" in candidate or "widgets" in candidate)


def _stream_ttl() -> int:
    return int(os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "3600"))


async def _load_entity_profile_override(workspace_id: str, pocket_id: str) -> dict[str, Any] | None:
    """Tenant-scoped load of a pocket's ``surface_profile`` override, or ``None``.

    Cloud Rule 7: the pocket MUST belong to ``workspace_id`` — a load that
    crosses the tenant boundary is treated as "no override" (we never read
    another tenant's profile). Returns the JSON-shaped override dict
    (``PocketSurfaceProfile`` model-dumped) when the pocket exists, is in this
    workspace, and carries a ``surface_profile``; otherwise ``None``.

    Any failure (bad id, missing pocket, read error) degrades to ``None`` so a
    transient hiccup never breaks the run — the resolved profile then equals the
    surface base (today's behavior).
    """
    if not pocket_id or not workspace_id:
        return None
    try:
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud.models.pocket import Pocket

        pocket = await Pocket.get(PydanticObjectId(pocket_id))
    except Exception:
        logger.debug("entity-profile pocket load failed for %s", pocket_id, exc_info=True)
        return None
    if pocket is None:
        return None
    # Tenant guard (Rule 7): never read a pocket outside the run's workspace.
    if str(getattr(pocket, "workspace", "")) != str(workspace_id):
        logger.warning(
            "entity-profile load crossed tenant boundary (pocket %s not in ws %s) — ignoring",
            pocket_id,
            workspace_id,
        )
        return None
    override = getattr(pocket, "surface_profile", None)
    if override is None:
        return None
    # ``surface_profile`` is a ``PocketSurfaceProfile`` Beanie sub-model; dump it
    # to the plain JSON-ish dict ``compose_entity_profile`` consumes. Keep
    # ``None`` sub-fields (they mean "no opinion") so compose can fall back to
    # the base — ``exclude_none`` would drop a deliberate ``ripple_mode=None``,
    # which is harmless, but keeping them is explicit.
    try:
        return override.model_dump()
    except Exception:
        # Defensive: a stored dict (model_construct path) is already the shape.
        return override if isinstance(override, dict) else None


async def _resolve_entity_profile(ctx: ScopeContext) -> SurfaceProfile:
    """Resolve the ENTITY-AWARE ``SurfaceProfile`` for this run (once).

    ``base`` is the pure surface-kind profile (``resolve_profile`` — no I/O).
    When the chat is bound to a pocket-entity (``surface_context.meta.pocket_id``
    set) whose pocket carries a ``surface_profile`` override, that override is
    loaded TENANT-SCOPED and folded OVER the base via ``compose_entity_profile``.
    Otherwise the base is returned unchanged — the legacy / non-entity path,
    byte-identical to today's behavior.

    Never raises: ``surface_context is None`` (older clients) → the safe default
    profile (ripple on, no deny); a missing/foreign pocket → the surface base.
    """
    if ctx.surface_context is None:
        # Legacy path: no resolved surface. Today's behavior is the default
        # profile (ripple on, no deny) — match it exactly.
        return resolve_profile(SurfaceKind.GENERIC, SurfaceMeta())

    base = resolve_profile(ctx.surface_context.kind, ctx.surface_context.meta)
    pocket_id = ctx.surface_context.meta.pocket_id
    if not pocket_id:
        return base
    override = await _load_entity_profile_override(ctx.workspace_id, pocket_id)
    return compose_entity_profile(base, override)


async def _persist_assistant_message(
    ctx: ScopeContext, content: str, attachments: list[dict[str, Any]]
) -> Any:
    from pocketpaw_ee.cloud.chat import message_service

    return await message_service.persist_assistant_message_for_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_key=session_key_for(ctx),
        target_agent_id=ctx.target_agent_id,
        content=content,
        attachments=attachments,
    )


async def _broadcast_message_new(
    ctx: ScopeContext,
    message_id: str,
    content: str,
    attachments: list[dict[str, Any]],
    created_at: datetime,
) -> None:
    # Include the caller so OS chat panels (which render off chatRoomsStore
    # via WS `message.new`) see the agent reply land without a refresh. The
    # new resumable-runs SSE writes to chatStore, which os/ChatPanel doesn't
    # subscribe to, so without this the caller would never see the message.
    recipients = list(ctx.members) if ctx.members else [ctx.user_id]
    if not recipients:
        return
    data = {
        "id": message_id,
        "group": ctx.scope_id,
        "sender_type": "agent",
        "agent": ctx.target_agent_id,
        "content": content,
        "attachments": attachments,
        "created_at": created_at.isoformat(),
    }
    if xproc.is_worker():
        await xproc.publish_ws_envelope(
            scope_id=ctx.scope_id,
            recipients=recipients,
            ws_type="message.new",
            ws_data=data,
        )
        return

    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import manager

    await manager.broadcast_to_group(
        ctx.scope_id,
        recipients,
        WsOutbound(type="message.new", data=data),
    )


async def _broadcast_agent_typing(ctx: ScopeContext, active: bool) -> None:
    others = [m for m in ctx.members if m != ctx.user_id]
    if not others:
        return
    data = {
        "scope": ctx.kind.value,
        "scope_id": ctx.scope_id,
        "agent_id": ctx.target_agent_id,
        "active": active,
    }
    if xproc.is_worker():
        await xproc.publish_ws_envelope(
            scope_id=ctx.scope_id,
            recipients=others,
            ws_type="agent.typing",
            ws_data=data,
        )
        return

    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import manager

    await manager.broadcast_to_group(
        ctx.scope_id,
        others,
        WsOutbound(type="agent.typing", data=data),
    )


def _extract_specialist_payload(output: Any) -> dict[str, Any] | None:
    """Return the specialist's ``{ok, action, pocket, ...}`` dict, else ``None``.

    Handles three payload shapes: raw dict, JSON string, or MCP content-block list.
    """
    if output is None:
        return None

    def _coerce(data: Any) -> dict[str, Any] | None:
        if (
            isinstance(data, dict)
            and "ok" in data
            and "action" in data
            and isinstance(data.get("pocket"), dict)
        ):
            return data
        return None

    if isinstance(output, dict):
        direct = _coerce(output)
        if direct is not None:
            return direct
        content = output.get("content")
        if isinstance(content, list):
            return _extract_specialist_payload(content)
        return None

    if isinstance(output, str):
        text = output.strip()
        if not text or not text.startswith("{"):
            return None
        try:
            return _coerce(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            return None

    if isinstance(output, list):
        for block in output:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parsed = _extract_specialist_payload(block.get("text", ""))
                if parsed is not None:
                    return parsed
        return None

    return None


async def _maybe_handle_specialist_response(
    *,
    ctx: ScopeContext,
    session_mongo_id: str | None,
    output: Any,
    handled_pocket_ids: set[str],
) -> None:
    """Bind session → pocket and push ``pocket_created`` SSE. Idempotent per pocket id."""
    payload = _extract_specialist_payload(output)
    if payload is None:
        return
    if not payload.get("ok"):
        return
    pocket = payload.get("pocket") or {}
    pocket_id = pocket.get("id") or pocket.get("_id")
    if not pocket_id or pocket_id in handled_pocket_ids:
        return
    handled_pocket_ids.add(pocket_id)

    if session_mongo_id:
        try:
            from pocketpaw_ee.cloud.sessions import service as sessions_service

            await sessions_service.attach_pocket_to_session_doc(
                session_mongo_id, ctx.user_id, pocket_id
            )
        except Exception:
            logger.warning(
                "attach_pocket_to_session_doc failed after specialist run",
                exc_info=True,
            )

    try:
        push_sse_event(
            "pocket_created",
            {
                "pocket_id": pocket_id,
                "pocket": pocket,
                "action": payload.get("action"),
                "session_id": ctx.session_id,
            },
        )
    except Exception:
        logger.debug("push_sse_event(pocket_created) failed", exc_info=True)

    try:
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud._core.realtime.emit import emit
        from pocketpaw_ee.cloud._core.realtime.events import PocketCreated, PocketUpdated
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
        from pocketpaw_ee.cloud.pockets.service import _pocket_event_payload

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is not None:
            event_payload = await _pocket_event_payload(doc)
            event_cls = PocketUpdated if payload.get("action") == "extended" else PocketCreated
            await emit(event_cls(data=event_payload))
    except Exception:
        logger.debug(
            "realtime re-emit of pocket %s after specialist run failed",
            pocket_id,
            exc_info=True,
        )


_DEFAULT_TITLES = ("", "New Chat", "Chat")
_TITLE_PLACEHOLDER_LIMIT = 60


def _truncate_for_title(message: str) -> str:
    raw = (message or "").strip().replace("\n", " ").replace("\r", " ")
    one_line = " ".join(raw.split())
    if len(one_line) > _TITLE_PLACEHOLDER_LIMIT:
        return one_line[:_TITLE_PLACEHOLDER_LIMIT].rstrip() + "…"
    return one_line


async def _set_session_title_in_mongo(session_id: str, title: str) -> bool:
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    return await sessions_service.set_title(session_id, title)


async def _generate_session_title(ctx: ScopeContext, first_message: str) -> None:
    """Write a placeholder title, then upgrade to a Haiku-generated one."""
    if not ctx.session_id:
        return

    placeholder = _truncate_for_title(first_message)
    if placeholder:
        if await _set_session_title_in_mongo(ctx.session_id, placeholder):
            push_sse_event(
                "session_titled",
                {"session_id": ctx.session_id, "title": placeholder},
            )

    try:
        from pocketpaw.config import Settings  # type: ignore[import-untyped]
        from pocketpaw.memory.titler import generate_title  # type: ignore[import-untyped]

        settings = Settings.load()
        title = await generate_title(
            first_message,
            model=settings.chat_title_model,
            api_key=settings.anthropic_api_key or None,
        )
    except Exception:
        logger.warning("cloud Haiku title generation failed for %s", ctx.session_id, exc_info=True)
        return

    if not title or title == placeholder:
        return

    if await _set_session_title_in_mongo(ctx.session_id, title):
        push_sse_event(
            "session_titled",
            {"session_id": ctx.session_id, "title": title},
        )


async def _mark_running(run_id: str) -> None:
    await run_service.mark_running(run_id)


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _extract_ripple_attachment(full_text: str) -> tuple[str, dict[str, Any] | None]:
    """Strip the inline ripple fence and return ``(remaining_text, spec_or_None)``.

    Contract (RFC 13 M0): the canonical fence is ``ui-spec`` carrying a
    ``{version, ui}`` envelope (the prompt's contract and what ``MarkdownRenderer``
    tokenizes). We try that first. If no canonical fence is present, we fall back
    to the DEPRECATED legacy ``json`` fence + ``{widgets, lifecycle}`` shape so
    in-flight conversations keep rendering — that branch ages out per RFC 13 open
    question #6. On either path, the spec is normalized through
    ``normalize_ripple_spec`` and the fence is stripped from the message body.
    A truncated / unparseable fence leaves the text untouched and returns no
    attachment (the frontend's ``ui-spec-error`` segment surfaces it).
    """
    # Canonical path: ``ui-spec`` fence + {version, ui}.
    match = RIPPLE_UISPEC_RE.search(full_text)
    if match is not None:
        candidate = _parse_fence_json(match.group(1))
        if _looks_like_ripple_spec(candidate):
            return _normalize_and_strip(full_text, match, candidate)
        # A ui-spec fence that doesn't parse / isn't a spec falls through; we do
        # NOT then try the legacy json fence on the same text — a malformed
        # canonical block stays inline rather than risk a wrong extraction.
        return full_text, None

    # Transitional path (DEPRECATED): legacy ``json`` fence + {widgets, lifecycle}.
    match = RIPPLE_JSON_RE.search(full_text)
    if match is None:
        return full_text, None
    candidate = _parse_fence_json(match.group(1))
    if not _looks_like_legacy_ripple_spec(candidate):
        return full_text, None
    return _normalize_and_strip(full_text, match, candidate)


def _parse_fence_json(raw: str) -> Any:
    """Parse a fence body to JSON, returning ``None`` on any failure.

    A truncated or malformed block (the model cut off mid-spec) yields ``None``,
    which both shape gates reject — so the fence is left inline for the
    frontend's recovery / ``ui-spec-error`` path rather than half-extracted.
    """
    try:
        return json.loads(raw)
    except Exception:
        logger.debug("Ripple parse failed", exc_info=True)
        return None


def _normalize_and_strip(
    full_text: str, match: re.Match[str], candidate: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Normalize ``candidate`` and remove its fence from ``full_text``."""
    spec: dict[str, Any] = candidate
    try:
        from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

        normalized = normalize_ripple_spec(candidate)
        if normalized:
            spec = normalized
    except Exception:
        logger.debug("Ripple normalize failed", exc_info=True)
    remaining = (full_text[: match.start()] + full_text[match.end() :]).strip()
    return remaining, spec


async def _persist_and_complete(
    spec: RunSpec,
    ctx: ScopeContext,
    full_text: str,
    attachments: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
) -> str:
    """Persist the assistant message, mark the run completed, broadcast.

    ``usage`` (W3a) is the per-run token-metering dict assembled from the
    backend's ``token_usage`` event; it is persisted onto the run doc so each
    completed run carries its real prompt / completion / cached token counts.
    ``None`` / empty leaves the stored usage untouched (legacy / no-usage runs).
    """
    msg = await _persist_assistant_message(ctx, full_text, attachments)
    assistant_id = str(msg.id)
    await run_service.mark_completed(
        spec.run_id,
        assistant_message_id=assistant_id,
        partial_text=full_text,
        usage=usage or None,
    )
    await _broadcast_message_new(
        ctx, assistant_id, full_text, attachments, created_at=msg.createdAt
    )

    try:
        pool = get_agent_pool()
        await pool.observe(ctx.target_agent_id, spec.content, full_text)
    except Exception:
        logger.warning(
            "pool.observe failed for agent %s — per-agent soul not updated",
            ctx.target_agent_id,
            exc_info=True,
        )
    return assistant_id


def _agent_skill_set(instance: Any) -> frozenset[str]:
    """Resolve the agent's own skill set: direct ``skill_refs`` UNION the
    skills bundled by its enabled ``plugins``.

    ``instance.config`` is the raw config dict. ``plugins`` are resolved to
    their skills via the OSS ``PluginInstaller`` registry (a plain read, no
    clone). Unknown plugin names are ignored. The whole plugin resolution is
    guarded in try/except → empty plugin set on any failure, so a missing /
    unreadable registry can never break a run. Returns a plain
    ``frozenset[str]`` — the type that crosses the EE→OSS ``AgentPool.run``
    boundary (no EE symbol crosses into OSS).
    """
    config = getattr(instance, "config", None) or {}
    direct = frozenset(config.get("skill_refs", []) or [])

    enabled = config.get("plugins", []) or []
    plugin_skills: frozenset[str] = frozenset()
    if enabled:
        try:
            from pocketpaw.plugins.installer import PluginInstaller

            by_name = {p.name: p for p in PluginInstaller().list_plugins()}
            plugin_skills = frozenset(s for n in enabled if n in by_name for s in by_name[n].skills)
        except Exception:
            logger.warning(
                "Plugin-skill resolution failed; agent plugin skills skipped this run",
                exc_info=True,
            )
            plugin_skills = frozenset()

    return direct | plugin_skills


async def _prewarm_session(ctx: ScopeContext) -> None:
    """Eagerly warm the agent's CLI subprocess for this run's session BEFORE the
    first model turn (feat/claude-sdk-prewarm).

    The warm-client fix (#1456) made skill-bearing turns REUSE the warm CLI
    subprocess across turns, but the FIRST send of every new session still paid a
    cold ~12s ``connect()`` because the warm client was created lazily inside the
    first ``run``. This fires concurrently with the remaining pre-turn work
    (knowledge-context build, soul recall, SSE setup, prompt assembly) so the
    subprocess is already live by the time ``pool.run`` calls
    ``_get_or_create_client`` — turn 1 then reuses it.

    It resolves the SAME inputs ``_drive_agent_loop`` will (instructions, the
    entity-aware deny/allow/skills/override, session_key) so the prewarmed
    client's cache key MATCHES the first turn's — a mismatch would make turn 1
    EVICT the prewarmed client (a net loss).

    FIRE-AND-FORGET, never-break-a-turn: meant to be wrapped in
    ``asyncio.create_task``. Every failure path is swallowed (this guard +
    ``AgentPool.prewarm`` + the backend's ``prewarm``), so a failed prewarm just
    leaves turn 1 to pay the cold connect it would have paid anyway.

    LIMITATION: skipped when smart routing is ON, because the model is then
    classified from the message (which we don't have yet) — prewarming a guessed
    tier could warm the wrong subprocess and cause evict-churn. Those deployments
    keep today's cold turn-1.
    """
    try:
        from pocketpaw.config import Settings

        # Smart routing makes the model message-dependent → can't match the
        # turn-1 cache key from a message-less prewarm. Skip to avoid churn.
        if Settings.load().smart_routing_enabled:
            return

        pool = get_agent_pool()
        instance = await pool.get(ctx.target_agent_id)

        backend_name = (
            instance.config.get("backend", "claude_agent_sdk")
            if hasattr(instance, "config")
            else None
        )
        # Only the Claude SDK backend has a warm subprocess to prewarm.
        if backend_name is not None and "claude" not in backend_name:
            return

        # Mirror _drive_agent_loop's resolution EXACTLY so the cache key matches.
        behavior_instructions = build_behavior_instructions(ctx, backend_name=backend_name)
        surface_deny: frozenset[str] = frozenset()
        surface_allow: frozenset[str] = frozenset()
        surface_allow_mcp: frozenset[str] | None = None
        surface_sys_override: str | None = None
        surface_skills: frozenset[str] = frozenset()
        if ctx.resolved_profile is not None:
            surface_deny = ctx.resolved_profile.deny_mcp_tool_ids
            surface_allow = ctx.resolved_profile.allowed_sdk_tools or frozenset()
            surface_allow_mcp = ctx.resolved_profile.allow_mcp_tool_ids
            surface_sys_override = ctx.resolved_profile.system_message_override
            surface_skills = ctx.resolved_profile.skill_names or frozenset()
        surface_skills = surface_skills | _agent_skill_set(instance)

        await pool.prewarm(
            ctx.target_agent_id,
            session_key_for(ctx),
            instructions=behavior_instructions,
            deny_mcp_tool_ids=surface_deny,
            allow_sdk_tools=surface_allow,
            allow_mcp_tool_ids=surface_allow_mcp,
            system_message_override=surface_sys_override,
            skill_names=surface_skills,
        )
    except Exception as exc:  # noqa: BLE001 — prewarm must NEVER break a run
        logger.debug("prewarm_session skipped (swallowed): %s", exc)


async def _drive_agent_loop(
    ctx: ScopeContext,
    *,
    user_content: str,
    attachments_in: list[dict[str, Any]] | None,
    mentions_in: list[Any] | None,
    history: list[dict[str, str]] | None,
    is_cancelled: Any,
    emit_stream_start: bool,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Drive ``AgentPool.run`` and yield ``(event_name, event_data)`` tuples."""
    pool = get_agent_pool()
    try:
        instance = await pool.get(ctx.target_agent_id)
    except Exception as e:
        logger.exception("Failed to load agent instance %s", ctx.target_agent_id)
        yield ("error", {"code": "agent.load_failed", "message": str(e)})
        return

    # Bail early if /agent/stop was called while we loaded the agent
    # instance — without this the while-loop cancel check below never
    # runs if build_knowledge_context or pool.run blocks for many
    # seconds, and the SSE generator stays alive waiting for a terminal
    # event that never arrives.
    if await is_cancelled():
        return

    knowledge_context = await build_knowledge_context(
        ctx,
        user_message=user_content,
        attachments=attachments_in,
        mentions=mentions_in,
    )
    # Bail early if /agent/stop was called while knowledge context was
    # being built (another blocking point before the cancel-check loop).
    if await is_cancelled():
        return

    backend_name = (
        instance.config.get("backend", "claude_agent_sdk") if hasattr(instance, "config") else None
    )
    behavior_instructions = build_behavior_instructions(ctx, backend_name=backend_name)

    if emit_stream_start:
        stream_start_payload: dict[str, Any] = {
            "run_id": _new_run_id(),
            "agent_id": ctx.target_agent_id,
            "agent_name": getattr(instance, "agent_name", ""),
            "scope": ctx.kind.value,
            "scope_id": ctx.scope_id,
        }
        if ctx.session_id:
            stream_start_payload["session_id"] = ctx.session_id
        yield ("stream_start", stream_start_payload)

    side_channel_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    sink_token = attach_sse_event_sink(side_channel_queue)
    session_mongo_id = ctx.scope_id if ctx.kind is ScopeKind.SESSION else None
    identity_tokens = attach_agent_identity(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        session_mongo_id=session_mongo_id,
        # Anchor the connector-execution MCP server to the room this stream is
        # in: ``ctx.pocket_id`` is the Mongo ``Pocket._id`` for pocket-scoped
        # chats (and session chats whose Session.pocket is set). ``None`` for
        # plain DM/group threads — the connector tools then say "no pocket".
        pocket_id=ctx.pocket_id,
    )

    if not history and ctx.session_id:
        asyncio.create_task(_generate_session_title(ctx, user_content))

    def _drain_side_channel() -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        while True:
            try:
                events.append(side_channel_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    handled_pocket_ids: set[str] = set()
    next_event_task: asyncio.Task[Any] | None = None
    next_queue_task: asyncio.Task[tuple[str, dict[str, Any]]] | None = None
    try:
        session_key = session_key_for(ctx)
        # Read the per-run tool policy from the PRE-RESOLVED, ENTITY-AWARE
        # profile (entity-rooms chunk ①). ``ctx.resolved_profile`` was resolved
        # once in ``execute_run`` — the surface base composed with the
        # pocket-entity's ``surface_profile`` override (deny UNION, allow UNION).
        # ``deny_mcp_tool_ids`` (a plain ``frozenset[str]``) is subtracted from
        # the SDK allowlist downstream; ``allowed_sdk_tools`` is the optional
        # additive allowlist (entity-rooms STRETCH). Both cross the EE→OSS
        # boundary as plain frozensets — never as imported EE symbols
        # (import-linter forbids EE→OSS imports). ``resolved_profile is None``
        # is the legacy path: empty deny, no allow → unchanged behavior.
        surface_deny: frozenset[str] = frozenset()
        surface_allow: frozenset[str] = frozenset()
        # entity-rooms A1/A2: the per-entity system-message override (a base swap)
        # and the per-entity skill subset. Both READ here and forwarded as plain
        # data (a ``str`` and a ``frozenset[str]``) — never an EE symbol crossing
        # into the OSS pool (import-linter forbids EE→OSS imports). Withheld when
        # None / empty so legacy and non-entity runs are byte-identical.
        surface_sys_override: str | None = None
        surface_skills: frozenset[str] = frozenset()
        # Per-MODE restrictive MCP allow-list (lean per-mode tool set). ``None``
        # = no restriction (broad surfaces like /chat keep every tool); a scoped
        # mode keeps only its own tools plus the universal grant. Forwarded only
        # when not None so legacy / broad runs are byte-identical.
        surface_allow_mcp: frozenset[str] | None = None
        if ctx.resolved_profile is not None:
            surface_deny = ctx.resolved_profile.deny_mcp_tool_ids
            surface_allow = ctx.resolved_profile.allowed_sdk_tools or frozenset()
            surface_allow_mcp = ctx.resolved_profile.allow_mcp_tool_ids
            surface_sys_override = ctx.resolved_profile.system_message_override
            surface_skills = ctx.resolved_profile.skill_names or frozenset()
        # M2: fold the AGENT's own skill set (skill_refs + enabled-plugin skills)
        # into the per-run skill materialization REGARDLESS of the resolved_profile
        # guard above — so an agent's skills materialize on EVERY run it does,
        # including the legacy ``resolved_profile is None`` path (no entity / no
        # profile). Still a plain ``frozenset[str]`` crossing into the OSS pool.
        surface_skills = surface_skills | _agent_skill_set(instance)
        run_kwargs: dict[str, Any] = dict(
            history=history,
            knowledge_context=knowledge_context,
            instructions=behavior_instructions,
            deny_mcp_tool_ids=surface_deny,
            allow_sdk_tools=surface_allow,
        )
        if surface_allow_mcp is not None:
            run_kwargs["allow_mcp_tool_ids"] = surface_allow_mcp
        # Forward the override only when the entity actually set one — withholding
        # keeps the prompt assembly untouched on every other run.
        if surface_sys_override is not None:
            run_kwargs["system_message_override"] = surface_sys_override
        # Forward the skill subset only when non-empty — the OSS pool's
        # withhold-when-empty idiom then keeps the 6 non-Claude backends'
        # narrower signature safe.
        if surface_skills:
            run_kwargs["skill_names"] = surface_skills
        agent_iter = pool.run(
            ctx.target_agent_id,
            user_content,
            session_key,
            **run_kwargs,
        ).__aiter__()

        async def _next_event() -> Any:
            return await agent_iter.__anext__()

        next_event_task = asyncio.create_task(_next_event())
        next_queue_task = asyncio.create_task(side_channel_queue.get())
        while True:
            if await is_cancelled():
                break
            wait_set: set[asyncio.Task[Any]] = {next_queue_task}
            if next_event_task is not None:
                wait_set.add(next_event_task)
            # 1-second timeout so cancellation is checked periodically during
            # long-running LLM calls or tool executions — without a timeout the
            # loop can block here indefinitely (no events yielded → execute_run's
            # post-loop cancel check never runs → /agent/stop returns 200 but the
            # SSE stream stays alive).
            done, _pending = await asyncio.wait(
                wait_set, return_when=asyncio.FIRST_COMPLETED, timeout=1.0
            )
            if next_queue_task in done:
                yield next_queue_task.result()
                for ev in _drain_side_channel():
                    yield ev
                next_queue_task = asyncio.create_task(side_channel_queue.get())
            if next_event_task is None or next_event_task not in done:
                continue
            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                next_event_task = None
                break
            etype = getattr(event, "type", None)
            econtent = getattr(event, "content", "")
            if etype == "done":
                next_event_task = None
                break
            next_event_task = asyncio.create_task(_next_event())
            if etype == "message":
                yield (
                    "chunk",
                    {
                        "content": econtent if isinstance(econtent, str) else "",
                        "type": "text",
                    },
                )
            elif etype == "thinking":
                yield ("thinking", {"content": econtent if isinstance(econtent, str) else ""})
            elif etype == "tool_use":
                meta = getattr(event, "metadata", None) or {}
                name = ""
                tool_input: Any = {}
                if isinstance(meta, dict):
                    name = meta.get("name") or meta.get("tool") or ""
                    tool_input = meta.get("input") or {}
                if not name:
                    if isinstance(econtent, dict):
                        name = econtent.get("tool") or econtent.get("name") or ""
                        tool_input = econtent
                    elif isinstance(econtent, str):
                        name = econtent
                yield ("tool_start", {"tool": name, "input": tool_input})
            elif etype == "tool_result":
                meta = getattr(event, "metadata", None) or {}
                name = ""
                output: Any = econtent
                if isinstance(meta, dict):
                    name = meta.get("name") or meta.get("tool") or ""
                if not name and isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                if isinstance(econtent, dict):
                    output = econtent.get("result", econtent)
                await _maybe_handle_specialist_response(
                    ctx=ctx,
                    session_mongo_id=session_mongo_id,
                    output=output,
                    handled_pocket_ids=handled_pocket_ids,
                )
                yield ("tool_result", {"tool": name, "output": output})
            elif etype == "token_usage":
                # Real per-run token metering (W3a). The backend (claude_sdk and
                # every other backend) emits a ``token_usage`` AgentEvent whose
                # ``metadata`` carries input / output / cached token counts +
                # total_cost_usd + model + backend. This branch used to be
                # missing, so the whole event was silently dropped and the run's
                # ``usage`` stayed ``{}`` (tokens were not metered). Surface the
                # metadata as a plain dict; ``execute_run`` keeps the latest one
                # and folds it into the ``stream_end`` frame + the persisted run
                # doc. Mirrors the native ``agents/loop.py`` token_usage handler.
                meta = getattr(event, "metadata", None) or {}
                usage_payload = dict(meta) if isinstance(meta, dict) else {}
                yield ("token_usage", usage_payload)
            elif etype == "error":
                # Surface backend-yielded errors instead of silently dropping
                # them — a misconfigured backend (codex_cli without
                # ``openai-codex-sdk``, claude_agent_sdk without the CLI) would
                # otherwise end the stream with no diagnostic and a blank reply.
                # Port of PR #1191's fix from the old ``_run_agent_stream``.
                message = econtent if isinstance(econtent, str) else str(econtent)
                logger.warning(
                    "Backend yielded error for agent=%s: %s",
                    ctx.target_agent_id,
                    message[:200],
                )
                yield ("error", {"code": "agent.backend_error", "message": message})
                break
        for ev in _drain_side_channel():
            yield ev
    finally:
        pending = [t for t in (next_event_task, next_queue_task) if t is not None and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            detach_sse_event_sink(sink_token)
        except Exception:
            pass
        try:
            detach_agent_identity(identity_tokens)
        except Exception:
            pass


async def _iter_agent_events(
    spec: RunSpec, ctx: ScopeContext
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    # Transport writes happen only in ``execute_run`` so the seam stays clean.
    transport = get_stream_transport()

    async def _is_cancelled() -> bool:
        return await transport.is_cancelled(spec.run_id)

    async for ev in _drive_agent_loop(
        ctx,
        user_content=spec.content,
        attachments_in=list(spec.attachments) if spec.attachments else None,
        mentions_in=list(spec.mentions) if spec.mentions else None,
        history=list(spec.history),
        is_cancelled=_is_cancelled,
        emit_stream_start=True,
    ):
        yield ev


async def _handle_interrupted_cleanup(
    spec: RunSpec,
    ctx: ScopeContext,
    full_text: str,
    transport: Any,
) -> None:
    """Best-effort finalisation when ``execute_run`` is cancelled by the host.

    Each step is wrapped so a single transient failure (Mongo, Redis) can't
    block the others — every action is independently best-effort. The
    caller wraps THIS in ``asyncio.shield`` so a second cancel arriving
    during the cleanup can't abort it mid-flight.
    """
    try:
        await _broadcast_agent_typing(ctx, active=False)
    except Exception:
        logger.debug("agent.typing(active=False) broadcast failed", exc_info=True)
    try:
        await run_service.mark_terminal(
            spec.run_id,
            status="interrupted",
            partial_text=full_text,
        )
    except Exception:
        logger.exception("mark_terminal(interrupted) failed for %s", spec.run_id)
    try:
        await transport.append_event(spec.run_id, "interrupted", {"run_id": spec.run_id})
        await transport.set_ttl(spec.run_id, _stream_ttl())
    except Exception:
        logger.debug("interrupted stream write failed", exc_info=True)


async def execute_run(spec: RunSpec) -> None:
    """Run the agent for ``spec`` and write every event to the transport.

    A stream that produced no text is treated like ``cancelled`` for
    persistence purposes (no assistant message created).
    """
    transport = get_stream_transport()
    ctx = await resolve_scope_context(
        scope=spec.context_type,
        scope_id=spec.scope_id,
        user_id=spec.user_id,
        agent_id_hint=spec.agent_id,
    )
    ctx.intent = spec.intent

    # Mirror agent_router._ensure_scope_session so _drive_agent_loop's
    # title-gen guard (`if not history and ctx.session_id`) actually fires
    # — the executor builds its own ctx and resolve_scope_context leaves
    # session_id as None.
    try:
        from pocketpaw_ee.cloud.sessions import service as _sessions_service

        ctx.session_id = await _sessions_service.ensure_for_agent_scope(
            kind=ctx.kind.value,
            scope_id=ctx.scope_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            target_agent_id=ctx.target_agent_id,
        )
    except Exception:
        logger.exception("ensure session failed for run %s", spec.run_id)
        ctx.session_id = None

    # Mirror agent_router:77 — re-resolve the surface context from the spec.
    # The HTTP handler resolves ``ctx.surface_context`` on ITS request ctx,
    # but submits a RunSpec to the executor, which rebuilds its own ctx via
    # resolve_scope_context (scope only — surface_context stays None). Without
    # this the whole SurfaceProfile gate silently no-ops on the /agent path:
    # the tool-deny (run_core:457), the ripple-block omission + create-svelte
    # skill (build_behavior_instructions), and the surface preamble
    # (build_dynamic_context) all see None and fall back to the legacy shape.
    # The resolver never raises; a missing/legacy hint -> GENERIC + empty deny.
    ctx.surface_context = await resolve_surface_context(
        ctx.workspace_id,
        ctx.user_id,
        {"surface": spec.surface, "meta": spec.surface_meta},
    )

    # Resolve the ENTITY-AWARE SurfaceProfile ONCE, now that surface_context +
    # tenancy are in hand (entity-rooms chunk ①). When the chat is bound to a
    # pocket-entity (meta.pocket_id set) carrying a surface_profile override,
    # this folds that override (tenant-scoped load) OVER the surface base; else
    # it returns the base unchanged. Both consumers (build_behavior_instructions
    # ripple-omit + _drive_agent_loop tool-deny/allow) read ctx.resolved_profile
    # instead of re-resolving. Never raises — a missing/foreign pocket degrades
    # to the surface base (today's behavior).
    ctx.resolved_profile = await _resolve_entity_profile(ctx)

    # PREWARM (feat/claude-sdk-prewarm): kick off warming the agent's CLI
    # subprocess for this session NOW — concurrently with the remaining pre-turn
    # work below (mark-running, typing broadcast, and inside _drive_agent_loop:
    # knowledge-context build, soul recall, SSE setup, prompt assembly). By the
    # time pool.run reaches the first connect(), the subprocess is already live
    # and turn 1 reuses it instead of paying the ~12s cold connect. Fire-and-
    # forget: _prewarm_session swallows every error, so it can never delay or
    # break this run; the task is intentionally not awaited.
    asyncio.create_task(_prewarm_session(ctx))

    await _mark_running(spec.run_id)
    await _broadcast_agent_typing(ctx, active=True)

    full_text = ""
    cancelled = False
    error: Exception | None = None
    backend_error_message: str | None = None
    # Per-run token metering (W3a). The backend yields a ``token_usage`` event
    # carrying the real prompt / completion / cached token counts; ``_drive_agent_loop``
    # surfaces it as a ``("token_usage", {...})`` tuple. Keep the LATEST one (a
    # multi-turn agent loop can report usage more than once) so the final
    # ``stream_end`` frame and the persisted run doc carry actual counts instead
    # of the old hardcoded ``{}``.
    usage: dict[str, Any] = {}
    try:
        async for event_name, event_data in _iter_agent_events(spec, ctx):
            if event_name == "chunk":
                content = event_data.get("content", "")
                if isinstance(content, str):
                    full_text += content
            elif event_name == "token_usage":
                if isinstance(event_data, dict) and event_data:
                    usage = event_data
            await transport.append_event(spec.run_id, event_name, event_data)
            if event_name == "error":
                # ``_drive_agent_loop`` already broke out after yielding this.
                # The frame is terminal for the client (TERMINAL_EVENTS); stop
                # writing and route to the failed-mark path below so the doc
                # doesn't get flipped to ``completed`` by the empty-text branch.
                backend_error_message = str(event_data.get("message") or "")
                break
    except asyncio.CancelledError:
        # The task itself was cancelled (worker shutdown, host signal). Run
        # the interrupted-cleanup INSIDE the except clause so the bare
        # ``raise`` below re-raises the original CancelledError instance —
        # preserving the cancel-reason arq supplies via ``task.cancel(msg)``
        # and the original traceback. The cleanup is shielded so a second
        # cancel (SIGKILL grace window) can't abort mark_terminal mid-flight
        # and strand the doc in ``running`` with no terminal stream frame.
        logger.info("execute_run %s cancelled by host", spec.run_id)
        try:
            await asyncio.shield(_handle_interrupted_cleanup(spec, ctx, full_text, transport))
        except asyncio.CancelledError:
            # The outer await is cancelled but the shielded inner task
            # continues running to completion in the background. That's
            # exactly what we want; just don't re-raise from this layer —
            # let the original cancel propagate after the except clause.
            pass
        except Exception:
            logger.exception("interrupted cleanup raised after shield for %s", spec.run_id)
        raise
    except Exception as exc:
        error = exc
        logger.exception("execute_run %s crashed", spec.run_id)
        await transport.append_event(
            spec.run_id,
            "error",
            {"code": "agent.run_failed", "message": str(exc)},
        )

    # Check cancellation AFTER the agent loop. _drive_agent_loop now checks the
    # cancel flag internally (via _iter_agent_events -> real _is_cancelled callback
    # with a 1-second asyncio.wait timeout), so the loop exits cleanly when the
    # user hits /agent/stop. Without this the SSE stream stays alive because
    # execute_run only checked cancellation inside the loop (between events), and
    # _drive_agent_loop could block on asyncio.wait for the first LLM event for
    # many seconds without yielding -- the /agent/stop endpoint returned 200 but
    # the stream never stopped.
    if await transport.is_cancelled(spec.run_id):
        cancelled = True

    # Drop the typing indicator before persist so a slow Mongo write
    # doesn't leave it stuck on. Only reached on non-cancelled paths;
    # the cancelled path handles typing-off inside the cleanup helper.
    try:
        await _broadcast_agent_typing(ctx, active=False)
    except Exception:
        logger.debug("agent.typing(active=False) broadcast failed", exc_info=True)

    if error is not None or backend_error_message is not None:
        err_msg = str(error) if error is not None else (backend_error_message or "")
        try:
            await run_service.mark_terminal(
                spec.run_id,
                status="failed",
                partial_text=full_text,
                error=err_msg,
            )
        except Exception:
            logger.exception("mark_terminal(failed) failed for %s", spec.run_id)
        await transport.set_ttl(spec.run_id, _stream_ttl())
        return

    if cancelled or not full_text.strip():
        # Empty-text non-cancelled runs still complete cleanly — without this,
        # the doc would sit in ``running`` until the 10-minute sweeper marked
        # it ``interrupted``, surfacing a phantom active_run to the frontend.
        try:
            if cancelled:
                await run_service.mark_terminal(
                    spec.run_id,
                    status="cancelled",
                    partial_text=full_text,
                    usage=usage or None,
                )
            else:
                await run_service.mark_completed(
                    spec.run_id,
                    assistant_message_id=None,
                    partial_text=full_text,
                    usage=usage or None,
                )
        except Exception:
            logger.exception(
                "mark_%s failed for %s",
                "cancelled" if cancelled else "completed",
                spec.run_id,
            )
        await transport.append_event(
            spec.run_id,
            "stream_end",
            {"assistant_message_id": None, "usage": usage, "cancelled": cancelled},
        )
        await transport.set_ttl(spec.run_id, _stream_ttl())
        return

    remaining_text, ripple_spec = _extract_ripple_attachment(full_text)
    attachments: list[dict[str, Any]] = []
    if ripple_spec is not None:
        attachments.append({"type": "ripple", "meta": ripple_spec})
        full_text = remaining_text
        await transport.append_event(spec.run_id, "ripple", {"spec": ripple_spec})

    assistant_id = await _persist_and_complete(spec, ctx, full_text, attachments, usage=usage)
    await transport.append_event(
        spec.run_id,
        "stream_end",
        {"assistant_message_id": assistant_id, "usage": usage, "cancelled": False},
    )
    await transport.set_ttl(spec.run_id, _stream_ttl())
