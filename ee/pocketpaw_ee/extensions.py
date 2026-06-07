"""Entry-point provider classes for the OSS-EE extension surfaces.

Core (`pocketpaw`) defines the Protocols in `pocketpaw.extensions` and
discovers implementations via `importlib.metadata.entry_points`. This module
collects every `pocketpaw_ee` provider in one place; the entry-points that
point at these classes are declared in `pyproject.toml` (and will migrate to
`ee/pyproject.toml` in Phase 4).

Each provider does its heavy `pocketpaw_ee` imports lazily inside methods so
that merely loading this module — which the registry does on first access —
stays cheap and free of import cycles.
"""

from __future__ import annotations

from typing import Any


class CloudEventBusProvider:
    """`pocketpaw.event_bus` — the process-wide async pub/sub bus."""

    def get_event_bus(self) -> Any:
        from pocketpaw_ee.cloud.shared.events import event_bus

        return event_bus


class CloudEmbeddingProvider:
    """`pocketpaw.embeddings` — KB text/image embedder factory."""

    def build_embedder(self, settings: Any) -> Any:
        from pocketpaw_ee.cloud.embeddings import build_embedder

        return build_embedder(settings)


class MongoMemoryBackendProvider:
    """`pocketpaw.memory_backends` — MongoDB-backed memory store."""

    name = "mongodb"

    def build(self, settings: Any) -> Any:
        from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

        return MongoMemoryStore()


class CloudCapabilityProvider:
    """`pocketpaw.capabilities` — features the cloud product force-enables."""

    def capabilities(self) -> dict[str, bool]:
        from pocketpaw_ee.cloud import features

        return {"chat_titles_enabled": features.chat_titles_enabled()}


class CloudAuthProvider:
    """`pocketpaw.auth` — FastAPI auth dependencies for cloud-mounted routes."""

    def current_optional_user(self) -> Any:
        from pocketpaw_ee.cloud.auth.core import current_optional_user

        return current_optional_user


class CloudRouteProvider:
    """`pocketpaw.routes` — mounts the multi-tenant cloud API."""

    def mount(self, app: Any) -> None:
        from pocketpaw_ee.cloud import mount_cloud

        mount_cloud(app)


class CloudLifecycleHook:
    """`pocketpaw.lifecycle` — cloud DB init + admin/workspace seeding +
    chat-title listener registration, run on dashboard startup."""

    async def on_startup(self) -> None:
        import logging
        import os

        logger = logging.getLogger(__name__)

        from pocketpaw_ee.cloud.db import init_cloud_db

        mongo_uri = os.environ.get("CLOUD_MONGODB_URI", "mongodb://localhost:27017/paw-enterprise")
        await init_cloud_db(mongo_uri)

        from pocketpaw_ee.cloud.auth.core import (
            ensure_default_agent_all_workspaces,
            seed_admin,
            seed_workspace,
        )

        admin = await seed_admin()
        await seed_workspace(admin)
        # Back-fill the pocketpaw agent for workspaces that predate agent seeding.
        await ensure_default_agent_all_workspaces()

        # Persist Haiku-generated chat titles into MongoDB.
        try:
            from pocketpaw_ee.cloud.sessions.title_listener import (
                register as register_title_listener,
            )

            register_title_listener()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cloud chat-title listener registration failed: %s", exc)

    async def on_shutdown(self) -> None:
        # Cloud teardown is handled inside mount_cloud's own shutdown hook.
        return None


class CloudStorageBackend:
    """`pocketpaw.storage_backends` — the EE Mongo-backed upload store."""

    name = "cloud"

    def adapter(self) -> Any:
        from pocketpaw_ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER

    def meta(self) -> Any:
        from pocketpaw_ee.cloud.uploads.router import _META

        return _META


class CloudModelProvider:
    """`pocketpaw.models` — cloud Beanie document classes resolved by name.

    Core looks up ``Agent`` (the only cloud model it references after the
    Phase 3b split — the agent pool / per-agent loop cache). Other cloud
    entities are imported directly within `pocketpaw_ee`.
    """

    def get_model(self, name: str) -> type | None:
        if name == "Agent":
            from pocketpaw_ee.cloud.models.agent import Agent

            return Agent
        return None


class CloudPocketWriter:
    """`pocketpaw.pockets` — persists agent-created pockets to MongoDB."""

    async def create_pocket_and_session(
        self,
        spec: dict,
        session_key: str,
        user_id: str | None,
        workspace_id: str | None,
    ) -> str | None:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        return await pockets_service.create_pocket_and_session(
            spec, session_key, user_id, workspace_id
        )


class CloudTasksMcpProvider:
    """`pocketpaw.mcp_servers` — the Mission Control Tasks in-process server."""

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.tasks import build_tasks_context_server

        return build_tasks_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.tasks import TASK_TOOL_IDS

        return list(TASK_TOOL_IDS)


