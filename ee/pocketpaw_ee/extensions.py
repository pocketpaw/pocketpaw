"""Entry-point provider classes for the OSS-EE extension surfaces.

Core (`pocketpaw`) defines the Protocols in `pocketpaw.extensions` and
discovers implementations via `importlib.metadata.entry_points`. This module
collects every `pocketpaw_ee` provider in one place; the entry-points that
point at these classes are declared in `pyproject.toml` (and will migrate to
`ee/pyproject.toml` in Phase 4).

Each provider does its heavy `pocketpaw_ee` imports lazily inside methods so
that merely loading this module — which the registry does on first access —
stays cheap and free of import cycles.

Updated: 2026-05-22 (RFC 04 M3) — ``CloudLifecycleHook`` now starts the
pocket interval-refresh scheduler in ``on_startup`` and cancels it in
``on_shutdown``. The scheduler is a single asyncio task owned at module
scope inside ``cloud.pockets.refresh_scheduler``; it is self-gated on
``POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED`` so the start call is a
no-op unless a deployment opts in.

Updated: 2026-05-28 (feat/wave-3d-temporal-scheduler) — ``CloudLifecycleHook``
also starts the RFC 03 v2 temporal trigger sweep scheduler in
``on_startup`` and cancels it in ``on_shutdown``. The scheduler lives at
``cloud._core.temporal_scheduler`` and is self-gated on
``POCKETPAW_TEMPORAL_SWEEP_ENABLED`` (default OFF) so pytest runs and
multi-replica deployments don't double-fire. Cadence is configurable via
``POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS`` (default 3600, floor 60).

Updated: 2026-06-10 (feat/studio-code-migration) — added ``CloudMediaMcpProvider``
(``pocketpaw.mcp_servers`` entry ``media``) exposing the STUDIO image +
video generation in-process server (``pocketpaw_media``) to the
claude_agent_sdk cloud chat backend, mirroring ``CloudSitesMcpProvider``.

Updated: 2026-06-10 (feat/belt-loom-mcp, BS-1) — added ``CloudLoomMcpProvider``
(``pocketpaw.mcp_servers`` entry ``loom``) registering the external loom
codebase-orientation binary as a STDIO MCP server (server name ``loom``;
5 read tools: orient / locate / why / what_depends_on / boundaries) on the
claude_agent_sdk cloud chat backend. Unlike the sibling providers this one
returns a stdio config DICT (Path A), not an in-process SDK server object —
the registration loop passes it through untouched. Ambient (not opt-in); the
/belt surface scopes access via its profile allowlist. Returns None — and the
loop skips it — when ``loom_model_path`` is unset or the binary is missing,
so chat never breaks.

Updated: 2026-06-10 (feat/belt-gate, BS-3) — added ``CloudBeltMcpProvider``
(``pocketpaw.mcp_servers`` entry ``belt``) exposing the Belt & Pulley
code-change gate in-process server (``pocketpaw_belt``; one tool
``belt_propose_change``) to the claude_agent_sdk cloud chat backend, mirroring
``CloudMediaMcpProvider``. The develop station proposes a diff through Instinct;
the ee instinct router fires ``ee.cloud.belt.executor.execute_approved_change``
on approval. Ambient (not opt-in).

Updated: 2026-06-11 (feat/external-action-mcp-tool) — added
``CloudExternalActionsMcpProvider`` (``pocketpaw.mcp_servers`` entry
``external_actions``) exposing the gated external-action proposal server
(``pocketpaw_external_actions``; one tool ``propose_external_action``) to the
claude_agent_sdk cloud chat backend, mirroring ``CloudBeltMcpProvider``. A chat
agent proposes a connector call through Instinct; the ee instinct router fires
``ee.cloud.external_actions.executor.execute_approved_external_action`` on
approval. Propose-only — the tool never fires the connector itself. Ambient
(not opt-in).

Updated: 2026-06-12 (connector-store-unification CS-3) — added
``CloudConnectorStateStoreProvider`` (``pocketpaw.connector_state_stores``)
supplying the ``WorkspaceConnector``-backed ``CloudConnectorStateStore`` as the
ConnectorRegistry's default durable state store, so cloud connector config
rehydrates from the tenant DB after a process restart (no /connect needed).

Updated: 2026-06-26 (ART-2) — ``CloudAgentExtension`` gained ``agent_cwd``: a
per-tenant agent working-directory jail. It delegates to
``pocketpaw_ee.cloud.agent_jail.resolve_agent_cwd``, which returns a
per-workspace/session dir (``~/.pocketpaw/workspaces/<ws>/agent/<session>/``) so
a cloud tenant's file ops never co-mingle in the shared home dir, and FAILS
CLOSED when a cloud run has no resolvable workspace. OSS / dedicated installs
return ``None`` and keep ``settings.file_jail_path``.

Updated: 2026-06-11 (feat/fabric-instinct-mcp-providers) — added
``CloudFabricMcpProvider`` (entry ``fabric``; server ``pocketpaw_fabric``,
tools ``fabric_query`` / ``fabric_stats``) and ``CloudInstinctMcpProvider``
(entry ``instinct``; server ``pocketpaw_instinct``, tools ``instinct_pending``
/ ``instinct_audit``), both mirroring ``CloudExternalActionsMcpProvider``. On
the claude_agent_sdk backend, registry tools (BaseTool) never reach the agent —
only MCP servers do — so without these the cloud chat agent had no path to the
Fabric ontology or Instinct gate visibility. Both are READ-ONLY and
workspace-scoped via the chat ContextVars. Gated proposing stays on
``pocketpaw_external_actions``. Ambient (not opt-in).

Updated: 2026-06-26 (ISO-3 — workspace store bridge) — added ``CloudStoreProvider``
(``pocketpaw.stores`` entry-point) so the dormant ``StoreProvider`` seam ISO-1 lit
up is now live under EE. It returns the standard per-workspace SQLite file store
(Fabric / Instinct at ``~/.pocketpaw/workspaces/<id>/<name>.db``) by delegating to
the OSS helper ``pocketpaw.stores.build_workspace_store`` — so the path + the
path-traversal allowlist stay authoritative in OSS and the provider can never drift
from or weaken them. It returns ``None`` for the legacy (no-workspace) path, leaving
the OSS factory's shared singleton in place. This activates the entry-point end to
end and gives EE the single hook to later swap in a cloud-backed store without
touching core.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

_run_sweeper_logger = logging.getLogger(__name__)
_sweeper_task: asyncio.Task[None] | None = None
_xproc_consumer_task: asyncio.Task[None] | None = None


async def _sweeper_loop() -> None:
    from pocketpaw_ee.cloud.agent_jail_gc import sweep_agent_jails
    from pocketpaw_ee.cloud.chat.runs.sweeper import sweep_stale_runs
    from pocketpaw_ee.cloud.llm_provisioning.cutover_sweeper import run_cutover_sweep
    from pocketpaw_ee.cloud.metering.sweeper import sweep_unbilled_runs
    from pocketpaw_ee.sites.pending_sweeper import sweep_pending_sites

    while True:
        try:
            await asyncio.sleep(300)
            await sweep_stale_runs()
        except asyncio.CancelledError:
            raise
        except Exception:
            _run_sweeper_logger.exception("sweep_stale_runs tick failed")
        # ART-3 jail lifecycle: TTL-GC idle agent jails + LRU-evict under the
        # disk watermark, so scratch disk scales with active concurrency not user
        # count. Own try so a jail-GC failure can't suppress the other sweeps (or
        # vice versa); never evicts a jail backing a queued/running run.
        try:
            await sweep_agent_jails()
        except Exception:
            _run_sweeper_logger.exception("sweep_agent_jails tick failed")
        # BC-3 metering: bill every newly-terminal run's compute cost on the same
        # heartbeat. Kept in its own try so a metering failure can't suppress the
        # stale-run sweep (or vice versa) on the next tick. Self-gated OFF in the
        # WU-F ``live`` cutover mode (LiteLLM is then the sole meter).
        try:
            await sweep_unbilled_runs()
        except Exception:
            _run_sweeper_logger.exception("sweep_unbilled_runs tick failed")
        # WU-F billing cutover: per-tenant LiteLLM spend sweep. No-op in ``off``;
        # a read-only reconciliation compare in ``shadow``; debits proxy spend in
        # ``live``. Own try so a cutover-sweep failure can't suppress the other
        # sweeps (or vice versa) on the next tick.
        try:
            await run_cutover_sweep()
        except Exception:
            _run_sweeper_logger.exception("run_cutover_sweep tick failed")
        # Charge-first (review fix C): surface PAID sites stuck pending past the
        # threshold (a lost/delayed subscription.active webhook). VISIBILITY ONLY —
        # logs at WARNING, never auto-deploys or auto-cancels. Its own try so a
        # failure here can't suppress the other sweeps (or vice versa).
        try:
            await sweep_pending_sites()
        except Exception:
            _run_sweeper_logger.exception("sweep_pending_sites tick failed")


async def start_run_sweeper() -> None:
    """Sweep once on boot, then tick every 5 minutes until shutdown.

    Boot runs the stale-run sweep (interrupt orphaned runs), the BC-3 compute-cost
    metering sweep (bill any terminal runs left unbilled by the prior process), the
    WU-F LiteLLM billing-cutover sweep (no-op / shadow-compare / live-ingest per the
    cutover mode), the charge-first pending-site reconciliation sweep (surface paid
    sites stuck pending), and the ART-3 agent-jail GC (reclaim scratch left by a
    prior process's idle runs); the 5-minute loop then ticks all of them.
    """
    from pocketpaw_ee.cloud.agent_jail_gc import sweep_agent_jails
    from pocketpaw_ee.cloud.chat.runs.sweeper import sweep_stale_runs
    from pocketpaw_ee.cloud.llm_provisioning.cutover_sweeper import run_cutover_sweep
    from pocketpaw_ee.cloud.metering.sweeper import sweep_unbilled_runs
    from pocketpaw_ee.sites.pending_sweeper import sweep_pending_sites

    global _sweeper_task
    with suppress(Exception):
        await sweep_stale_runs()
    with suppress(Exception):
        await sweep_unbilled_runs()
    with suppress(Exception):
        await run_cutover_sweep()
    with suppress(Exception):
        await sweep_pending_sites()
    with suppress(Exception):
        await sweep_agent_jails()
    _sweeper_task = asyncio.create_task(_sweeper_loop())


async def stop_run_sweeper() -> None:
    global _sweeper_task
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        with suppress(asyncio.CancelledError):
            await _sweeper_task
        _sweeper_task = None


async def start_xproc_consumer() -> None:
    """Start the cross-process bus/WS bridge consumer in the web process.

    No-op when ``POCKETPAW_REDIS_URL`` is unset — without Redis no Tier 2
    worker can publish to the bridge, and the existing in-process bus
    handles every emit locally. This keeps Tier 0 deployments quiet.
    """
    import os

    if not os.environ.get("POCKETPAW_REDIS_URL", "").strip():
        return
    from pocketpaw_ee.cloud._core.realtime.xproc import run_consumer

    global _xproc_consumer_task
    _xproc_consumer_task = asyncio.create_task(run_consumer())


async def stop_xproc_consumer() -> None:
    global _xproc_consumer_task
    if _xproc_consumer_task is not None:
        _xproc_consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await _xproc_consumer_task
        _xproc_consumer_task = None


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

        # Start meeting reminder + auto-start background loop.
        try:
            from pocketpaw_ee.cloud.meetings.scheduling.reminders import start_reminder_loop

            start_reminder_loop()
            logger.info("Meeting reminder + auto-start loop started")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start meeting reminder loop: %s", exc)

        # Re-index meeting transcripts that were ingested under an older
        # KB-extraction pipeline (pre-VTT-cleaner). Fire-and-forget, gated
        # by per-row version markers so repeated boots are no-ops.
        try:
            import asyncio

            from pocketpaw_ee.cloud.meetings.service import reindex_outdated_transcripts

            async def _reindex_meeting_transcripts() -> None:
                try:
                    summary = await reindex_outdated_transcripts()
                    if summary.get("republished"):
                        logger.info(
                            "Meeting transcript KB reindex complete: %s",
                            summary,
                        )
                except Exception:
                    logger.exception("meeting transcript reindex failed")

            asyncio.create_task(_reindex_meeting_transcripts())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to schedule transcript reindex: %s", exc)
        # Pocket interval-refresh scheduler (RFC 04 M3). A single asyncio
        # task that periodically re-runs pocket data sources whose refresh
        # policy includes `"interval"`. Self-gated on
        # POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED (default OFF — a
        # pytest run never spawns it; a multi-replica deploy runs it on
        # exactly one replica). The task lives at module scope inside the
        # scheduler so this no-`app` lifecycle hook can still own it.
        try:
            from pocketpaw_ee.cloud.pockets.refresh_scheduler import start_scheduler

            await start_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pocket interval-refresh scheduler start failed: %s", exc)

        # RFC 03 v2 temporal trigger sweep scheduler (Wave 3d). A single
        # asyncio task that periodically sweeps every pocket with a
        # ``type: temporal`` trigger, detects rising edges, and
        # dispatches the trigger's action through the Wave 3a gate.
        # Self-gated on ``POCKETPAW_TEMPORAL_SWEEP_ENABLED`` (default
        # OFF — a pytest run never spawns it; a multi-replica deploy
        # runs it on exactly one replica). The task lives at module
        # scope inside the scheduler so this no-``app`` lifecycle hook
        # can still own it.
        try:
            from pocketpaw_ee.cloud._core.temporal_scheduler import (
                start_scheduler as start_temporal_scheduler,
            )

            await start_temporal_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Temporal sweep scheduler start failed: %s", exc)

        # Stale-run sweeper. Marks queued/running ChatRunDocs whose backend
        # process died as ``interrupted`` so clients render a retry instead
        # of subscribing to a stream nobody is writing to. One pass at boot
        # to catch runs left behind by the prior process, then a 5-minute
        # tick.
        try:
            await start_run_sweeper()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run sweeper start failed: %s", exc)

        # Cross-process bus/WS bridge consumer. Tier 2's arq worker can't
        # reach this process's InProcessBus or WsManager directly; it XADDs
        # envelopes to a shared Redis stream and the consumer dispatches
        # them locally. No-op without POCKETPAW_REDIS_URL.
        try:
            await start_xproc_consumer()
        except Exception as exc:  # noqa: BLE001
            logger.warning("xproc consumer start failed: %s", exc)

        # Re-serve locally-deployed Paw Sites. In LOCAL deploy mode the static
        # server binds an ephemeral port and is only started during publish, so
        # after this restart every prior site's stored url points at a dead
        # port even though its files survived on disk. reserve_local_sites()
        # (re)starts the shared server and rewrites every deployed site's url to
        # the fresh live base so previously-published local sites are openable
        # again with no manual re-publish. Unscoped (all workspaces) — this is
        # the automatic boot path. A no-op when real Cloudflare creds are present
        # (the CF path owns its own URLs). Guarded so a failure here never blocks
        # boot. Mirrors this lifecycle hook's other best-effort boot reconcilers
        # (run sweeper, xproc consumer) rather than mount_cloud's
        # ``@app.on_event("startup")``, which is silently dropped under
        # ``FastAPI(lifespan=...)`` (the host's default).
        try:
            from pocketpaw_ee.sites.service import reserve_local_sites

            reconciled = await reserve_local_sites()
            if reconciled:
                logger.info("Re-served %d local Paw Site(s) after restart", reconciled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Local Paw Sites re-serve failed (non-fatal): %s", exc)

        # Paw Sites live Vite dev-server reaper (Phase 2 / P2a). The DevServerManager
        # runs one long-lived `vite dev` per pocket being edited (HMR preview); the
        # idle reaper stops servers idle past DEV_SERVER_IDLE_SECONDS so editor
        # sessions that walk away never leak processes. Best-effort, like the other
        # boot reconcilers — a failure here must not block boot. The manager itself
        # is lazily created on the first dev-preview call; starting the reaper here
        # just ensures the idle sweep is ticking. on_shutdown stops it + every server.
        try:
            from pocketpaw_ee.sites.dev_server import start_dev_server_reaper

            await start_dev_server_reaper()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Paw Sites dev-server reaper start failed (non-fatal): %s", exc)

    async def on_shutdown(self) -> None:
        import logging

        logger = logging.getLogger(__name__)
        # Shut down the meeting APScheduler if it was started.
        try:
            from pocketpaw_ee.cloud.meetings.scheduling.reminders import shutdown_scheduler

            await shutdown_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Meeting scheduler shutdown error: %s", exc)

        # Most cloud teardown is handled inside mount_cloud's own shutdown
        # hook. The interval-refresh scheduler is owned by this lifecycle
        # hook (it was started in on_startup), so it is cancelled here so
        # the background task does not outlive the process.
        import logging

        logger = logging.getLogger(__name__)
        try:
            from pocketpaw_ee.cloud.pockets.refresh_scheduler import stop_scheduler

            await stop_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pocket interval-refresh scheduler stop failed: %s", exc)

        try:
            from pocketpaw_ee.cloud._core.temporal_scheduler import (
                stop_scheduler as stop_temporal_scheduler,
            )

            await stop_temporal_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Temporal sweep scheduler stop failed: %s", exc)

        try:
            await stop_run_sweeper()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run sweeper stop failed: %s", exc)

        try:
            await stop_xproc_consumer()
        except Exception as exc:  # noqa: BLE001
            logger.warning("xproc consumer stop failed: %s", exc)

        # Stop the Paw Sites dev-server reaper AND terminate every running `vite dev`
        # so no editor preview process outlives the web process (P2a resource safety).
        try:
            from pocketpaw_ee.sites.dev_server import stop_dev_servers

            await stop_dev_servers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Paw Sites dev-server stop failed: %s", exc)

        # Close the arq enqueuer pool if this web process ever built one
        # (POCKETPAW_CLOUD_RUN_EXECUTOR=arq). No-op otherwise.
        try:
            from pocketpaw_ee.cloud.chat.runs.arq_executor import close_pool

            await close_pool()
        except Exception as exc:  # noqa: BLE001
            logger.warning("arq pool close failed: %s", exc)
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


class CloudConnectorsMcpProvider:
    """`pocketpaw.mcp_servers` — the connector-execution in-process server.

    Exposes ``list_connector_actions`` + ``connector_execute`` so the cloud
    chat agent can READ from a pocket's bound connectors (GitHub issues/PRs,
    Gmail search, …). Read-first: write actions are blocked until v2. Ambient
    (not opt-in) so the M3-derived connector skills can reach it.
    """

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.connectors import build_connectors_context_server

        return build_connectors_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.connectors import CONNECTOR_TOOL_IDS

        return list(CONNECTOR_TOOL_IDS)


class CloudMeetingsMcpProvider:
    """`pocketpaw.mcp_servers` — the meetings in-process server.

    Exposes schedule / list / cancel / search / find-transcript / send-bot
    tools so the agent can run Zoom + Google Meet meetings (and dispatch a
    Recall.ai recording bot) natively from chat.
    """

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.meetings import build_meetings_context_server

        return build_meetings_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.meetings import MEETING_TOOL_IDS

        return list(MEETING_TOOL_IDS)


class CloudPlannerMcpProvider:
    """`pocketpaw.mcp_servers` — the cloud Project Planner in-process
    server (``pocketpaw_planner``). Hosts ``plan_project`` only.

    Stays **opt-in** via ``OPT_IN_MCP_SERVERS`` — Mission Control's
    project-level planner is dead weight in the context of agents that
    never plan a project. The sibling ``CloudPocketPlannerMcpProvider``
    handles the ambient pocket planner.
    """

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.planner import build_planner_context_server

        return build_planner_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.planner import PLANNER_TOOL_IDS

        return list(PLANNER_TOOL_IDS)


class CloudPocketPlannerMcpProvider:
    """`pocketpaw.mcp_servers` — the pocket-create planner in-process
    server (``pocketpaw_pocket_planner``). Hosts ``plan_pocket`` only.

    Intentionally ambient — the bundled ``pocketpaw-pocket-planner``
    skill must be reachable from any cloud agent that hits the
    plan-pointer kit branch on pocket_specialist create. Splitting
    this off from the project planner is what restores the per-server
    OPT_IN gate for ``plan_project`` (see PR #1223 R2).
    """

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.planner import (
            build_pocket_planner_context_server,
        )

        return build_pocket_planner_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.planner import POCKET_PLANNER_TOOL_IDS

        return list(POCKET_PLANNER_TOOL_IDS)


class CloudPocketMcpProvider:
    """`pocketpaw.mcp_servers` — the cloud pocket-context in-process server."""

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.pockets import build_pocket_context_server

        return build_pocket_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.pockets import POCKET_TOOL_IDS

        return list(POCKET_TOOL_IDS)


class CloudDecisionsMcpProvider:
    """`pocketpaw.mcp_servers` — the decision-graph in-process server.

    Wires the three read tools (decisions_get / decisions_find /
    decisions_trace) shipped in RFC 07 Slice 2. The narrator
    `decisions_explain` tool is intentionally Slice 3 — not registered
    here yet so the SDK surface tracks what the substrate actually
    supports.
    """

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.decisions import build_decisions_context_server

        return build_decisions_context_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.decisions import DECISIONS_TOOL_IDS

        return list(DECISIONS_TOOL_IDS)


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


class CloudForesightMcpProvider:
    """`pocketpaw.mcp_servers` — the cloud Foresight scenario CRUD + run server.

    Closes the bug where the bundled ``foresight-create-sim`` skill told
    the chat agent to call the loopback REST surface with
    ``$WORKSPACE_ID`` / ``$USER_ID`` env vars the ``claude_agent_sdk``
    backend never sets. The typed MCP tools close over the chat session's
    workspace id (read from ``ee.cloud.chat.agent_service`` ContextVars)
    so the agent literally cannot save to the wrong workspace.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.foresight import build_foresight_server

            return build_foresight_server()
        except ImportError:
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.foresight import FORESIGHT_TOOL_IDS

        return list(FORESIGHT_TOOL_IDS)


class CloudSitesMcpProvider:
    """`pocketpaw.mcp_servers` — the Paw Sites publish in-process server
    (``pocketpaw_sites_manager``). Hosts ``publish`` only.

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) so the bundled
    ``pocketpaw-create-site`` skill can call it on any cloud chat agent that
    hits a "publish this pocket as a site" request without an explicit opt-in —
    the same regime the pocket specialist + pocket planner use.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

            return build_sites_manager_server()
        except ImportError:
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS

        return list(SITES_TOOL_IDS)


class CloudMediaMcpProvider:
    """`pocketpaw.mcp_servers` — the STUDIO media-generation in-process server
    (``pocketpaw_media``). Hosts ``image_generate`` + ``video_generate``.

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) so the bundled ``studio`` skill can
    call it on any cloud chat agent that hits a "generate an image / make a
    video" request without an explicit opt-in — the same regime the sites
    manager + pocket specialist use. The cloud chat agent runs on the
    claude_agent_sdk backend, which only sees in-process MCP servers (a plain
    BaseTool is invisible to it), so media generation MUST be surfaced here.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.media import build_media_server

            return build_media_server()
        except ImportError:
            # claude_agent_sdk not installed — the media server is unavailable,
            # same as the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.media import MEDIA_TOOL_IDS

        return list(MEDIA_TOOL_IDS)


