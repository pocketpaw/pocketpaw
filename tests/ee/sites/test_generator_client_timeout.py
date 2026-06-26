# tests/ee/sites/test_generator_client_timeout.py
# Created: 2026-06-26 (fix/sites-build-subprocess-timeout — stop-gap: bound the
# build subprocesses with a timeout + process-group kill).
#
# Reproduce-first tests for the build-subprocess timeout. They exercise the REAL
# _SubprocessRunner methods (install / build_static / generate) but make the
# subprocess HANG by patching asyncio.create_subprocess_exec to spawn a Python
# `time.sleep(999)` instead of bun — so no bun/node/workerd is ever required and the
# test is fast + deterministic. With PAW_SITES_BUILD_TIMEOUT_SEC=1 each method must:
#   * return / raise in WELL under 999s (the would-hang sleep) — proving the hang is
#     bounded,
#   * return its existing failure contract: install/build_static → (False, "<step>
#     timed out ...") tuple; generate → RuntimeError("generator timed out ..."),
#   * actually REAP the spawned process (proc.returncode is not None — the pid is
#     gone, not a lingering child), and SIGKILL its whole process GROUP.
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time

import pytest
from pocketpaw_ee.sites.generator_client import _SubprocessRunner

# A child that would hang far longer than any test timeout. The real `bun run build`
# wedges on a hung workerd prerender the same way — an unbounded communicate().
_HANG_ARGV = [sys.executable, "-c", "import time; time.sleep(999)"]


def _patch_hang(monkeypatch) -> dict[str, object]:
    """Replace asyncio.create_subprocess_exec so any command the runner launches
    becomes a real, hanging child (a Python `time.sleep(999)`), launched with the
    SAME kwargs the runner passed (notably start_new_session=True). Records the
    spawned proc so the test can assert it gets reaped."""
    spawned: dict[str, object] = {}
    real_exec = asyncio.create_subprocess_exec

    async def _fake_exec(*_args, **kwargs):
        proc = await real_exec(*_HANG_ARGV, **kwargs)
        spawned["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return spawned


def _pid_alive(pid: int) -> bool:
    """True if pid is still a live process (signal 0 probes without killing)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — alive for our purposes.
        return True
    return True


@pytest.mark.asyncio
async def test_build_static_bounds_a_wedged_build(tmp_path, monkeypatch):
    """The #1 wedge: `bun run build` hangs on a stuck workerd prerender. With a 1s
    timeout, build_static must return (False, <"timed out">) in seconds — NOT 999s —
    and the spawned process must be reaped."""
    monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", "1")
    spawned = _patch_hang(monkeypatch)
    runner = _SubprocessRunner()

    started = time.monotonic()
    ok, msg = await asyncio.wait_for(runner.build_static(str(tmp_path), gate=True), timeout=10)
    elapsed = time.monotonic() - started

    # The hang was bounded: well under the 999s sleep (and under a few seconds).
    assert elapsed < 5, f"build_static did not bound the hang (took {elapsed:.1f}s)"
    # Existing (ok, msg) contract: a timeout looks like a normal build failure so
    # build() raises SmokeGateFailed → CloudError → view-only FE.
    assert ok is False
    assert "timed out" in msg

    # The spawned process was actually reaped — not left lingering.
    proc = spawned["proc"]
    assert proc.returncode is not None, "wedged build process was not reaped"
    assert not _pid_alive(proc.pid), "wedged build process is still alive"


@pytest.mark.asyncio
async def test_install_bounds_a_wedged_install(tmp_path, monkeypatch):
    """`bun install` is bounded the same way: on timeout it returns the (False,
    <"timed out">) tuple (its existing non-zero-exit contract) and the process is
    reaped."""
    monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", "1")
    spawned = _patch_hang(monkeypatch)
    runner = _SubprocessRunner()

    started = time.monotonic()
    ok, msg = await asyncio.wait_for(runner.install(str(tmp_path)), timeout=10)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"install did not bound the hang (took {elapsed:.1f}s)"
    assert ok is False
    assert "timed out" in msg

    proc = spawned["proc"]
    assert proc.returncode is not None, "wedged install process was not reaped"
    assert not _pid_alive(proc.pid), "wedged install process is still alive"


@pytest.mark.asyncio
async def test_generate_bounds_a_wedged_generator(tmp_path, monkeypatch):
    """The generator preserves its raise-on-failure contract: a timeout raises
    RuntimeError("generator timed out ..."), bounded well under the would-hang
    sleep, and the process is reaped."""
    monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", "1")
    spawned = _patch_hang(monkeypatch)
    runner = _SubprocessRunner()

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="generator timed out"):
        await asyncio.wait_for(runner.generate({"engine": "ripple"}, str(tmp_path)), timeout=10)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"generate did not bound the hang (took {elapsed:.1f}s)"

    proc = spawned["proc"]
    assert proc.returncode is not None, "wedged generator process was not reaped"
    assert not _pid_alive(proc.pid), "wedged generator process is still alive"


@pytest.mark.asyncio
async def test_process_group_kill_reaps_a_leaked_child(tmp_path, monkeypatch):
    """Group-kill proof: start_new_session=True + os.killpg means a CHILD the
    wedged build leaked (the workerd analogue) dies too, not just the parent.

    The faked parent spawns a grandchild that ignores SIGTERM and sleeps; only a
    process-GROUP SIGKILL takes it down. After the timeout fires, the grandchild's
    pid must be gone."""
    monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", "1")

    # A child that forks a grandchild (the leaked-workerd analogue), prints the
    # grandchild pid, then both sleep. The grandchild ignores SIGTERM, so only a
    # SIGKILL to the whole group reaps it.
    child_src = (
        "import os, sys, time, signal\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(999)\n"
        "else:\n"
        "    sys.stdout.write(str(pid) + '\\n'); sys.stdout.flush()\n"
        "    time.sleep(999)\n"
    )
    child_argv = [sys.executable, "-c", child_src]

    grandchild_pid: dict[str, int] = {}
    real_exec = asyncio.create_subprocess_exec

    async def _fake_exec(*_args, **kwargs):
        proc = await real_exec(
            *child_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=kwargs.get("stderr", asyncio.subprocess.PIPE),
            cwd=kwargs.get("cwd"),
            start_new_session=kwargs.get("start_new_session", False),
        )
        # Read the grandchild pid the child prints before either sleeps.
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        grandchild_pid["pid"] = int(line.decode().strip())
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = _SubprocessRunner()

    ok, msg = await asyncio.wait_for(runner.build_static(str(tmp_path), gate=False), timeout=10)
    assert ok is False
    assert "timed out" in msg

    gc_pid = grandchild_pid["pid"]
    # Give the group SIGKILL a beat to land, then confirm the leaked child is gone.
    deadline = time.monotonic() + 5
    while _pid_alive(gc_pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _pid_alive(gc_pid), "leaked child survived — process-group kill failed"
    # Reap the now-dead grandchild's zombie if it became ours (best-effort).
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(gc_pid, os.WNOHANG)