class CloudPlannerMcpProvider:
    """`pocketpaw.mcp_servers` — the cloud Planner in-process server."""

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.planner import build_planner_context_server

        return build_planner_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.planner import PLANNER_TOOL_IDS

        return list(PLANNER_TOOL_IDS)


class CloudPocketMcpProvider:
    """`pocketpaw.mcp_servers` — the cloud pocket-context in-process server."""

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.pockets import build_pocket_context_server

        return build_pocket_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.pockets import POCKET_TOOL_IDS

        return list(POCKET_TOOL_IDS)


class CloudPocketSpecialistMcpProvider:
    """`pocketpaw.mcp_servers` — the pocket specialist (create/edit) server."""

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.pocket_specialist.mcp_tool import (
                SERVER_NAME,
                build_pocket_specialist_server,
            )

            return SERVER_NAME, build_pocket_specialist_server()
        except ImportError:
            # claude_agent_sdk not installed — the specialist server is
            # unavailable, same as the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import POCKET_SPECIALIST_TOOL_IDS

        return list(POCKET_SPECIALIST_TOOL_IDS)


class CloudAgentExtension:
    """`pocketpaw.agent_extensions` — EE additions to the core agent runtime.

    Contributes the cloud pocket-specialist function tool to MCP-capable
    tool-list backends, and cloud workspace/user/session identity to agent
    subprocess environments.
    """

    # Backends that receive ``PocketSpecialistTool`` as a native function
    # tool. Shell-CLI backends (codex_cli, opencode, copilot_sdk) use the
    # cloud_pocket_specialist_create CLI command instead; claude_agent_sdk
    # uses its own in-process specialist MCP server — surfacing the tool
    # through the function-tool bridge for either would advertise a name
    # their dispatcher can't resolve.
    _SPECIALIST_FUNCTION_TOOL_BACKENDS = frozenset({"deep_agents", "google_adk", "openai_agents"})

    def agent_tools(self, backend: str) -> list[Any]:
        if backend not in self._SPECIALIST_FUNCTION_TOOL_BACKENDS:
            return []
        try:
            from pocketpaw_ee.agent.pocket_specialist.tool import PocketSpecialistTool

            return [PocketSpecialistTool()]
        except Exception:  # noqa: BLE001
            return []

    def subprocess_env(self) -> dict[str, str]:
        try:
            from pocketpaw_ee.cloud.chat.agent_service import (
                current_session_mongo_id,
                current_user_id,
                current_workspace_id,
            )
        except Exception:  # noqa: BLE001
            return {}
        env: dict[str, str] = {}
        for var, fn in (
            ("POCKETPAW_WORKSPACE_ID", current_workspace_id),
            ("POCKETPAW_USER_ID", current_user_id),
            ("POCKETPAW_SESSION_ID", current_session_mongo_id),
        ):
            value = fn()
            if value:
                env[var] = str(value)
        return env


class CloudComposioToolProvider:
    """`pocketpaw.composio_tools` — Composio integration tools for the
    parent cloud chat agent.

    Composio supplies 200+ pre-built OAuth integrations (Gmail, Slack,
    GitHub, Calendar, Drive, …). Tools are fetched per-stream via the
    official Composio provider package for the requesting backend and
    returned in that backend's native tool format. The pocket specialist
    deliberately does not receive Composio — the parent agent fetches the
    data and passes it down in the brief.
    """

    def build_tools(self, backend: str, settings: Any) -> list[Any]:
        from pocketpaw_ee.cloud.composio.providers import build_tools_for_backend

        return list(build_tools_for_backend(backend, settings=settings))


class CloudComposioMcpProvider:
    """`pocketpaw.mcp_servers` — Composio integrations for the
    ``claude_agent_sdk`` backend, exposed as an in-process MCP server.

    The other agent backends consume Composio as native function tools
    via ``CloudComposioToolProvider``; ``claude_agent_sdk`` instead
    discovers MCP servers, so Composio reaches it this way. ``build_server``
    runs once per ``_get_mcp_servers`` call (i.e. per stream) and the
    tools are bound to the active user via Composio's per-user sessions,
    so the server stays multi-tenant safe.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from claude_agent_sdk import create_sdk_mcp_server

            from pocketpaw_ee.cloud.composio.providers import build_tools_for_backend
        except ImportError:
            # claude_agent_sdk / composio provider package not installed.
            return None
        try:
            tools = build_tools_for_backend("claude_agent_sdk")
        except Exception:  # noqa: BLE001
            return None
        if not tools:
            return None
        return "composio", create_sdk_mcp_server(name="composio", version="1.0.0", tools=tools)

    def tool_ids(self) -> list[str]:
        # Composio's tool set is per-user and per-toolkit (resolved at
        # session-build time), so it can't be statically enumerated.
        # ``mcp__composio`` is the server-level allowlist entry — it
        # permits every tool the in-process ``composio`` server exposes.
        return ["mcp__composio"]
