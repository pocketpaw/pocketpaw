# ee/pocketpaw_ee/sites/dev_server.py — Phase 2 / P2a: live Vite dev-server
# preview manager for the Paw Sites EDITING surface.
#
# Created: 2026-06-18 (feat/sites-devserver, Phase 2 / P2a).
#
# Updated 2026-06-19 (fix/sites-preview-fresh-build, P0a): the P0a fix makes
# generator.build()'s ``bun run build`` static-output step ALWAYS run by default
# (so the served preview/publish is fresh + anchored — the #1 bug). The dev server
# serves from SOURCE via ``vite dev`` and never goes through deploy_local, so it
# needs no ``.svelte-kit/cloudflare/`` static output; ``_default_materialize`` now
# passes ``static_build=False`` to SKIP the prod build (generate + cached install
# is all ``vite dev`` needs). Without this the dev path would run a wasted full prod
# build before every dev-server start.
#
# WHY: today editing a site rebuilds the whole SvelteKit app per edit (generate +
# install + render). Phase 1 (PERF-3/PERF-4) cut that to a cached install + no
# smoke, but a full `vite build` still runs per edit. P2a replaces the EDITING
# preview with a long-lived `vite dev` server per pocket: the first edit
# materializes the pocket's source into the PERSISTENT per-pocket dir (PERF-3 —
# node_modules already cached) and starts `vite dev` on an ephemeral port;
# subsequent edits hot-reload in ~ms over Vite HMR. PUBLISH is unchanged — it still
# runs the full prod build + workerd smoke (PERF-4). The dev server is the editing
# preview ONLY, never a deploy.
#
# DESIGN (this file: DevServerManager):
#   * Async singleton (get_manager()). Tracks {pocket_id -> _DevServer} in an
#     OrderedDict so the registry doubles as the LRU order (move_to_end on touch).
#   * ensure_dev_server(workspace_id, user_id, pocket_id) -> dev URL: live server
#     for the pocket → touch + return its url; else enforce the LRU cap, materialize
#     the source, allocate a free port, spawn `vite dev`, register, return the url.
#   * Per-pocket asyncio lock so two concurrent ensure calls never double-spawn; a
#     global lock guards registry mutations (cap eviction, reaper sweep).
#   * Idle reaper: a background task (start_reaper/stop_reaper, mirroring
#     extensions.start_run_sweeper) that stops servers idle > idle_seconds.
#   * LRU cap (max_servers): when starting would exceed the cap, the
#     least-recently-used server is stopped first. Tenant-box reality: a handful of
#     concurrent editors, so a small cap + a reaper keeps the process bounded.
#   * INJECTABLE seams so the lifecycle is unit-testable WITHOUT real vite/bun:
#       - _spawn(cmd, cwd, port) -> process   (default: asyncio subprocess)
#       - _free_port() -> int                 (default: bind a socket to port 0)
#       - _materialize(workspace_id, user_id, pocket_id) -> project_dir
#                                             (default: GeneratorClient.build →
#                                              the persistent per-pocket project dir)
#
# EDIT-BRIDGE NOTE (P2a scope decision): the hover-edit overlay needs the
# `data-paw-section` anchors + the gated postMessage edit-bridge (SE-1/SE-2b) to be
# present in the SOURCE the dev server serves. As of this change that bridge is NOT
# in the ACTIVE generator (paw-workspace/paw-sites @ feat/dynamic-sites-authoring,
# the one PAW_SITES_GEN_CMD invokes) — it lives only in an unmerged intg-paw-sites
# worktree. So P2a ships INSTANT VISUAL feedback (Vite HMR) — the core win — and the
# overlay-in-dev is a documented follow-up: merge the SE-1/SE-2b bridge into the
# active generator's source templates (app.html / component scaffolds) so a
# dev-served page carries it. No deploy/publish path changes here.

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

# Defaults — all env-overridable so a tenant box can tune them without a redeploy.
_DEFAULT_IDLE_SECONDS = 600.0  # stop a dev server idle longer than this
_DEFAULT_MAX_SERVERS = 3  # LRU cap on concurrently running dev servers
_DEFAULT_HOST = "127.0.0.1"  # dev servers bind loopback only
_DEFAULT_REAP_INTERVAL = 60.0  # how often the reaper sweeps for idle servers