class CloudDeliverMcpProvider:
    """`pocketpaw.mcp_servers` — the artifact-delivery in-process server
    (``pocketpaw_deliver``). Hosts ``deliver_artifact`` only (ART-4).

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) so any cloud chat agent can deliver
    a file it built — a report, an export, a zipped bundle — to the tenant's
    blob storage and hand the user a real download URL, without an explicit
    opt-in. Same regime the sites manager + media + pocket specialist use: the
    cloud chat agent runs on the claude_agent_sdk backend, which only sees
    in-process MCP servers, so the deliver path MUST be surfaced here.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.deliver import build_deliver_server

            return build_deliver_server()
        except ImportError:
            # claude_agent_sdk not installed — the deliver server is
            # unavailable, same as the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.deliver import DELIVER_TOOL_IDS

        return list(DELIVER_TOOL_IDS)


class CloudLoomMcpProvider:
    """`pocketpaw.mcp_servers` — the loom codebase-orientation MCP server.

    Registers the external loom binary (``loom mcp -model <worldmodel.json>``)
    as a STDIO MCP server — server name ``loom``, 5 read tools (orient /
    locate / why / what_depends_on / boundaries). Unlike the sibling
    providers, ``build_server`` returns a stdio CONFIG DICT, not an
    in-process SDK server object (Path A): the claude_agent_sdk's
    ``mcp_servers`` option accepts stdio configs natively and the pocketpaw
    registration loop passes the dict through untouched.

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) — the /belt surface scopes
    access via its profile allowlist; surfaces whose allowlists don't name
    the loom tool ids simply never see them. ``build_loom_server`` returns
    None (loop skips it) when ``loom_model_path`` is unset or the binary is
    missing, so chat keeps working with orientation simply absent.
    """

    def build_server(self) -> tuple[str, Any] | None:
        from pocketpaw_ee.agent.mcp_servers.loom import build_loom_server

        return build_loom_server()

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.loom import LOOM_TOOL_IDS

        return list(LOOM_TOOL_IDS)


class CloudBeltMcpProvider:
    """`pocketpaw.mcp_servers` — the Belt & Pulley code-change gate in-process
    server (``pocketpaw_belt``). Hosts ``belt_propose_change`` only.

    The develop station agent on the /belt surface produces a unified diff and
    proposes it THROUGH Instinct (the human approve/reject layer) via this tool.
    On approval the ee instinct router fires
    ``ee.cloud.belt.executor.execute_approved_change`` to apply the diff in a
    fresh worktree and open a PR — the captain still merges on GitHub.

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) — the /belt surface scopes access
    via its profile allowlist, the same regime the sibling loom / media / sites
    servers use. ``build_belt_server`` returns None — and the loop skips it —
    when the claude_agent_sdk isn't installed, so chat never breaks.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.belt import build_belt_server

            return build_belt_server()
        except ImportError:
            # claude_agent_sdk not installed — the belt server is unavailable,
            # same as the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.belt import BELT_TOOL_IDS

        return list(BELT_TOOL_IDS)


