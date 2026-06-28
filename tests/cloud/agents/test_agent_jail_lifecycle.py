# tests/cloud/agents/test_agent_jail_lifecycle.py
# Created 2026-06-26 (ART-3) — locks the agent-jail lifecycle contract:
#   * per-workspace quota: over-limit -> clear rejection message; under-limit,
#     disabled (0), and no-workspace -> None; sums across a workspace's sessions
#   * scan_jail_dir: total size + NEWEST mtime over the tree (nested files count,
#     so a build that only writes deep subdirs isn't misread as idle)
#   * TTL GC: an idle jail past the grace is evicted; a fresh idle jail is kept
#   * active-protection: a jail backing a queued/running run is NEVER evicted
#     (both the per-session dir and the workspace _shared dir), and a TERMINAL
#     run does not protect its idle jail
#   * watermark: idle jails are evicted LRU-first, and only while over the mark
"""Agent-jail lifecycle — quota / TTL-GC / watermark (ART-3)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from pocketpaw_ee.cloud import agent_jail, agent_jail_gc


@pytest.fixture(autouse=True)
def _jail_root(tmp_path, monkeypatch):
    """Anchor the jail under tmp_path so tests never touch the real home dir."""
    root = tmp_path / "jail"
    monkeypatch.setenv("POCKETPAW_WORKSPACE_JAIL_ROOT", str(root))
    return root


def _make_jail(
    root: Path,
    workspace_id: str,
    segment: str,
    *,
    files: dict[str, int] | None = None,
    mtime: float | None = None,
) -> Path:
    """Create ``<root>/<ws>/agent/<seg>/`` with ``files`` (``{relpath: bytes}``).

    When ``mtime`` is given, stamp the whole subtree to it so ``scan_jail_dir``
    reads a deterministic last-activity (utime never cascades, so order is moot).
    """
    jail = root / workspace_id / "agent" / segment
    jail.mkdir(parents=True, exist_ok=True)
    for rel, size in (files or {}).items():
        target = jail / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
    if mtime is not None:
        for path in jail.rglob("*"):
            os.utime(path, (mtime, mtime))
        os.utime(jail, (mtime, mtime))
    return jail


async def _insert_run(
    *, workspace_id: str, context_type: str, scope_id: str, status: str, cmid: str
):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    doc = ChatRunDoc(
        run_id=f"r-{cmid}",
        workspace=workspace_id,
        context_type=context_type,
        scope_id=scope_id,
        session_key="k",
        user_id="u1",
        agent_id="a1",
        client_message_id=cmid,
        user_message_id="m1",
        status=status,  # type: ignore[arg-type]
    )
    await doc.insert()
    return doc


# --------------------------------------------------------------------------- #
# Quota (run-start measurement)
# --------------------------------------------------------------------------- #


def test_quota_over_limit_returns_message(_jail_root, monkeypatch):
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_QUOTA_MB", "1")
    _make_jail(_jail_root, "ws1", "s1", files={"big.bin": 2 * 1024 * 1024})
    msg = agent_jail.check_workspace_jail_quota("ws1")
    assert msg is not None
    assert "full" in msg.lower() and "MB" in msg


def test_quota_under_limit_returns_none(_jail_root, monkeypatch):
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_QUOTA_MB", "10")
    _make_jail(_jail_root, "ws1", "s1", files={"small.bin": 1024})
    assert agent_jail.check_workspace_jail_quota("ws1") is None


def test_quota_none_workspace_allowed(_jail_root):
    assert agent_jail.check_workspace_jail_quota(None) is None


def test_quota_disabled_when_zero(_jail_root, monkeypatch):
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_QUOTA_MB", "0")
    _make_jail(_jail_root, "ws1", "s1", files={"big.bin": 5 * 1024 * 1024})
    assert agent_jail.check_workspace_jail_quota("ws1") is None


def test_quota_sums_across_sessions(_jail_root, monkeypatch):
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_QUOTA_MB", "3")
    _make_jail(_jail_root, "ws1", "s1", files={"a.bin": 2 * 1024 * 1024})
    _make_jail(_jail_root, "ws1", "s2", files={"b.bin": 2 * 1024 * 1024})
    # 4 MB across two session dirs > 3 MB cap — the cap is per-workspace, not per-session.
    assert agent_jail.check_workspace_jail_quota("ws1") is not None


def test_quota_over_limit_short_circuits(_jail_root, monkeypatch):
    """The run-start check stops walking once the running sum passes the limit —
    it does not stat the whole tree (the hot-path guarantee for big node_modules
    jails). Two session dirs each already exceed the limit on their own, so
    whichever the DFS reaches first crosses immediately; the other is never
    scanned."""
    limit = 1024
    _make_jail(_jail_root, "ws1", "s1", files={"big1.bin": 4096})
    _make_jail(_jail_root, "ws1", "s2", files={"big2.bin": 4096})

    real_scandir = os.scandir
    scanned: list[str] = []

    def counting_scandir(path):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(agent_jail.os, "scandir", counting_scandir)

    assert agent_jail.workspace_jail_over_quota("ws1", limit) is True

    agent_root = str(_jail_root / "ws1" / "agent")
    session_scans = [p for p in scanned if p.startswith(agent_root) and p != agent_root]
    # Exactly ONE session dir was walked before the early-exit fired; the second
    # (and the rest of the tree) was skipped.
    assert len(session_scans) == 1, f"expected short-circuit after 1 session dir, scanned {scanned}"


# --------------------------------------------------------------------------- #
# scan_jail_dir
# --------------------------------------------------------------------------- #


def test_scan_counts_nested_files_and_newest_mtime(_jail_root):
    jail = _make_jail(
        _jail_root,
        "ws1",
        "s1",
        files={"top.txt": 100, "node_modules/pkg/index.js": 200},
        mtime=time.time() - 10_000,
    )
    size, last_all_old = agent_jail.scan_jail_dir(jail)
    assert size == 300

    # Touch ONLY the deeply-nested file; the top dir's own mtime stays old.
    nested = jail / "node_modules" / "pkg" / "index.js"
    recent = time.time()
    os.utime(nested, (recent, recent))

    _size, last = agent_jail.scan_jail_dir(jail)
    assert last >= recent - 1  # last-activity tracks the nested write
    assert last > last_all_old
    # The top dir's mtime alone would have read the busy jail as idle.
    assert jail.stat().st_mtime <= recent - 9_000


# --------------------------------------------------------------------------- #
# TTL garbage-collection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ttl_evicts_idle_jail_past_grace(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "60")
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", "0")  # isolate TTL
    jail = _make_jail(_jail_root, "ws1", "s_idle", files={"f": 10}, mtime=time.time() - 7200)

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 1
    assert not jail.exists()


@pytest.mark.asyncio
async def test_ttl_keeps_fresh_idle_jail(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "3600")
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", "0")
    jail = _make_jail(_jail_root, "ws1", "s_fresh", files={"f": 10}, mtime=time.time())

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 0
    assert jail.is_dir()


# --------------------------------------------------------------------------- #
# Active-run protection (the safety invariant)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_active_session_jail_not_evicted(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "1")  # would TTL-evict if idle
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", "0")
    jail = _make_jail(_jail_root, "ws1", "sess-active", files={"f": 10}, mtime=time.time() - 7200)
    # A running run whose scope_id == the jail dir name protects it.
    await _insert_run(
        workspace_id="ws1",
        context_type="session",
        scope_id="sess-active",
        status="running",
        cmid="c1",
    )

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 0
    assert jail.is_dir()


@pytest.mark.asyncio
async def test_shared_jail_protected_by_bridge_run(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "1")
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", "0")
    jail = _make_jail(
        _jail_root,
        "ws1",
        agent_jail._SESSIONLESS_DIRNAME,
        files={"f": 10},
        mtime=time.time() - 7200,
    )
    # The sessionless DM/group/pocket bridge shares the per-workspace _shared dir;
    # any non-session run in the workspace protects it.
    await _insert_run(
        workspace_id="ws1", context_type="dm", scope_id="dm-xyz", status="queued", cmid="c2"
    )

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 0
    assert jail.is_dir()


@pytest.mark.asyncio
async def test_terminal_run_does_not_protect_jail(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "60")
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", "0")
    jail = _make_jail(_jail_root, "ws1", "sess-done", files={"f": 10}, mtime=time.time() - 7200)
    # A completed (terminal) run must NOT keep its idle jail alive.
    await _insert_run(
        workspace_id="ws1",
        context_type="session",
        scope_id="sess-done",
        status="completed",
        cmid="c3",
    )

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 1
    assert not jail.exists()


# --------------------------------------------------------------------------- #
# Disk-watermark eviction (LRU-first, only while over the mark)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_watermark_evicts_lru_idle_first(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    # Huge TTL so neither dir is TTL-evicted; both reach the watermark stage.
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "999999")
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", "90")
    now = time.time()
    older = _make_jail(_jail_root, "ws1", "s_old", files={"f": 10}, mtime=now - 5000)
    newer = _make_jail(_jail_root, "ws1", "s_new", files={"f": 10}, mtime=now - 100)

    evicted_paths: list[Path] = []
    real_evict = agent_jail.evict_jail_dir

    def fake_disk() -> float:
        # Over the mark until one dir is reclaimed, then back under.
        return 95.0 if not evicted_paths else 80.0

    def spy_evict(path: Path) -> bool:
        evicted_paths.append(Path(path))
        return real_evict(path)

    monkeypatch.setattr(agent_jail_gc.agent_jail, "disk_usage_pct", fake_disk)
    monkeypatch.setattr(agent_jail_gc.agent_jail, "evict_jail_dir", spy_evict)

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 1
    # Only the least-recently-used (oldest) jail was evicted; eviction stopped
    # as soon as disk dropped under the mark.
    assert older in evicted_paths and newer not in evicted_paths
    assert not older.exists() and newer.is_dir()


@pytest.mark.asyncio
async def test_watermark_noop_when_under_mark(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "999999")
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", "90")
    jail = _make_jail(_jail_root, "ws1", "s1", files={"f": 10}, mtime=time.time() - 5000)
    monkeypatch.setattr(agent_jail_gc.agent_jail, "disk_usage_pct", lambda: 50.0)

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 0
    assert jail.is_dir()


@pytest.mark.asyncio
async def test_gc_disabled_via_env(_jail_root, mongo_db, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_GC_ENABLED", "false")
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", "1")
    jail = _make_jail(_jail_root, "ws1", "s_idle", files={"f": 10}, mtime=time.time() - 7200)

    n = await agent_jail_gc.sweep_agent_jails()

    assert n == 0
    assert jail.is_dir()