def _idle_seconds() -> float:
    raw = os.environ.get("DEV_SERVER_IDLE_SECONDS")
    try:
        return float(raw) if raw else _DEFAULT_IDLE_SECONDS
    except ValueError:
        return _DEFAULT_IDLE_SECONDS


def _max_servers() -> int:
    raw = os.environ.get("DEV_SERVER_MAX")
    try:
        return max(1, int(raw)) if raw else _DEFAULT_MAX_SERVERS
    except ValueError:
        return _DEFAULT_MAX_SERVERS


def _dev_host() -> str:
    return os.environ.get("DEV_SERVER_HOST", _DEFAULT_HOST)


def _dev_cmd_argv(port: int, host: str) -> list[str]:
    """The `vite dev` invocation, tokenised. Default runs the generated app's own
    ``dev`` script via bun (the template declares ``"dev": "vite dev"``), pinned to
    the chosen host + ephemeral port and ``--strictPort`` so vite fails loudly
    rather than silently hopping to another port we don't know about. Override the
    whole command with PAW_SITES_DEV_CMD (shell-tokenised) for a different runtime
    (e.g. ``npm run dev``); the host/port/strictPort flags are always appended after
    a ``--`` separator so they reach vite, not the package-manager wrapper."""
    import shlex

    base = shlex.split(os.environ.get("PAW_SITES_DEV_CMD", "bun run dev"))
    # `--` ends the package-manager's own args so the flags pass through to vite.
    return [*base, "--", "--host", host, "--port", str(port), "--strictPort"]


class _Process(Protocol):
    """The subset of an async subprocess the manager drives. Both
    asyncio.subprocess.Process and the test's _FakeProc satisfy it."""

    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...


@dataclass
class _DevServer:
    """One running `vite dev` server for a pocket."""

    pocket_id: str
    workspace_id: str
    port: int
    url: str
    process: _Process
    last_activity: float = field(default_factory=time.monotonic)


async def _default_spawn(cmd: list[str], cwd: str, port: int) -> _Process:
    """Default spawner: start `vite dev` as an async subprocess in the project dir.
    Output is discarded (DEVNULL) so a long-lived server never fills a pipe buffer
    and blocks. Replaced wholesale in tests by an injected fake."""
    return await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


def _default_free_port() -> int:
    """Default port allocator: bind a socket to port 0, read the OS-assigned port,
    release it, and hand it to vite. There is a tiny TOCTOU window between release
    and vite binding; ``--strictPort`` makes vite fail loudly if it ever loses the
    race, rather than silently serving on a different port. Replaced in tests."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((_dev_host(), 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


async def _default_materialize(*, workspace_id: str, user_id: str, pocket_id: str) -> str:
    """Default source-materialize: run the generator into the PERSISTENT per-pocket
    dir (PERF-3 — node_modules already cached there) and return the SvelteKit
    project dir `vite dev` runs in. We reuse the same build path the preview/edit
    flow uses with ``smoke=False`` (the workerd render is a publish-only gate,
    PERF-4) — generate + cached install is all the dev server needs before it can
    serve + HMR. Reads the pocket's draft content via the sites service the same way
    a preview build does. Replaced in tests by an injected fake."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    pocket = await pockets_service.get(pocket_id, user_id)
    ripple_spec = pocket.get("rippleSpec") or {}
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}
    engine = pocket.get("engine") or "ripple"
    source = pocket.get("source") if isinstance(pocket.get("source"), dict) else None
    title = (pocket.get("name") or "").strip() or "Untitled site"

    build = await GeneratorClient().build(
        ripple_spec=ripple_spec,
        theme=theme,
        # A dev-only id namespace so the dev build dir never collides with the
        # publish/preview build dirs for the same pocket.
        site_id=f"dev-{pocket_id}",
        title=title,
        capture_api_base=os.environ.get("PAW_CAPTURE_API_BASE", "http://localhost:8888/api/v1"),
        capture_signed_key="dev-preview",
        engine=engine,
        source=source,
        # PERF-3: build into the stable per-pocket working dir (cached node_modules).
        pocket_id=f"dev-{pocket_id}",
        # PERF-4: the dev server never needs the workerd smoke render.
        smoke=False,
        # P0a: the dev server serves from SOURCE via `vite dev` (never deploy_local),
        # so it needs NO `.svelte-kit/cloudflare/` static output — skip `bun run
        # build` entirely (generate + cached install is all it needs before `vite
        # dev`). Forcing the static build here would add a wasted full prod build
        # before every dev-server start. (The static build is REQUIRED only on the
        # served preview/publish path — that is where the #1 stale-build bug lived.)
        static_build=False,
    )
    return build.project_dir


