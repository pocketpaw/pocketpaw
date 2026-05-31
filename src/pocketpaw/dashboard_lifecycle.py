"""Dashboard lifecycle management — startup, shutdown, and broadcast helpers.

Extracted from dashboard.py — contains:
- ``broadcast_reminder()`` / ``broadcast_intention()`` — push to WS + notification channels
- ``_broadcast_audit_entry()`` / ``_broadcast_health_update()`` — WS-only broadcasts
- ``startup_event()`` — initializes bus, agent loop, channels, MCP, health, scheduler, daemon
- ``shutdown_event()`` — tears down all services

Changes:
- 2026-05-22: ``startup_event`` also mirrors the built-in pocket
  templates into ``~/.pocketpaw/templates/`` via the
  ``bundled_templates`` installer — same best-effort, catch-and-log
  pattern as the skills / kb installers, gated on
  ``auto_install_bundled_templates`` (feat/bundled-templates, 2a).
- 2026-05-21: ``startup_event`` mirrors bundled SKILL.md files and
  pre-compiled kb-go scopes into the user's home dir at boot via the
  ``bundled_skills`` / ``bundled_kb`` installers. Best-effort, gated on
  the ``auto_install_bundled_*`` settings; reuses the already-loaded
  ``settings`` object.
"""

import asyncio
import logging
from datetime import UTC

import pocketpaw.dashboard_state as _state
from pocketpaw.bus import get_message_bus
from pocketpaw.config import Settings
from pocketpaw.daemon import get_daemon
from pocketpaw.dashboard_state import (
    _channel_adapters,
    _channel_autostart_enabled,
    active_connections,
    agent_loop,
    trace_collector,
    ws_adapter,
)
from pocketpaw.scheduler import get_scheduler
from pocketpaw.security import get_audit_logger
from pocketpaw.security.rate_limiter import cleanup_all

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------


async def broadcast_reminder(reminder: dict):
    """Broadcast a reminder notification to all connected clients."""
    # Use new adapter for broadcast
    await ws_adapter.broadcast(reminder, msg_type="reminder")

    # Legacy broadcast (backup)
    message = {"type": "reminder", "reminder": reminder}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            pass

    # Persist reminder as an assistant message in every active WebSocket session
    # so it survives session switches and page reloads.
    reminder_text = reminder.get("text", "")
    reminder_content = f"Reminder: {reminder_text}"
    try:
        from pocketpaw.memory import get_memory_manager

        manager = get_memory_manager()
        for chat_id in list(ws_adapter._connections.keys()):
            session_key = f"websocket:{chat_id}"
            try:
                await manager.add_to_session(
                    session_key=session_key,
                    role="assistant",
                    content=reminder_content,
                    metadata={
                        "reminder_id": reminder.get("id", ""),
                        "type": "reminder",
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to persist reminder to session %s", session_key, exc_info=True
                )
    except Exception:
        logger.warning("Failed to persist reminder to session history", exc_info=True)

    # Push to notification channels
    try:
        from pocketpaw.bus.notifier import notify

        await notify(reminder_content)
    except Exception:
        pass


async def broadcast_intention(intention_id: str, chunk: dict):
    """Broadcast intention execution results to all connected clients."""
    message = {"type": "intention_event", "intention_id": intention_id, **chunk}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)

    # Push message-type intention chunks to notification channels
    if chunk.get("type") == "message":
        try:
            from pocketpaw.bus.notifier import notify

            await notify(chunk.get("content", ""))
        except Exception:
            pass


async def _broadcast_audit_entry(entry: dict):
    """Broadcast a new audit log entry to all connected WebSocket clients."""
    message = {"type": "system_event", "event_type": "audit_entry", "data": entry}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)


async def _broadcast_health_update(summary: dict):
    """Broadcast health status update to all connected WebSocket clients."""
    message = {"type": "health_update", "data": summary}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)


