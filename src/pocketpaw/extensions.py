"""Extension-point Protocols for pluggable PocketPaw functionality.

Core (`pocketpaw`) defines narrow `typing.Protocol` contracts here. Concrete
implementations live in `pocketpaw_ee` (and potentially third-party packages)
and are discovered at runtime via `importlib.metadata.entry_points` — see
`pocketpaw._registry`. Core code must never `import pocketpaw_ee` directly;
the OSS-EE split (open-core) depends on that boundary, and an `import-linter`
contract enforces it.

Each Protocol documents the entry-point group that carries its implementations.
An entry-point points at a zero-arg callable (usually the class itself) that
the registry instantiates once and caches.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventBusProvider(Protocol):
    """Entry-point group: ``pocketpaw.event_bus``

    Supplies the process-wide async event bus. When no provider is
    registered, core falls back to a local in-process bus.
    """

    def get_event_bus(self) -> Any: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Entry-point group: ``pocketpaw.embeddings``

    Builds a text/image embedder for knowledge-base retrieval.
    """

    def build_embedder(self, settings: Any) -> Any: ...


@runtime_checkable
class MemoryBackendProvider(Protocol):
    """Entry-point group: ``pocketpaw.memory_backends``

    Supplies an alternative `MemoryStoreProtocol` implementation keyed by
    ``name`` (e.g. ``"mongodb"``). Core ships ``file`` and ``mem0``.
    """

    name: str

    def build(self, settings: Any) -> Any: ...


@runtime_checkable
class CapabilityProvider(Protocol):
    """Entry-point group: ``pocketpaw.capabilities``

    Reports which optional capabilities an install has. Core's
    ``features.py`` merges every provider's map into the capability registry.
    """

    def capabilities(self) -> dict[str, bool]: ...


@runtime_checkable
class AuthProvider(Protocol):
    """Entry-point group: ``pocketpaw.auth``

    Supplies FastAPI auth dependencies for routes that behave differently
    under multi-tenant cloud auth vs the single-tenant dashboard.
    """

    def current_optional_user(self) -> Any: ...


@runtime_checkable
class StoreProvider(Protocol):
    """Entry-point group: ``pocketpaw.stores``

    Overrides the default singleton store factories (instinct, fabric, …).
    Core ships local SQLite-backed defaults; EE swaps in cloud-backed ones.
    """

    def get_store(self, name: str) -> Any: ...


@runtime_checkable
class RouteProvider(Protocol):
    """Entry-point group: ``pocketpaw.routes``

    Mounts additional sub-applications / routers onto the dashboard app
    (the multi-tenant cloud API).
    """

    def mount(self, app: Any) -> None:
        """Mount sub-applications / routers onto *app*. Called early, before
        the core v1 routers, so EE routes take priority."""
        ...


@runtime_checkable
class LifecycleHook(Protocol):
    """Entry-point group: ``pocketpaw.lifecycle``

    Startup/shutdown hooks run by the dashboard lifecycle. Used for things
    like initializing the cloud database or registering event listeners.

    Hooks take no arguments — the dashboard lifecycle (``startup_event`` in
    ``dashboard_lifecycle.py``) has no FastAPI ``app`` reference to thread
    through, and the cloud hook resolves what it needs from the environment.
    """

    async def on_startup(self) -> None: ...

    async def on_shutdown(self) -> None: ...


@runtime_checkable
class StorageBackend(Protocol):
    """Entry-point group: ``pocketpaw.storage_backends``

    Supplies a file-storage adapter + metadata store for upload resolution.
    """

    name: str

    def adapter(self) -> Any: ...

    def meta(self) -> Any: ...


@runtime_checkable
class ModelProvider(Protocol):
    """Entry-point group: ``pocketpaw.models``

    Exposes Beanie document classes (cloud entities: Agent, Session, User,
    Workspace, …) so core code can look them up by name without importing
    `pocketpaw_ee`. Returns ``None`` for unknown names.
    """

    def get_model(self, name: str) -> type | None: ...


@runtime_checkable
class McpServerProvider(Protocol):
    """Entry-point group: ``pocketpaw.mcp_servers``

    Builds in-process MCP servers exposed to agent backends. Each provider
    returns ``(name, config_entry)`` or ``None`` when its feature is
    unavailable in the current context.
    """

    def build_server(self) -> tuple[str, Any] | None: ...

    def tool_ids(self) -> list[str]:
        """Tool ids this server exposes (for allowlist matching)."""
        ...


@runtime_checkable
class AgentExtension(Protocol):
    """Entry-point group: ``pocketpaw.agent_extensions``

    Extends the core agent runtime with EE-only capabilities: extra
    function-tools for tool-list backends (deep_agents, openai_agents, …)
    and environment variables carrying cloud identity for subprocess (CLI)
    backends. An OSS install registers no provider and the runtime runs
    with only its built-in tools and a bare subprocess environment.
    """

    def agent_tools(self, backend: str) -> list[Any]:
        """Extra tool instances to expose to *backend*, or ``[]``."""
        ...

    def subprocess_env(self) -> dict[str, str]:
        """Environment variables to inject into agent subprocesses —
        cloud workspace/user/session identity. ``{}`` when no cloud
        context is active (the common OSS / out-of-stream case)."""
        ...


@runtime_checkable
class PocketWriter(Protocol):
    """Entry-point group: ``pocketpaw.pockets``

    Persists a pocket created by an agent. The cloud product writes a
    Pocket document and links it to the chat Session in MongoDB; an OSS
    install registers no provider, so agent-emitted pocket specs fan out
    as SSE events only and are never persisted.
    """

    async def create_pocket_and_session(
        self,
        spec: dict,
        session_key: str,
        user_id: str | None,
        workspace_id: str | None,
    ) -> str | None:
        """Create the pocket + linked session. Returns the new pocket id,
        or ``None`` on failure."""
        ...


@runtime_checkable
class ComposioToolProvider(Protocol):
    """Entry-point group: ``pocketpaw.composio_tools``

    Fetches Composio integration tools for an agent backend. Composio
    wires 200+ pre-built OAuth integrations (Gmail, Slack, GitHub,
    Calendar, Drive, …); the cloud product fetches them per-stream and
    returns them in the tool format native to *backend*. An OSS install
    registers no provider, so agents run with only their built-in tools.
    """

    def build_tools(self, backend: str, settings: Any) -> list[Any]:
        """Composio tool instances for *backend*, or ``[]`` when Composio
        is not configured / not applicable."""
        ...
