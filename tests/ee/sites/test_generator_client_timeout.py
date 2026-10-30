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
    """True if pid is still a live process.

    **``os.kill(pid, 0)`` does not answer this on Windows**, which is what made these
    four tests fail there while the code under test was working correctly. CPython
    emulates signal 0 with ``OpenProcess``, and a process that has EXITED but whose
    handle is still open — which is exactly the state ``proc.wait()`` leaves it in —
    opens fine. So the probe reported "alive" for a process that had been killed and
    reaped, and the assertion blamed the timeout handler for a bug in the assertion.
    Measured 2026-08-12: kill via ``taskkill /F /T``, ``wait()`` returns 1, and
    ``os.kill(pid, 0)`` still succeeds.

    The honest Windows question is the process's EXIT CODE: ``STILL_ACTIVE`` (259) means
    genuinely running, anything else means exited. POSIX keeps signal 0, where it means
    what it says.

    (259 is also a legal real exit code, so a process that exits with 259 reads as alive
    here. It cannot happen in these tests — the child is killed, never exits normally —
    and there is no way to distinguish the two through this API.)
    """
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        _STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # pid is gone entirely
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False  # cannot ask — treat as gone rather than assert on it
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
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
    """Group-kill proof: a CHILD the wedged build leaked (the workerd analogue) dies
    too, not just the parent.

    This is the one test that exercises the actual reason the kill is not just
    ``proc.kill()``: adapter-cloudflare's prerender boots a workerd child that the bun
    parent never reaps, so killing only the parent leaves the real hang in place.

    **Portable, because the production code has TWO implementations and both are load-
    bearing.** It used to spawn its grandchild with ``os.fork()``, which does not exist
    on Windows — the child died instantly, printed nothing, and the test failed parsing
    an empty pid. That left ``_kill_process_tree_windows`` (the ``taskkill /F /T``
    branch, added precisely because ``os.killpg`` raised AttributeError there and
    masked the timeout as an unhandled 500) with no coverage at all on the platform it
    exists for. ``subprocess.Popen`` spawns a grandchild on both, and the grandchild is
    reached by the process-GROUP SIGKILL on POSIX and by ``taskkill``'s ``/T`` recursion
    on Windows.

    On POSIX the grandchild also ignores SIGTERM, so a polite terminate cannot account
    for its death. Windows ``taskkill /F`` is unconditional, so there is no equivalent
    knob — the grandchild simply has to be reached through the tree."""
    monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", "1")

    # The grandchild: ignores SIGTERM where that is meaningful, then sleeps far longer
    # than the test. Only a group SIGKILL (POSIX) or a tree kill (Windows) stops it.
    grandchild_src = "\n".join(
        (
            "import time",
            "try:",
            "    import signal",
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "except (AttributeError, ValueError, OSError):",
            "    pass",
            "time.sleep(999)",
        )
    )
    # The child (the "bun" analogue): spawns that grandchild, prints its pid, sleeps.
    child_src = "\n".join(
        (
            "import subprocess, sys, time",
            f"gc = subprocess.Popen([sys.executable, '-c', {grandchild_src!r}])",
            "print(gc.pid, flush=True)",
            "time.sleep(999)",
        )
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
        # Read the grandchild pid the child prints before either sleeps. A blank line
        # here means the child itself failed to start — assert on that rather than
        # letting int() raise a ValueError that names nothing.
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
        raw = line.decode().strip()
        assert raw.isdigit(), f"child never reported a grandchild pid (got {raw!r})"
        grandchild_pid["pid"] = int(raw)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = _SubprocessRunner()

    ok, msg = await asyncio.wait_for(runner.build_static(str(tmp_path), gate=False), timeout=15)
    assert ok is False
    assert "timed out" in msg

    gc_pid = grandchild_pid["pid"]
    # Give the kill a beat to land, then confirm the leaked child is gone.
    deadline = time.monotonic() + 5
    while _pid_alive(gc_pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _pid_alive(gc_pid), "leaked child survived — the tree/group kill failed"
    # Reap the now-dead grandchild's zombie if it became ours (POSIX only; Windows has
    # no zombies and no waitpid).
    if sys.platform != "win32":
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(gc_pid, os.WNOHANG)
