"""Agents domain — business logic service."""

from __future__ import annotations

from beanie import PydanticObjectId

from ee.cloud.agents.schemas import (
    CreateAgentRequest,
    DiscoverRequest,
    UpdateAgentRequest,
)
from ee.cloud.models.agent import Agent, AgentConfig
from ee.cloud.shared.errors import ConflictError, Forbidden, NotFound


def _agent_response(agent: Agent) -> dict:
    """Build a frontend-compatible dict from an Agent document."""
    return {
        "_id": str(agent.id),
        "workspace": agent.workspace,
        "name": agent.name,
        "uname": agent.slug,
        "avatar": agent.avatar,
        "visibility": agent.visibility,
        "config": agent.config.model_dump(),
        "owner": agent.owner,
        "createdOn": agent.createdAt.isoformat() if agent.createdAt else None,
        "lastUpdatedOn": agent.updatedAt.isoformat() if agent.updatedAt else None,
    }


class AgentService:
    """Stateless service encapsulating agent business logic."""

    @staticmethod
    async def create(workspace_id: str, user_id: str, body: CreateAgentRequest) -> dict:
        """Create an agent with slug uniqueness within the workspace."""
        existing = await Agent.find_one(
            Agent.workspace == workspace_id,
            Agent.slug == body.slug,
        )
        if existing:
            raise ConflictError(
                "agent.slug_taken",
                f"Slug '{body.slug}' is already in use in this workspace",
            )

        config_data: dict = {
            "backend": body.backend,
            "model": body.model,
            "system_prompt": body.system_prompt,
            "soul_enabled": body.soul_enabled,
            "soul_persona": body.persona,
            "soul_archetype": body.soul_archetype or f"The {body.name}",
        }
        if body.temperature is not None:
            config_data["temperature"] = body.temperature
        if body.max_tokens is not None:
            config_data["max_tokens"] = body.max_tokens
        if body.tools is not None:
            config_data["tools"] = body.tools
        if body.trust_level is not None:
            config_data["trust_level"] = body.trust_level
        if body.soul_values is not None:
            config_data["soul_values"] = body.soul_values
        if body.soul_ocean is not None:
            config_data["soul_ocean"] = body.soul_ocean
        config = AgentConfig(**config_data)

        agent = Agent(
            workspace=workspace_id,
            name=body.name,
            slug=body.slug,
            avatar=body.avatar,
            visibility=body.visibility,
            config=config,
            owner=user_id,
        )
        await agent.insert()
        return _agent_response(agent)

    @staticmethod
    async def list_agents(workspace_id: str, query: str | None = None) -> list[dict]:
        """List agents in a workspace with optional name search."""
        filters: dict = {"workspace": workspace_id}
        if query:
            filters["name"] = {"$regex": query, "$options": "i"}

        agents = await Agent.find(filters).to_list()
        return [_agent_response(a) for a in agents]

    @staticmethod
    async def get(agent_id: str) -> dict:
        """Get a single agent by ID. Raises NotFound if missing."""
        agent = await Agent.get(PydanticObjectId(agent_id))
        if not agent:
            raise NotFound("agent", agent_id)
        return _agent_response(agent)

    @staticmethod
    async def get_by_slug(workspace_id: str, slug: str) -> dict:
        """Find an agent by slug within a workspace."""
        agent = await Agent.find_one(
            Agent.workspace == workspace_id,
            Agent.slug == slug,
        )
        if not agent:
            raise NotFound("agent", slug)
        return _agent_response(agent)

    @staticmethod
    async def update(agent_id: str, user_id: str, body: UpdateAgentRequest) -> dict:
        """Update agent fields. Owner only."""
        agent = await Agent.get(PydanticObjectId(agent_id))
        if not agent:
            raise NotFound("agent", agent_id)
        if agent.owner != user_id:
            raise Forbidden("agent.not_owner", "Only the agent owner can update it")

        if body.name is not None:
            agent.name = body.name
        if body.avatar is not None:
            agent.avatar = body.avatar
        if body.visibility is not None:
            agent.visibility = body.visibility
        if body.config is not None:
            agent.config = AgentConfig(**body.config)
        else:
            # Apply individual config/soul field overrides
            current = agent.config.model_dump()
            changed = False
            for field, attr in [
                ("backend", body.backend),
                ("model", body.model),
                ("system_prompt", body.system_prompt),
                ("temperature", body.temperature),
                ("max_tokens", body.max_tokens),
                ("tools", body.tools),
                ("trust_level", body.trust_level),
                ("soul_enabled", body.soul_enabled),
                ("soul_archetype", body.soul_archetype),
                ("soul_values", body.soul_values),
                ("soul_ocean", body.soul_ocean),
            ]:
                if attr is not None:
                    current[field] = attr
                    changed = True
            if body.persona is not None:
                current["soul_persona"] = body.persona
                changed = True
            if changed:
                agent.config = AgentConfig(**current)

        await agent.save()
        return _agent_response(agent)

    @staticmethod
    async def delete(agent_id: str, user_id: str) -> None:
        """Hard-delete an agent. Owner only."""
        agent = await Agent.get(PydanticObjectId(agent_id))
        if not agent:
            raise NotFound("agent", agent_id)
        if agent.owner != user_id:
            raise Forbidden("agent.not_owner", "Only the agent owner can delete it")

        await agent.delete()

    @staticmethod
    async def discover(workspace_id: str, user_id: str, body: DiscoverRequest) -> list[dict]:
        """Paginated agent discovery with visibility filtering.

        Visibility rules:
        - private: only the requesting user's own agents
        - workspace: all agents in the workspace
        - public: all public agents (across workspaces)
        """
        filters: dict = {}

        if body.visibility == "private":
            filters["workspace"] = workspace_id
            filters["owner"] = user_id
        elif body.visibility == "workspace":
            filters["workspace"] = workspace_id
        elif body.visibility == "public":
            filters["visibility"] = "public"
        else:
            # Default: user's own agents + workspace-visible + public
            filters["$or"] = [
                {"workspace": workspace_id, "owner": user_id},
                {"workspace": workspace_id, "visibility": "workspace"},
                {"visibility": "public"},
            ]

        if body.query:
            filters["name"] = {"$regex": body.query, "$options": "i"}

        skip = (body.page - 1) * body.page_size
        agents = await Agent.find(filters).skip(skip).limit(body.page_size).to_list()
        return [_agent_response(a) for a in agents]
