# tests/ee/sites/test_dev_server.py
# Created: 2026-06-18 (feat/sites-devserver, Phase 2 / P2a) — unit tests for the
# live Vite dev-server preview manager (ee/pocketpaw_ee/sites/dev_server.py).
#
# Updated 2026-06-26 (feat/sites-dev-bridge-source, S1 — dev source carries the
# edit-bridge): the fake ``_materialize`` seams now accept the new ``builder_origin``
# kwarg ``ensure_dev_server`` threads through, and ``test_ensure_threads_builder_
# origin_to_materialize`` pins that the origin reaches the materialize seam (the
# manager-level half of the dev-path threading — the source-level proof that the
# materialized files carry the bridge lives in test_dev_bridge_source.py).
#
# The manager spawns a long-lived `vite dev` per pocket so edits hot-reload in ~ms
# instead of rebuilding the whole site per edit. These tests exercise the FULL
# lifecycle WITHOUT a real vite/bun: the spawner, the free-port allocator, and the
# source-materialize step are all injected fakes (the manager exposes _spawn,
# _free_port, and _materialize seams for exactly this). So they assert the
# resource-safety logic the captain cares about — start-once, LRU cap, idle reaper,
# stop/stop_all, per-pocket no-double-spawn — deterministically and offline.
#
# Live verification (real vite spawn + HMR) is a manual captain step documented in
# the branch report; it cannot run in CI without bun + the generator on PATH.
from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.sites.dev_server import DevServerManager


class _FakeProc:
    """Stands in for an asyncio subprocess running `vite dev`. Tracks whether it
    is still 'running' so the manager's stop() (terminate + wait) is observable."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        # Terminating a fake proc resolves immediately.
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


def _make_manager(
    *, idle_seconds: float = 600.0, max_servers: int = 3
) -> tuple[DevServerManager, dict]:
    """Build a manager with all three subprocess/IO seams faked. Returns the
    manager plus a ``probe`` dict the test reads to assert spawn behaviour."""
    probe: dict = {"spawned": [], "ports": iter(range(40000, 40100)), "materialized": []}

    def _free_port() -> int:
        return next(probe["ports"])

    async def _materialize(
        *, workspace_id: str, user_id: str, pocket_id: str, builder_origin: str | None = None
    ) -> str:
        # The real seam runs GeneratorClient.build(preview) and returns the
        # persistent per-pocket project dir; the fake just records the call (incl.
        # the S1 builder_origin) and returns a deterministic path so the spawn cwd
        # is observable.
        probe["materialized"].append(pocket_id)
        probe.setdefault("builder_origins", []).append(builder_origin)
        return f"/tmp/site-builds/{pocket_id}"

    async def _spawn(cmd: list[str], cwd: str, port: int) -> _FakeProc:
        proc = _FakeProc(port)
        probe["spawned"].append({"cmd": cmd, "cwd": cwd, "port": port, "proc": proc})
        return proc

    mgr = DevServerManager(
        idle_seconds=idle_seconds,
        max_servers=max_servers,
        _spawn=_spawn,
        _free_port=_free_port,
        _materialize=_materialize,
    )
    return mgr, probe


@pytest.mark.asyncio
async def test_ensure_starts_once_and_reuses_on_second_call():
    """P2a CORE: the first ensure_dev_server spawns one server; a second call for
    the SAME pocket returns the SAME url WITHOUT spawning again (it touches the
    existing server). This is the whole point — a long-lived dev server, not a
    per-edit rebuild."""
    mgr, probe = _make_manager()

    url1 = await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    url2 = await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1")

    assert url1 == url2
    assert url1.startswith("http://127.0.0.1:")
    assert url1.endswith("/")
    # Exactly ONE spawn + ONE materialize across both calls.
    assert len(probe["spawned"]) == 1
    assert probe["materialized"] == ["pk1"]


@pytest.mark.asyncio
async def test_ensure_threads_builder_origin_to_materialize():
    """S1: ensure_dev_server forwards ``builder_origin`` to the materialize seam, so
    the dev-server-materialized SOURCE can carry SE-1's gated edit-bridge. Before S1
    the dev path materialized with no origin, so the dev source had no anchors / no
    bridge (flipping BRIDGE_IN_DEV regressed the overlay)."""
    mgr, probe = _make_manager()

    await mgr.ensure_dev_server(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        builder_origin="https://app.paw.example",
    )

    # The origin reached materialize verbatim (it then rides build()'s builder_origin).
    assert probe["builder_origins"] == ["https://app.paw.example"]


@pytest.mark.asyncio
async def test_ensure_without_builder_origin_passes_none():
    """The gate still holds: with no ``builder_origin``, materialize is called with
    None, so the generator injects no bridge and the dev source stays non-editable
    (matches the publish path's non-editable behaviour)."""
    mgr, probe = _make_manager()

    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1")

    assert probe["builder_origins"] == [None]


@pytest.mark.asyncio
async def test_distinct_pockets_get_distinct_servers():
    mgr, probe = _make_manager()
    url_a = await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk_a")
    url_b = await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk_b")
    assert url_a != url_b
    assert len(probe["spawned"]) == 2


@pytest.mark.asyncio
async def test_lru_cap_stops_oldest_when_exceeded():
    """The cap must prevent unbounded servers: starting a server when the cap is
    full stops the LEAST-recently-used one first (resource safety)."""
    mgr, probe = _make_manager(max_servers=2)

    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk2")
    # Touch pk1 so pk2 becomes the least-recently-used.
    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    # Third distinct pocket exceeds the cap of 2 → the LRU (pk2) is stopped.
    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk3")

    assert set(mgr.live_pocket_ids()) == {"pk1", "pk3"}
    # pk2's process was terminated.
    pk2_proc = probe["spawned"][1]["proc"]
    assert pk2_proc.terminated is True


@pytest.mark.asyncio
async def test_idle_reaper_stops_idle_servers():
    """The reaper stops servers idle longer than idle_seconds and leaves fresh
    ones alone. We drive one tick directly (no real timer) with a tiny idle bound
    and a back-dated last_activity so the test is deterministic."""
    mgr, probe = _make_manager(idle_seconds=0.05)

    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="stale")
    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="fresh")
    # Back-date the stale one's activity well past the idle bound.
    mgr._servers["stale"].last_activity -= 10.0

    reaped = await mgr.reap_idle()

    assert reaped == ["stale"]
    assert mgr.live_pocket_ids() == ["fresh"]
    assert probe["spawned"][0]["proc"].terminated is True
    assert probe["spawned"][1]["proc"].terminated is False