class CloudExternalActionsMcpProvider:
    """`pocketpaw.mcp_servers` — the gated external-action proposal in-process
    server (``pocketpaw_external_actions``). Hosts ``propose_external_action``
    only.

    A chat agent proposes a call to an external system through a bound connector
    THROUGH Instinct (the human approve/reject layer) via this tool. On approval
    the ee instinct router fires
    ``ee.cloud.external_actions.executor.execute_approved_external_action`` to
    make the connector call — the tool itself never executes anything (propose
    only).

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) — surfaces scope access via their
    profile allowlist, the same regime the sibling belt / loom / media / sites
    servers use. ``build_external_actions_server`` returns None — and the loop
    skips it — when the claude_agent_sdk isn't installed, so chat never breaks.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.external_actions import (
                build_external_actions_server,
            )

            return build_external_actions_server()
        except ImportError:
            # claude_agent_sdk not installed — the server is unavailable, same as
            # the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.external_actions import (
            EXTERNAL_ACTIONS_TOOL_IDS,
        )

        return list(EXTERNAL_ACTIONS_TOOL_IDS)


class CloudFabricMcpProvider:
    """`pocketpaw.mcp_servers` — read-only Fabric ontology access in-process
    server (``pocketpaw_fabric``). Hosts ``fabric_query`` + ``fabric_stats``.

    On the claude_agent_sdk backend, registry tools (BaseTool) never reach the
    agent — only MCP servers do — so this server is the cloud chat agent's only
    path to the Fabric ontology. Both tools are READ-ONLY and workspace-scoped
    via the chat ContextVars; ontology writes from this backend should arrive
    as gated proposals, never ambient writes.

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) — surfaces scope access via their
    profile allowlist, the same regime the sibling belt / external-actions /
    media servers use. ``build_fabric_server`` returns None — and the loop
    skips it — when the claude_agent_sdk isn't installed, so chat never breaks.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.fabric import build_fabric_server

            return build_fabric_server()
        except ImportError:
            # claude_agent_sdk not installed — the server is unavailable, same as
            # the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.fabric import FABRIC_TOOL_IDS

        return list(FABRIC_TOOL_IDS)


