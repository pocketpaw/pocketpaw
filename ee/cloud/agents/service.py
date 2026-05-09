"""Agents domain — business logic service.

Sole owner of writes to the ``Agent`` Beanie document. Module-level
``async def`` API. Eager soul materialization
(``get_agent_pool().ensure_soul``) preserved on create when
``soul_enabled``.

Public API:
- ``create(ctx, workspace_id, body)``
- ``get(agent_id)``
- ``get_by_slug(workspace_id, slug)``
- ``list_agents(workspace_id, query=None)``
- ``update(ctx, agent_id, body)``
- ``delete(ctx, agent_id)``
- ``get_scopes(agent_id)``
- ``set_scopes(agent_id, scopes)``
- ``discover(ctx, workspace_id, body)``
- ``legacy_ctx(user_id, workspace_id)`` — helper for the router
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import ConflictError, Forbidden, NotFound
from ee.cloud.agents.domain import Agent, AgentConfigSpec
from ee.cloud.agents.dto import (
    CreateAgentRequest,
    DiscoverRequest,
    UpdateAgentRequest,
)
from ee.cloud.agents.scope_rules import normalise_and_validate_scopes
from ee.cloud.models.agent import Agent as _AgentDoc
from ee.cloud.models.agent import AgentConfig as _AgentConfigDoc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _config_to_domain(c: _AgentConfigDoc) -> AgentConfigSpec:
    return AgentConfigSpec(
        backend=c.backend,
        model=c.model,
        system_prompt=c.system_prompt,
        tools=tuple(c.tools),
        trust_level=c.trust_level,
        temperature=c.temperature,
        max_tokens=c.max_tokens,
        scopes=tuple(c.scopes),
        soul_enabled=c.soul_enabled,
        soul_persona=c.soul_persona,
        soul_archetype=c.soul_archetype,
        soul_values=tuple(c.soul_values),
        soul_ocean=tuple(c.soul_ocean.items()),
    )


def _config_to_doc(c: AgentConfigSpec) -> _AgentConfigDoc:
    return _AgentConfigDoc(
        backend=c.backend,
        model=c.model,
        system_prompt=c.system_prompt,
        tools=list(c.tools),
        trust_level=c.trust_level,
        temperature=c.temperature,
        max_tokens=c.max_tokens,
        scopes=list(c.scopes),
        soul_enabled=c.soul_enabled,
        soul_persona=c.soul_persona,
        soul_archetype=c.soul_archetype,
        soul_values=list(c.soul_values),
        soul_ocean=dict(c.soul_ocean),
    )


def _to_domain(doc: _AgentDoc) -> Agent:
    return Agent(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        slug=doc.slug,
        avatar=doc.avatar,
        visibility=doc.visibility,
        owner=doc.owner,
        config=_config_to_domain(doc.config),
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        updated_at=getattr(doc, "updatedAt", None),  # type: ignore[arg-type]
    )


def legacy_ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    """Build a RequestContext for routers that haven't migrated to
    ``Depends(request_context)`` yet."""
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="legacy",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def _build_create_config(body: CreateAgentRequest) -> AgentConfigSpec:
    """Build a domain AgentConfigSpec from a CreateAgentRequest."""
    base = AgentConfigSpec(
        backend=body.backend,
        model=body.model,
        system_prompt=body.system_prompt,
        soul_enabled=body.soul_enabled,
        soul_persona=body.persona,
        soul_archetype=body.soul_archetype or f"The {body.name}",
    )
    overrides: dict[str, Any] = {}
    if body.temperature is not None:
        overrides["temperature"] = body.temperature
    if body.max_tokens is not None:
        overrides["max_tokens"] = body.max_tokens
    if body.tools is not None:
        overrides["tools"] = tuple(body.tools)
    if body.trust_level is not None:
        overrides["trust_level"] = body.trust_level
    if body.scopes is not None:
        overrides["scopes"] = tuple(body.scopes)
    if body.soul_values is not None:
        overrides["soul_values"] = tuple(body.soul_values)
    if body.soul_ocean is not None:
        overrides["soul_ocean"] = tuple(body.soul_ocean.items())
    return replace(base, **overrides) if overrides else base


def _apply_update(current: AgentConfigSpec, body: UpdateAgentRequest) -> AgentConfigSpec:
    """Apply config-shaped overrides from an UpdateAgentRequest."""
    if body.config is not None:
        c = body.config
        return AgentConfigSpec(
            backend=c.get("backend", current.backend),
            model=c.get("model", current.model),
            system_prompt=c.get("system_prompt", current.system_prompt),
            tools=tuple(c.get("tools", list(current.tools))),
            trust_level=c.get("trust_level", current.trust_level),
            temperature=c.get("temperature", current.temperature),
            max_tokens=c.get("max_tokens", current.max_tokens),
            scopes=tuple(c.get("scopes", list(current.scopes))),
            soul_enabled=c.get("soul_enabled", current.soul_enabled),
            soul_persona=c.get("soul_persona", current.soul_persona),
            soul_archetype=c.get("soul_archetype", current.soul_archetype),
            soul_values=tuple(c.get("soul_values", list(current.soul_values))),
            soul_ocean=tuple(
                c.get("soul_ocean", dict(current.soul_ocean)).items()
                if isinstance(c.get("soul_ocean", dict(current.soul_ocean)), dict)
                else current.soul_ocean
            ),
        )

    overrides: dict[str, Any] = {}
    for field, attr in [
        ("backend", body.backend),
        ("model", body.model),
        ("system_prompt", body.system_prompt),
        ("temperature", body.temperature),
        ("max_tokens", body.max_tokens),
        ("trust_level", body.trust_level),
        ("soul_enabled", body.soul_enabled),
        ("soul_archetype", body.soul_archetype),
    ]:
        if attr is not None:
            overrides[field] = attr
    if body.tools is not None:
        overrides["tools"] = tuple(body.tools)
    if body.scopes is not None:
        overrides["scopes"] = tuple(body.scopes)
    if body.soul_values is not None:
        overrides["soul_values"] = tuple(body.soul_values)
    if body.soul_ocean is not None:
        overrides["soul_ocean"] = tuple(body.soul_ocean.items())
    if body.persona is not None:
        overrides["soul_persona"] = body.persona

    return replace(current, **overrides) if overrides else current


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create(ctx: RequestContext, workspace_id: str, body: CreateAgentRequest) -> Agent:
    existing = await _AgentDoc.find_one(
        _AgentDoc.workspace == workspace_id,
        _AgentDoc.slug == body.slug,
    )
    if existing is not None:
        raise ConflictError(
            "agent.slug_taken",
            f"Slug '{body.slug}' is already in use in this workspace",
        )

    config = _build_create_config(body)
    doc = _AgentDoc(
        workspace=workspace_id,
        name=body.name,
        slug=body.slug,
        avatar=body.avatar,
        visibility=body.visibility,
        owner=ctx.user_id,
        config=_config_to_doc(config),
    )
    await doc.insert()
    agent = _to_domain(doc)

    # Eagerly materialize the soul on disk if enabled. Failures are
    # non-fatal; AgentPool will retry lazily on first chat.
    if agent.config.soul_enabled:
        await _try_eager_soul(agent)

    return agent


async def get(agent_id: str) -> Agent:
    try:
        doc = await _AgentDoc.get(PydanticObjectId(agent_id))
    except Exception:
        doc = None
    if doc is None:
        raise NotFound("agent", agent_id)
    return _to_domain(doc)


async def get_by_slug(workspace_id: str, slug: str) -> Agent:
    doc = await _AgentDoc.find_one(
        _AgentDoc.workspace == workspace_id,
        _AgentDoc.slug == slug,
    )
    if doc is None:
        raise NotFound("agent", slug)
    return _to_domain(doc)


async def list_agents(workspace_id: str, *, query: str | None = None) -> list[Agent]:
    filters: dict[str, Any] = {"workspace": workspace_id}
    if query:
        filters["name"] = {"$regex": query, "$options": "i"}
    docs = await _AgentDoc.find(filters).to_list()
    return [_to_domain(d) for d in docs]


async def update(ctx: RequestContext, agent_id: str, body: UpdateAgentRequest) -> Agent:
    try:
        doc = await _AgentDoc.get(PydanticObjectId(agent_id))
    except Exception:
        doc = None
    if doc is None:
        raise NotFound("agent", agent_id)
    if doc.owner != ctx.user_id:
        raise Forbidden("agent.not_owner", "Only the agent owner can update it")

    new_config = _apply_update(_config_to_domain(doc.config), body)

    if body.name is not None:
        doc.name = body.name
    if body.avatar is not None:
        doc.avatar = body.avatar
    if body.visibility is not None:
        doc.visibility = body.visibility
    if new_config != _config_to_domain(doc.config):
        doc.config = _config_to_doc(new_config)
    await doc.save()
    return _to_domain(doc)


async def delete(ctx: RequestContext, agent_id: str) -> None:
    try:
        doc = await _AgentDoc.get(PydanticObjectId(agent_id))
    except Exception:
        doc = None
    if doc is None:
        raise NotFound("agent", agent_id)
    if doc.owner != ctx.user_id:
        raise Forbidden("agent.not_owner", "Only the agent owner can delete it")
    await doc.delete()


async def get_scopes(agent_id: str) -> list[str]:
    agent = await get(agent_id)
    return list(agent.config.scopes)


async def set_scopes(agent_id: str, scopes: list[str]) -> list[str]:
    cleaned = normalise_and_validate_scopes(scopes)
    try:
        doc = await _AgentDoc.get(PydanticObjectId(agent_id))
    except Exception:
        doc = None
    if doc is None:
        raise NotFound("agent", agent_id)
    new_config = replace(_config_to_domain(doc.config), scopes=tuple(cleaned))
    doc.config = _config_to_doc(new_config)
    await doc.save()
    return list(new_config.scopes)


async def discover(
    ctx: RequestContext,
    workspace_id: str,
    body: DiscoverRequest,
) -> list[Agent]:
    filters: dict[str, Any] = {}
    if body.visibility == "private":
        filters["workspace"] = workspace_id
        filters["owner"] = ctx.user_id
    elif body.visibility == "workspace":
        filters["workspace"] = workspace_id
    elif body.visibility == "public":
        filters["visibility"] = "public"
    else:
        filters["$or"] = [
            {"workspace": workspace_id, "owner": ctx.user_id},
            {"workspace": workspace_id, "visibility": "workspace"},
            {"visibility": "public"},
        ]
    if body.query:
        filters["name"] = {"$regex": body.query, "$options": "i"}

    skip = (body.page - 1) * body.page_size
    docs = await _AgentDoc.find(filters).skip(skip).limit(body.page_size).to_list()
    return [_to_domain(d) for d in docs]


async def _try_eager_soul(agent: Agent) -> None:
    """Best-effort eager soul materialization. Logs and continues on failure."""
    try:
        from pocketpaw.agents.pool import get_agent_pool

        doc = await _AgentDoc.get(PydanticObjectId(agent.id))
        if doc is None:
            return
        await get_agent_pool().ensure_soul(doc)
    except Exception:
        logger.warning("Eager soul creation failed for agent %s", agent.id, exc_info=True)


async def suggest_for_mentions(workspace_id: str, q: str, *, limit: int = 8) -> list[dict]:
    """Return up to ``limit`` agents matching ``q`` against name / slug.
    Used by the chat ``/mentions/suggest`` endpoint."""
    aquery: dict = {"workspace": workspace_id}
    if q:
        aquery["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"slug": {"$regex": q, "$options": "i"}},
        ]
    docs = await _AgentDoc.find(aquery).limit(limit).to_list()
    return [
        {
            "type": "agent",
            "id": str(a.id),
            "display_name": a.name or a.slug,
        }
        for a in docs
    ]


async def is_owner_or_workspace_admin(agent_id: str, user: Any) -> bool:
    """Return ``True`` if ``user`` owns the agent or is an admin in the
    agent's workspace. Raises ``NotFound`` if the agent doesn't exist.

    Used by the ``require_agent_owner_or_admin`` FastAPI guard so the
    Agent Beanie load stays inside the service.
    """
    from pocketpaw.ee.guards.deps import resolve_workspace_role
    from pocketpaw.ee.guards.rbac import Forbidden as GuardForbidden
    from pocketpaw.ee.guards.rbac import WorkspaceRole

    try:
        agent_oid = PydanticObjectId(agent_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("agent", agent_id) from exc

    doc = await _AgentDoc.get(agent_oid)
    if doc is None:
        raise NotFound("agent", agent_id)

    user_id = str(user.id)
    if doc.owner == user_id:
        return True
    try:
        role = resolve_workspace_role(user, doc.workspace)
    except GuardForbidden:
        return False
    return role.level >= WorkspaceRole.ADMIN.level


async def get_workspace(agent_id: str) -> str | None:
    """Return the agent's workspace id, or ``None`` if the agent doesn't
    exist. Used by the deny-log path of ``require_agent_owner_or_admin``."""
    try:
        agent_oid = PydanticObjectId(agent_id)
    except Exception:
        return None
    doc = await _AgentDoc.get(agent_oid)
    return doc.workspace if doc else None


async def get_persona(agent_id: str) -> str | None:
    """Return the agent's persona snippet for relevance/smart checks.

    Resolves to ``soul_persona`` when set, falling back to ``system_prompt``
    and finally the agent's display name. Returns ``None`` if the agent
    doesn't exist (callers degrade silently).
    """
    try:
        doc = await _AgentDoc.get(PydanticObjectId(agent_id))
    except Exception:
        return None
    if doc is None:
        return None
    return doc.config.soul_persona or doc.config.system_prompt or doc.name


async def seed_default_agent(
    workspace_id: str, owner_id: str
) -> tuple[_AgentDoc, bool] | tuple[None, bool]:
    """Create the default ``pocketpaw`` Agent for a workspace if missing.

    The frontend uses this agent's id as the DM room identifier and Session
    docs for DMs carry ``agent=<this agent's id>`` so per-agent history works.

    Idempotent. Returns ``(agent, created)`` — ``created`` is ``True`` only
    when this call inserted a new row, so back-fill paths can report
    accurate counts. Returns ``(None, False)`` if the insert raises (callers
    are expected to wrap in try/except).
    """
    existing = await _AgentDoc.find_one(
        _AgentDoc.workspace == workspace_id, _AgentDoc.slug == "pocketpaw"
    )
    if existing is not None:
        return existing, False

    agent = _AgentDoc(
        workspace=workspace_id,
        name="PocketPaw",
        slug="pocketpaw",
        avatar="",
        owner=owner_id,
        visibility="workspace",
        config=_AgentConfigDoc(
            system_prompt=(
                "You are PocketPaw — the default assistant in this workspace. "
                "Help the user with their tasks. Be concise, accurate, and honest."
            ),
            soul_persona="PocketPaw",
        ),
    )
    await agent.insert()
    logger.info(
        "Default 'pocketpaw' agent seeded in workspace %s (id: %s)",
        workspace_id,
        agent.id,
    )
    return agent, True


async def ensure_default_agent_all_workspaces() -> int:
    """Back-fill the pocketpaw agent for every existing workspace.

    Called on every boot so the DM target exists regardless of install age.
    Returns the number of agents actually created this run.
    """
    from ee.cloud.models.workspace import Workspace as _WorkspaceDoc

    seeded = 0
    async for ws in _WorkspaceDoc.find_all():
        try:
            _, created = await seed_default_agent(str(ws.id), str(ws.owner))
            if created:
                seeded += 1
        except Exception as exc:
            logger.warning("Failed to back-fill pocketpaw agent for ws=%s: %s", ws.id, exc)
    return seeded


__all__ = [
    "create",
    "delete",
    "discover",
    "ensure_default_agent_all_workspaces",
    "get",
    "get_by_slug",
    "get_persona",
    "get_scopes",
    "get_workspace",
    "is_owner_or_workspace_admin",
    "legacy_ctx",
    "list_agents",
    "seed_default_agent",
    "set_scopes",
    "suggest_for_mentions",
    "update",
]