@pytest.mark.asyncio
async def test_touch_bumps_activity():
    mgr, _ = _make_manager()
    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    before = mgr._servers["pk1"].last_activity
    await asyncio.sleep(0.01)
    mgr.touch("pk1")
    assert mgr._servers["pk1"].last_activity > before


@pytest.mark.asyncio
async def test_stop_and_stop_all():
    mgr, probe = _make_manager()
    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    await mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk2")

    await mgr.stop("pk1")
    assert mgr.live_pocket_ids() == ["pk2"]
    assert probe["spawned"][0]["proc"].terminated is True
    # stop() of an unknown pocket is a no-op (idempotent).
    await mgr.stop("pk1")

    await mgr.stop_all()
    assert mgr.live_pocket_ids() == []
    assert probe["spawned"][1]["proc"].terminated is True


@pytest.mark.asyncio
async def test_concurrent_ensure_spawns_only_once():
    """Two concurrent ensure calls for the same pocket must not double-spawn — the
    per-pocket lock serializes them so the second sees the first's server."""
    # A materialize that yields control mid-flight so the two coroutines actually
    # interleave at the await point (where a naive impl would double-spawn).
    probe: dict = {"spawned": [], "ports": iter(range(40000, 40100))}

    def _free_port() -> int:
        return next(probe["ports"])

    async def _materialize(
        *, workspace_id: str, user_id: str, pocket_id: str, builder_origin: str | None = None
    ) -> str:
        await asyncio.sleep(0.02)  # force interleave
        return f"/tmp/site-builds/{pocket_id}"

    async def _spawn(cmd: list[str], cwd: str, port: int) -> _FakeProc:
        proc = _FakeProc(port)
        probe["spawned"].append(proc)
        return proc

    mgr = DevServerManager(_spawn=_spawn, _free_port=_free_port, _materialize=_materialize)

    urls = await asyncio.gather(
        mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1"),
        mgr.ensure_dev_server(workspace_id="ws1", user_id="u1", pocket_id="pk1"),
    )
    assert urls[0] == urls[1]
    assert len(probe["spawned"]) == 1


@pytest.mark.asyncio
async def test_reaper_loop_start_stop_is_clean():
    """The background reaper task starts and cancels cleanly (mirrors the run
    sweeper lifecycle so on_shutdown can cancel it without a hang)."""
    mgr, _ = _make_manager(idle_seconds=600.0)
    await mgr.start_reaper(interval=0.01)
    assert mgr._reaper_task is not None
    await asyncio.sleep(0.03)  # let it tick at least once
    await mgr.stop_reaper()
    assert mgr._reaper_task is None