class CloudInstinctMcpProvider:
    """`pocketpaw.mcp_servers` — read-only Instinct gate visibility in-process
    server (``pocketpaw_instinct``). Hosts ``instinct_pending`` +
    ``instinct_audit``.

    On the claude_agent_sdk backend, registry tools (BaseTool) never reach the
    agent — only MCP servers do — so this server is the cloud chat agent's only
    view into the Instinct gate (pending approvals + the decision audit log).
    READ-ONLY: it never approves, rejects, executes, or proposes. Gated
    proposing on this backend goes through ``pocketpaw_external_actions``
    (``propose_external_action``), not a wrapped InstinctProposeTool.

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) — surfaces scope access via their
    profile allowlist, the same regime the sibling belt / external-actions /
    media servers use. ``build_instinct_server`` returns None — and the loop
    skips it — when the claude_agent_sdk isn't installed, so chat never breaks.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.instinct import build_instinct_server

            return build_instinct_server()
        except ImportError:
            # claude_agent_sdk not installed — the server is unavailable, same as
            # the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.instinct import INSTINCT_TOOL_IDS

        return list(INSTINCT_TOOL_IDS)


class CloudDaytonaMcpProvider:
    """`pocketpaw.mcp_servers` — the Daytona sandbox in-process server
    (``pocketpaw_daytona``). Hosts sandbox-aware read_file, write_file,
    edit_file, list_dir, shell, and run_python tools.

    When an agent is on the /code surface with a cloud project that has a
    Daytona sandbox provisioned, these tools let the agent read, write, edit
    files, run shell commands, and execute Python code — all inside the
    sandbox VM instead of the local filesystem.

    Ambient (NOT in ``OPT_IN_MCP_SERVERS``) — the /code surface scopes
    access via its profile allowlist, the same regime the sibling
    fabric / instinct / media servers use. ``build_daytona_server`` returns
    None — and the loop skips it — when the claude_agent_sdk isn't installed,
    so chat never breaks.
    """

    def build_server(self) -> tuple[str, Any] | None:
        try:
            from pocketpaw_ee.agent.mcp_servers.daytona import build_daytona_server

            return build_daytona_server()
        except ImportError:
            # claude_agent_sdk not installed — the server is unavailable, same as
            # the other in-process servers.
            return None

    def tool_ids(self) -> list[str]:
        from pocketpaw_ee.agent.mcp_servers.daytona import DAYTONA_TOOL_IDS

        return list(DAYTONA_TOOL_IDS)


class CloudAgentExtension:
    """`pocketpaw.agent_extensions` — EE additions to the core agent runtime.

    Contributes the cloud pocket-specialist function tool to MCP-capable
    tool-list backends, cloud workspace/user/session identity to agent
    subprocess environments, (ART-2) a per-tenant agent working-directory
    jail so each cloud workspace's file ops stay isolated from every other,
    and Daytona-aware tool variants that route file/shell operations through
    a provisioned sandbox VM when active.
    """

    # Backends that receive ``PocketSpecialistTool`` as a native function
    # tool. Shell-CLI backends (codex_cli, opencode, copilot_sdk) use the
    # cloud_pocket_specialist_create CLI command instead; claude_agent_sdk
    # uses its own in-process specialist MCP server — surfacing the tool
    # through the function-tool bridge for either would advertise a name
    # their dispatcher can't resolve.
    _SPECIALIST_FUNCTION_TOOL_BACKENDS = frozenset({"deep_agents", "google_adk", "openai_agents"})
    # Same backends also receive Daytona-aware tools (read_file, write_file,
    # edit_file, list_dir, shell, run_python) that route through the sandbox
    # VM when a Daytona context is active. Because these tools use the EXACT
    # SAME names as the OSS builtins, they replace them in the ToolRegistry
    # via last-writer-wins registration.
    _DAYTONA_TOOL_BACKENDS = frozenset({"deep_agents", "google_adk", "openai_agents"})

    def agent_tools(self, backend: str) -> list[Any]:
        tools: list[Any] = []

        # Pocket specialist tool.
        if backend in self._SPECIALIST_FUNCTION_TOOL_BACKENDS:
            try:
                from pocketpaw_ee.agent.pocket_specialist.tool import PocketSpecialistTool

                tools.append(PocketSpecialistTool())
            except Exception:  # noqa: BLE001
                pass

        # Daytona-aware tools — replace OSS builtins with sandbox-routing
        # variants when Daytona is configured.
        if backend in self._DAYTONA_TOOL_BACKENDS:
            try:
                from pocketpaw_ee.cloud.daytona.tools import get_daytona_tools

                tools.extend(get_daytona_tools())
            except Exception:  # noqa: BLE001
                pass

        return tools

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

    def agent_cwd(self) -> str | None:
        """`pocketpaw.agent_extensions` — per-session agent working directory.

        In multi-tenant cloud each workspace gets its own agent cwd
        (``~/.pocketpaw/workspaces/<ws>/agent/<session>/``) so tenant file ops
        never co-mingle in the shared home dir; fails CLOSED when a cloud run
        has no resolvable workspace. Returns ``None`` off-cloud so the core
        agent keeps using ``settings.file_jail_path`` (ART-2)."""
        from pocketpaw_ee.cloud.agent_jail import resolve_agent_cwd

        return resolve_agent_cwd()


class CloudConnectorStateStoreProvider:
    """`pocketpaw.connector_state_stores` — durable connector state from the
    cloud DB.

    Backs the ConnectorRegistry's restart-survival seam with the
    ``WorkspaceConnector`` Beanie doc (namespaced ``ws:<workspace_id>`` /
    ``pocket:<pocket_id>`` scope keys; everything else delegates to the OSS
    file store). With this registered, ``registry.ensure_connected`` on a
    fresh process rehydrates a connector from its workspace row — a cloud
    execute needs no prior /connect call.
    """

    def get_state_store(self) -> Any:
        from pocketpaw_ee.cloud.connectors.state_provider import CloudConnectorStateStore

        return CloudConnectorStateStore()


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


class CloudStoreProvider:
    """``pocketpaw.stores`` — the workspace-keyed store provider (ISO-3).

    Activates the ``StoreProvider`` seam ISO-1 added but left dormant. The OSS
    factory consults this provider FIRST when building a workspace-keyed store;
    we return the standard per-workspace SQLite file store (Fabric / Instinct
    under ``~/.pocketpaw/workspaces/<id>/<name>.db``) by delegating to the OSS
    helper ``build_workspace_store``, so:

    * the file path AND the strict path-traversal allowlist stay defined ONCE,
      in OSS core — a provider can't drift from or weaken the guard; and
    * delegation is recursion-safe (``build_workspace_store`` never re-enters
      the provider seam).

    We return ``None`` for the legacy / no-workspace path (``workspace_id`` is
    ``None``) so the OSS factory keeps using its shared single-tenant singleton
    there — the cloud always carries a workspace, so on cloud this provider only
    ever serves the per-workspace branch. This is the one hook a future task
    swaps to back stores with a cloud DB or a per-tenant server, without
    touching core.
    """

    def get_store(self, name: str, *, workspace_id: str | None = None) -> Any:
        if workspace_id is None:
            # No workspace → let the OSS factory use its legacy shared singleton
            # (or fail closed under POCKETPAW_REQUIRE_WORKSPACE_SCOPE, which the
            # factory enforces before it ever consults a provider).
            return None
        from pocketpaw.stores import build_workspace_store

        return build_workspace_store(name, workspace_id)