class DevServerManager:
    """Async singleton that owns the live `vite dev` servers (one per pocket).

    Resource safety is the priority: the idle reaper + LRU cap together bound the
    number of running servers, and a per-pocket lock prevents two concurrent ensure
    calls from double-spawning. All subprocess/IO is behind injectable seams so the
    full lifecycle is unit-testable without a real vite/bun."""

    def __init__(
        self,
        *,
        idle_seconds: float | None = None,
        max_servers: int | None = None,
        host: str | None = None,
        _spawn: Callable[[list[str], str, int], Awaitable[_Process]] | None = None,
        _free_port: Callable[[], int] | None = None,
        _materialize: Callable[..., Awaitable[str]] | None = None,
    ) -> None:
        self.idle_seconds = idle_seconds if idle_seconds is not None else _idle_seconds()
        self.max_servers = max_servers if max_servers is not None else _max_servers()
        self.host = host or _dev_host()
        self._spawn = _spawn or _default_spawn
        self._free_port = _free_port or _default_free_port
        self._materialize = _materialize or _default_materialize

        # OrderedDict so the registry itself encodes LRU order (front = least
        # recently used). Mutated only under _global_lock.
        self._servers: OrderedDict[str, _DevServer] = OrderedDict()
        self._global_lock = asyncio.Lock()
        # One lock per pocket so concurrent ensure calls for the SAME pocket
        # serialize (no double-spawn) while DIFFERENT pockets proceed in parallel.
        self._pocket_locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task[None] | None = None

    # --- public API --------------------------------------------------------

    async def ensure_dev_server(self, *, workspace_id: str, user_id: str, pocket_id: str) -> str:
        """Return the dev URL for the pocket's live `vite dev` server, starting it
        if needed. A second call for a running pocket touches it (LRU bump) and
        returns the same URL without respawning."""
        lock = self._lock_for(pocket_id)
        async with lock:
            existing = self._servers.get(pocket_id)
            if existing is not None and self._is_running(existing):
                self.touch(pocket_id)
                return existing.url

            # A dead/exited process left a stale entry — drop it before respawning.
            if existing is not None:
                async with self._global_lock:
                    self._servers.pop(pocket_id, None)

            # Enforce the LRU cap BEFORE spawning so we never momentarily exceed it.
            await self._enforce_cap()

            project_dir = await self._materialize(
                workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
            )
            port = self._free_port()
            cmd = _dev_cmd_argv(port, self.host)
            process = await self._spawn(cmd, project_dir, port)
            url = f"http://{self.host}:{port}/"
            server = _DevServer(
                pocket_id=pocket_id,
                workspace_id=workspace_id,
                port=port,
                url=url,
                process=process,
            )
            async with self._global_lock:
                self._servers[pocket_id] = server
                self._servers.move_to_end(pocket_id)  # most-recently-used = back
            logger.info(
                "dev-server: started for pocket %s on %s (cwd=%s)", pocket_id, url, project_dir
            )
            return url

    def touch(self, pocket_id: str) -> None:
        """Bump a server's last_activity and mark it most-recently-used. No-op for
        an unknown pocket."""
        server = self._servers.get(pocket_id)
        if server is None:
            return
        server.last_activity = time.monotonic()
        self._servers.move_to_end(pocket_id)

    async def stop(self, pocket_id: str) -> None:
        """Stop one pocket's dev server (terminate the process, drop the entry).
        Idempotent — stopping an unknown/already-stopped pocket is a no-op."""
        async with self._global_lock:
            server = self._servers.pop(pocket_id, None)
        if server is not None:
            await self._terminate(server)

    async def stop_all(self) -> None:
        """Stop every running dev server. Called on shutdown so no `vite dev`
        outlives the process."""
        async with self._global_lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            await self._terminate(server)

    async def reap_idle(self) -> list[str]:
        """Stop every server idle longer than ``idle_seconds`` and return the list
        of reaped pocket ids. One reaper tick — driven directly in tests, and on a
        timer by the background loop."""
        now = time.monotonic()
        async with self._global_lock:
            stale = [s for s in self._servers.values() if now - s.last_activity > self.idle_seconds]
            for s in stale:
                self._servers.pop(s.pocket_id, None)
        for s in stale:
            await self._terminate(s)
        if stale:
            logger.info("dev-server: reaped %d idle server(s)", len(stale))
        return [s.pocket_id for s in stale]

    def live_pocket_ids(self) -> list[str]:
        """Pocket ids with a live server, LRU order (least → most recent)."""
        return list(self._servers.keys())

    # --- reaper lifecycle (mirrors extensions.start_run_sweeper) -----------

    async def start_reaper(self, *, interval: float | None = None) -> None:
        """Start the background idle-reaper loop (idempotent). Cancelled by
        stop_reaper on shutdown."""
        if self._reaper_task is not None:
            return
        tick = interval if interval is not None else _DEFAULT_REAP_INTERVAL
        self._reaper_task = asyncio.create_task(self._reaper_loop(tick))

    async def stop_reaper(self) -> None:
        """Cancel the reaper loop and wait for it to unwind."""
        from contextlib import suppress

        task = self._reaper_task
        self._reaper_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _reaper_loop(self, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dev-server: reaper tick failed")

    # --- internals ---------------------------------------------------------

    def _lock_for(self, pocket_id: str) -> asyncio.Lock:
        lock = self._pocket_locks.get(pocket_id)
        if lock is None:
            lock = asyncio.Lock()
            self._pocket_locks[pocket_id] = lock
        return lock

    @staticmethod
    def _is_running(server: _DevServer) -> bool:
        """Whether the server's process is still alive. asyncio.subprocess.Process
        exposes ``returncode`` (None == running); the fake proc does too."""
        returncode = getattr(server.process, "returncode", None)
        return returncode is None

    async def _enforce_cap(self) -> None:
        """Stop least-recently-used servers until there is room for one more under
        the cap. Front of the OrderedDict is the LRU. Runs before a spawn so the
        cap is never momentarily exceeded."""
        while True:
            async with self._global_lock:
                if len(self._servers) < self.max_servers:
                    return
                # Pop the least-recently-used (front of the OrderedDict).
                _, victim = self._servers.popitem(last=False)
            await self._terminate(victim)
            logger.info("dev-server: LRU cap reached — stopped pocket %s", victim.pocket_id)

    async def _terminate(self, server: _DevServer) -> None:
        """Terminate a server's process, escalating to kill if it ignores SIGTERM.
        Never raises — a teardown failure must not break the caller (cap eviction,
        reaper, shutdown)."""
        proc = server.process
        try:
            if getattr(proc, "returncode", None) is None:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            # Already gone — fine.
            pass
        except Exception:
            logger.exception("dev-server: error stopping pocket %s", server.pocket_id)


# --- module singleton ------------------------------------------------------

_MANAGER: DevServerManager | None = None


def get_manager() -> DevServerManager:
    """The process-wide DevServerManager singleton. Lazily constructed with the
    default (real-subprocess) seams; tests build their own DevServerManager with
    fakes instead of going through here."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = DevServerManager()
    return _MANAGER


async def ensure_dev_server(*, workspace_id: str, user_id: str, pocket_id: str) -> str:
    """Module-level convenience over the singleton — the endpoint calls this."""
    return await get_manager().ensure_dev_server(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )


async def start_dev_server_reaper() -> None:
    """Start the singleton's idle reaper (called from the cloud boot hook)."""
    await get_manager().start_reaper()


async def stop_dev_servers() -> None:
    """Stop the reaper AND every running dev server (called from cloud shutdown) so
    no `vite dev` process outlives the web process."""
    if _MANAGER is None:
        return
    await _MANAGER.stop_reaper()
    await _MANAGER.stop_all()


__all__ = [
    "DevServerManager",
    "get_manager",
    "ensure_dev_server",
    "start_dev_server_reaper",
    "stop_dev_servers",
]