async def push_open_path(path: str, action: str = "navigate"):
    """Push an open_path event to all connected WebSocket clients.

    Parameters
    ----------
    path:
        Absolute filesystem path to open.
    action:
        ``"navigate"`` to open a folder in the explorer, or
        ``"view"`` to open a file in the viewer.
    """
    message = {"type": "open_path", "path": path, "action": action}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def startup_event(
    *,
    _start_channel_adapter_fn=None,
):
    """Start services on app startup.

    Parameters
    ----------
    _start_channel_adapter_fn:
        Callable for starting a channel adapter. Injected from dashboard.py
        to avoid circular import with dashboard_channels.
    """
    # Start Message Bus Integration
    bus = get_message_bus()
    await ws_adapter.start(bus)

    # Load .env for enterprise config (license key, MongoDB URI, admin creds)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    # Run enterprise lifecycle startup hooks — best-effort. The cloud hook
    # initializes the Beanie/MongoDB database, seeds the default admin +
    # workspace, back-fills agents and registers the chat-title listener.
    # Discovered via the `pocketpaw.lifecycle` entry-point; an OSS install
    # has no provider and skips this entirely.
    from pocketpaw._registry import providers as _ext_providers

    for _hook in _ext_providers("pocketpaw.lifecycle"):
        try:
            await _hook.on_startup()
        except Exception as exc:
            logger.warning("Lifecycle startup hook failed (cloud features disabled): %s", exc)

    # Start the cloud agent pool GC task. Previously registered via
    # ``@app.on_event("startup")`` inside ``mount_cloud()`` but that path is
    # silenced when the app is built with a custom ``lifespan=``.
    try:
        from pocketpaw.agents.pool import get_agent_pool

        await get_agent_pool().start()
    except Exception as exc:
        logger.warning("Agent pool start failed: %s", exc)

    # Start Agent Loop
    asyncio.create_task(agent_loop.start())
    logger.info("Agent Loop started")

    # Auto-start configured channel adapters (respects per-channel autostart setting)
    settings = Settings.load()

    # Mirror PocketPaw's bundled AgentSkills-format SKILL.md files into
    # ``~/.claude/skills/<name>/`` so PocketPaw's own ``SkillLoader``
    # (and Claude Code's native discovery, for claude_agent_sdk users)
    # picks them up. Skills loaded from there work across every chat
    # backend via PocketPaw's slash-command dispatcher; claude_agent_sdk
    # additionally auto-discovers them on natural-language intent.
    # Best-effort — a failure here just means the chat agent won't have
    # the ``pocketpaw-create-pocket`` / ``pocketpaw-edit-pocket`` skills;
    # the MCP tool surface still works. Opt-out:
    # ``POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false``.
    if settings.auto_install_bundled_skills:
        try:
            from pocketpaw.bundled_skills import install_bundled_skills

            results = install_bundled_skills()
            installed = sum(1 for r in results if r.status == "installed")
            updated = sum(1 for r in results if r.status == "updated")
            skipped = sum(1 for r in results if r.status == "skipped")
            failed = [r for r in results if r.status == "failed"]
            logger.info(
                "Bundled skills sync: %d installed / %d updated / %d skipped / %d failed",
                installed,
                updated,
                skipped,
                len(failed),
            )
            for r in failed:
                logger.warning("Skill %s failed to install: %s", r.name, r.error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bundled-skills install failed (non-fatal): %s", exc)

    # Mirror PocketPaw's pre-compiled kb-go scopes into
    # ``~/.knowledge-base/`` so the existing ``_get_kb_context``
    # injection can retrieve recipes at pocket-creation time.
    # Best-effort — a failure here just means the agent loses the
    # recipe-retrieval boost; the MCP tool + skill flow still works.
    # Opt-out: ``POCKETPAW_AUTO_INSTALL_BUNDLED_KB_SCOPES=false``.
    if settings.auto_install_bundled_kb_scopes:
        try:
            from pocketpaw.bundled_kb import install_bundled_kb_scopes

            kb_results = install_bundled_kb_scopes()
            installed = sum(1 for r in kb_results if r.status == "installed")
            updated = sum(1 for r in kb_results if r.status == "updated")
            skipped = sum(1 for r in kb_results if r.status == "skipped")
            failed = [r for r in kb_results if r.status == "failed"]
            logger.info(
                "Bundled kb-go scopes sync: %d installed / %d updated / %d skipped / %d failed",
                installed,
                updated,
                skipped,
                len(failed),
            )
            for r in failed:
                logger.warning("KB scope %s failed to install: %s", r.name, r.error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bundled kb-go scopes install failed (non-fatal): %s", exc)

    # Mirror PocketPaw's built-in pocket templates into
    # ``~/.pocketpaw/templates/`` so the create specialist can
    # instantiate-and-customize a matching template instead of
    # cold-generating a pocket from scratch. Best-effort — a failure
    # here just means the specialist falls back to cold generation;
    # pocket creation still works. Opt-out:
    # ``POCKETPAW_AUTO_INSTALL_BUNDLED_TEMPLATES=false``.
    if settings.auto_install_bundled_templates:
        try:
            from pocketpaw.bundled_templates import install_bundled_templates

            tpl_results = install_bundled_templates()
            installed = sum(1 for r in tpl_results if r.status == "installed")
            updated = sum(1 for r in tpl_results if r.status == "updated")
            skipped = sum(1 for r in tpl_results if r.status == "skipped")
            failed = [r for r in tpl_results if r.status == "failed"]
            logger.info(
                "Bundled pocket templates sync: %d installed / %d updated / %d skipped / %d failed",
                installed,
                updated,
                skipped,
                len(failed),
            )
            for r in failed:
                logger.warning("Pocket template %s failed to install: %s", r.name, r.error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bundled pocket templates install failed (non-fatal): %s", exc)

    # Start StatusTracker (agent state for external integrations)
    from pocketpaw.dashboard_state import status_tracker
    from pocketpaw.lifecycle import register as _register_lifecycle

    status_tracker._max_concurrent = settings.max_concurrent_conversations
    await status_tracker.subscribe()
    _register_lifecycle("status_tracker", shutdown=status_tracker.unsubscribe)

    await trace_collector.subscribe()
    _register_lifecycle("trace_collector", shutdown=trace_collector.unsubscribe)
    try:
        removed = await trace_collector.cleanup_retention(settings.trace_retention_days)
        if removed:
            logger.info("Trace retention cleanup removed %d file(s)", removed)
    except Exception:
        logger.debug("Trace retention cleanup failed", exc_info=True)

    # Start AlertManager (periodic threshold checks + bus subscriptions)
    try:
        from pocketpaw.alert_manager import get_alert_manager

        alert_manager = get_alert_manager()
        await alert_manager.start()
        _register_lifecycle("alert_manager", shutdown=alert_manager.stop)
        logger.info("AlertManager started")
    except Exception as e:
        logger.warning("Failed to start AlertManager: %s", e)

    # Start ChannelHealthStore (connects/disconnects uptime tracking)
    try:
        from pocketpaw.channel_health_store import get_channel_health_store

        channel_health_store = get_channel_health_store()
        await channel_health_store.subscribe()
        _register_lifecycle("channel_health_store", shutdown=channel_health_store.unsubscribe)
        logger.info("ChannelHealthStore subscribed")
    except Exception as e:
        logger.warning("Failed to subscribe ChannelHealthStore: %s", e)

    if _start_channel_adapter_fn:
        for ch in (
            "discord",
            "slack",
            "whatsapp",
            "telegram",
            "signal",
            "matrix",
            "teams",
            "google_chat",
        ):
            if not _channel_autostart_enabled(ch, settings):
                logger.debug("Skipping %s auto-start (disabled in settings)", ch)
                continue
            try:
                if await _start_channel_adapter_fn(ch, settings):
                    logger.info(f"{ch.title()} adapter auto-started alongside dashboard")
            except Exception as e:
                logger.warning(f"Failed to auto-start {ch} adapter: {e}")

        # Auto-start webhook adapter if webhooks are configured
        if settings.webhook_configs:
            try:
                if await _start_channel_adapter_fn("webhook", settings):
                    count = len(settings.webhook_configs)
                    logger.info("Webhook adapter auto-started (%d slots)", count)
            except Exception as e:
                logger.warning("Failed to auto-start webhook adapter: %s", e)

    # Ensure project directories exist for all Deep Work projects
    try:
        from pocketpaw.mission_control.manager import get_mission_control_manager

        mc_manager = get_mission_control_manager()
        await mc_manager.ensure_project_directories()
    except Exception as e:
        logger.warning("Failed to ensure project directories: %s", e)

    # Recover Deep Work projects interrupted by previous shutdown
    try:
        from pocketpaw.deep_work import recover_interrupted_projects

        recovered = await recover_interrupted_projects()
        if recovered:
            logger.info("Recovered %d interrupted Deep Work project(s)", recovered)
    except Exception as e:
        logger.warning("Failed to recover interrupted projects: %s", e)

    # Ensure built-in PawKits are installed
    try:
        from pathlib import Path

        from pocketpaw.kits.store import get_kit_store

        kit_store = get_kit_store()
        installed = await kit_store.list_kits()
        builtin_ids = {k.id for k in installed if k.config.meta.built_in}
        if "project-orchestrator" not in builtin_ids:
            yaml_path = Path(__file__).parent / "kits" / "builtins" / "project_orchestrator.yaml"
            yaml_str = yaml_path.read_text(encoding="utf-8")
            kit = await kit_store.install_kit(yaml_str, kit_id="project-orchestrator")
            await kit_store.activate_kit(kit.id)
            logger.info("Auto-installed built-in PawKit: Project Orchestrator")
    except Exception as e:
        logger.warning("Failed to ensure built-in PawKits: %s", e)

    # In-process MCP servers (entry-point `pocketpaw.mcp_servers`). These
    # are the claude_agent_sdk SDK servers contributed by extension wheels
    # — tasks, planner, pocket, pocket_specialist, composio, meetings, …
    # An empty list almost always means `pocketpaw-ee` wasn't installed
    # (so no entry points registered), which silently disables every cloud
    # MCP tool on the agent. Loud-log it once at boot.
    try:
        from pocketpaw._registry import providers as _ext_providers

        names: list[str] = []
        for provider in _ext_providers("pocketpaw.mcp_servers"):
            try:
                built = provider.build_server()
            except Exception as exc:  # noqa: BLE001
                logger.warning("In-process MCP provider %r failed to build: %s", provider, exc)
                continue
            if built is None:
                # None means the provider chose to soft-disable itself —
                # could be claude_agent_sdk missing, an unconfigured
                # integration (composio with no toolkits), or no work to do.
                # Distinguishing them is the provider's job; we just note it.
                logger.info(
                    "In-process MCP provider %s soft-disabled (build_server returned None)",
                    type(provider).__name__,
                )
                continue
            names.append(built[0])
        if names:
            logger.info("In-process MCP servers registered: %s", sorted(names))
        else:
            logger.warning(
                "No in-process MCP servers registered — cloud tools (meetings, "
                "tasks, pockets, composio, …) will be unavailable to the agent. "
                "Install pocketpaw-ee (uv sync --dev --group ee) and the "
                "claude_agent_sdk."
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enumerate in-process MCP providers: %s", exc)

    # Wire MCP OAuth broadcast + auto-start enabled MCP servers (non-blocking)
    try:
        from pocketpaw.mcp.manager import get_mcp_manager, set_ws_broadcast

        async def _mcp_ws_broadcast(message: dict) -> None:
            """Broadcast an MCP message to all connected WebSocket clients."""
            for ws in active_connections[:]:
                try:
                    await ws.send_json(message)
                except Exception:
                    pass

        set_ws_broadcast(_mcp_ws_broadcast)

        mcp = get_mcp_manager()

        async def _start_mcp_background() -> None:
            """Start MCP servers in background so dashboard isn't blocked."""
            try:
                await mcp.start_enabled_servers()
            except Exception as exc:
                logger.warning("Failed to start MCP servers: %s", exc)

        asyncio.create_task(_start_mcp_background())
    except Exception as e:
        logger.warning("Failed to initialize MCP manager: %s", e)

    # Initialize health engine and run startup checks
    try:
        from pocketpaw.health import get_health_engine

        health_engine = get_health_engine()
        health_engine.run_startup_checks()
        # Fire connectivity checks in background (non-blocking)
        asyncio.create_task(health_engine.run_connectivity_checks())
        logger.info("Health engine initialized: %s", health_engine.overall_status)
    except Exception as e:
        logger.warning("Failed to initialize health engine: %s", e)

    # Register audit log callback for live updates
    audit_logger = get_audit_logger()
    audit_logger.on_log(lambda entry: asyncio.ensure_future(_broadcast_audit_entry(entry)))

    # Start reminder scheduler
    scheduler = get_scheduler()
    scheduler.start(callback=broadcast_reminder)

    # Start proactive daemon. Prune orphan ``[auto] *`` intentions first so
    # bridged-from-EE entries whose Rule no longer exists don't keep
    # firing crons forever (test fixtures, deleted rules, manual edits).
    try:
        from pocketpaw.automations.bridge import prune_orphan_auto_intentions
        from pocketpaw.daemon.intentions import get_intention_store

        prune_orphan_auto_intentions()
        # Single line that tells the truth at startup-completion. The
        # ``Loaded N intentions`` line from the IntentionStore singleton
        # fires before pruning and can mislead readers when N includes
        # orphans that get cleared a few lines later.
        active = len(get_intention_store().get_enabled())
        logger.info("Active intentions: %d", active)
    except Exception:
        logger.exception("Failed to prune orphan [auto] intentions; continuing")

    daemon = get_daemon()
    daemon.start(stream_callback=broadcast_intention)

    # Health heartbeat — periodic checks every 5 min, broadcast on status transitions
    try:
        from pocketpaw.health import get_health_engine

        _health_engine = get_health_engine()
        _prev_status = _health_engine.overall_status

        async def _health_heartbeat():
            nonlocal _prev_status
            try:
                _health_engine.run_startup_checks()
                await _health_engine.run_connectivity_checks()
                new_status = _health_engine.overall_status
                if new_status != _prev_status:
                    logger.info("Health status changed: %s -> %s", _prev_status, new_status)
                    _prev_status = new_status
                    await _broadcast_health_update(_health_engine.summary)
            except Exception as e:
                logger.warning("Health heartbeat error: %s", e)

        # Reuse the daemon's APScheduler
        from datetime import datetime, timedelta

        daemon.trigger_engine.scheduler.add_job(
            _health_heartbeat,
            "interval",
            minutes=5,
            id="health_heartbeat",
            replace_existing=True,
            next_run_time=datetime.now(UTC) + timedelta(seconds=10),
        )
        logger.info("Health heartbeat registered (every 5 min)")
    except Exception as e:
        logger.warning("Failed to register health heartbeat: %s", e)

    # Hourly rate-limiter cleanup
    async def _rate_limit_cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            removed = cleanup_all()
            if removed:
                logger.debug("Rate limiter cleanup: removed %d stale entries", removed)

    asyncio.create_task(_rate_limit_cleanup_loop())

    async def _trace_retention_cleanup_loop():
        while True:
            await asyncio.sleep(21600)
            try:
                current_settings = Settings.load()
                removed = await trace_collector.cleanup_retention(
                    current_settings.trace_retention_days
                )
                if removed:
                    logger.debug("Trace retention cleanup removed %d stale file(s)", removed)
            except Exception:
                logger.debug("Trace retention cleanup loop failed", exc_info=True)

    asyncio.create_task(_trace_retention_cleanup_loop())

    # Open browser now that the server is actually listening
    if _state._open_browser_url:
        import webbrowser

        webbrowser.open(_state._open_browser_url)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def _bounded(label: str, coro, *, timeout: float = 5.0) -> None:
    """Await *coro* but never let a wedged subsystem hang process shutdown.

    A cleanup step that exceeds *timeout* is abandoned (cancelled) with a
    warning so the rest of shutdown — and the process exit — still proceeds.
    Without this, a stuck ``await`` here blocks uvicorn's lifespan shutdown,
    which has no timeout of its own: Ctrl+C appears to do nothing and the
    port stays bound until the terminal is killed.
    """
    try:
        await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        logger.warning("Shutdown step '%s' timed out after %.0fs — skipping", label, timeout)
    except Exception:
        logger.warning("Shutdown step '%s' failed", label, exc_info=True)


async def shutdown_event(*, _stop_channel_adapter_fn=None):
    """Stop services on app shutdown.

    Every async cleanup step is time-bounded via ``_bounded`` — a single
    subsystem that fails to stop cleanly must not wedge the whole process.

    Parameters
    ----------
    _stop_channel_adapter_fn:
        Callable for stopping a channel adapter. Injected from dashboard.py.
    """
    # Stop Agent Loop + WebSocket adapter
    await _bounded("agent_loop", agent_loop.stop())
    await _bounded("ws_adapter", ws_adapter.stop())

    # Stop the cloud agent pool (best-effort; may not have started if the
    # enterprise cloud module is not installed).
    try:
        from pocketpaw.agents.pool import get_agent_pool

        await _bounded("agent_pool", get_agent_pool().stop())
    except Exception as exc:
        logger.warning("Agent pool stop failed: %s", exc)

    # Drain in-flight cloud chat runs. Under ``FastAPI(lifespan=...)`` this
    # is the only shutdown hook that fires — ``@app.on_event("shutdown")``
    # handlers in ``mount_cloud`` are silently dropped. ``drain()`` waits for
    # tasks to finish naturally (no cancellation); ``_bounded`` caps the wait
    # so a stuck run can't wedge exit.
    try:
        from pocketpaw_ee.cloud.chat.runs.executor import InProcessExecutor, get_executor

        executor = get_executor()
        if isinstance(executor, InProcessExecutor):
            await _bounded("cloud_run_drain", executor.drain(), timeout=10.0)
    except Exception as exc:
        logger.debug("Cloud run drain skipped: %s", exc)

    # Stop all channel adapters
    if _stop_channel_adapter_fn:
        for channel in list(_channel_adapters):
            await _bounded(f"channel:{channel}", _stop_channel_adapter_fn(channel))

    # Stop proactive daemon (sync — scheduler.shutdown(wait=False), fast)
    try:
        get_daemon().stop()
    except Exception:
        logger.warning("Daemon stop failed", exc_info=True)

    # Stop reminder scheduler (sync — scheduler.shutdown(wait=False), fast)
    try:
        get_scheduler().stop()
    except Exception:
        logger.warning("Scheduler stop failed", exc_info=True)

    # Stop MCP servers — these own subprocesses, so give them a little longer
    # but still bound it: an MCP child that ignores SIGTERM must not hang exit.
    try:
        from pocketpaw.mcp.manager import get_mcp_manager

        await _bounded("mcp_servers", get_mcp_manager().stop_all(), timeout=8.0)
    except Exception as e:
        logger.warning("Error stopping MCP servers: %s", e)
