"""Proof that the known blocking calls on request paths run off the event loop.

Added: 2026-09-04 (fix/unblock-event-loop). PocketPaw serves from a single
uvicorn process with no ``--workers``, so a synchronous call inside an ``async
def`` handler freezes *every* concurrent request for its full duration. Six
sites did exactly that: the git subprocesses in /git/status and /git/diff, the
deflate loop in /files/download-zip, the lsof CWD probe in the terminal shell,
and the os.walk in the cloud-project uploader.

Each test drives the real handler with the blocking call stubbed to sleep,
while a ticker coroutine runs alongside it. The assertion is on the ticker: if
the offload is reverted the ticker records nothing while the handler runs, and
the test fails. Nothing here inspects source text, so a rewrite that keeps the
work off the loop by some other means still passes.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.api.v1 import cloud_projects, files

# The stub sleeps this long; the ticker wakes this often. Windows timer
# resolution is ~15.6 ms, so a 0.4 s window still affords ~25 ticks even when
# the 10 ms interval rounds up. _MIN_TICKS sits well under that, and a blocked
# loop records zero, so the two cases can never be confused.
_BLOCK_SECONDS = 0.4
_TICK_SECONDS = 0.01
_MIN_TICKS = 5

# Fraction of the ACHIEVABLE tick rate a handler must deliver.
#
# Why a fraction and not a floor: /git/status and /git/diff each make TWO
# subprocess calls. A floor of "some ticks happened" is satisfied by offloading
# only ONE of them — the other half of the call blocks the loop and the test
# still passes. Both mutations that revert a single call escaped exactly that
# way before this assertion was proportional.
#
# Why the baseline is MEASURED rather than computed as elapsed/_TICK_SECONDS:
# Windows' timer granularity is ~15.6 ms, so `asyncio.sleep(0.01)` really takes
# ~15.6 ms and the loop delivers roughly 64 ticks/s, not 100. The theoretical
# figure overstates by about 40% — enough to fail a handler that is behaving
# perfectly, which is exactly what it did on the first attempt here.
# Calibrating against an idle window takes the platform out of the assertion.
#
# A fully offloaded handler scores ~1.0 against that baseline and a half-blocked
# one ~0.5, so 0.75 has real margin in both directions.
_MIN_TICK_COVERAGE = 0.75


def _assert_loop_stayed_free(during: int, elapsed: float, rate: float, label: str) -> None:
    """Assert the ticker ran for essentially the whole handler call."""
    expected = rate * elapsed
    assert during >= _MIN_TICKS, f"{label}: loop was blocked outright ({during} ticks)"
    assert during >= expected * _MIN_TICK_COVERAGE, (
        f"{label}: {during} ticks in {elapsed:.2f}s against an idle baseline of "
        f"~{expected:.0f} — part of the handler is still on the event loop"
    )


async def _idle_tick_rate() -> float:
    """Ticks per second this machine actually delivers when the loop is free.

    The control for the measurements below. Any figure derived from
    ``_TICK_SECONDS`` instead describes the sleep we REQUESTED, not the one the
    platform granted.
    """
    _, during, elapsed = await _run_with_ticker(lambda: asyncio.sleep(_BLOCK_SECONDS * 2))
    return during / elapsed


async def _run_with_ticker(factory):
    """Await ``factory()`` while a ticker runs, and count ticks landing inside.

    Returns ``(result, ticks_during, elapsed)``. Only ticks recorded strictly
    between the start and the end of the awaited call are counted — that is
    what makes the assertion sharp, since ticks before and after happen either
    way.
    """
    ticks: list[float] = []
    stop = asyncio.Event()

    async def _ticker() -> None:
        while not stop.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(_TICK_SECONDS)

    task = asyncio.create_task(_ticker())
    await asyncio.sleep(0)  # let the ticker reach its first sleep
    started = time.perf_counter()
    try:
        result = await factory()
    finally:
        finished = time.perf_counter()
        stop.set()
        await task
    during = sum(1 for t in ticks if started < t < finished)
    return result, during, finished - started


def _blocking_run(stdout: str = ""):
    """A ``subprocess.run`` stand-in that blocks, then reports success."""

    def _run(*args, **kwargs):
        time.sleep(_BLOCK_SECONDS)
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    return _run


@pytest.fixture
def jail(tmp_path):
    """A file jail holding a fake git repo, with settings pointed at it."""
    root = tmp_path / "jail"
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tracked.txt").write_text("hello\n", encoding="utf-8")

    settings = MagicMock()
    settings.file_jail_path = root
    with patch("pocketpaw.config.get_settings", return_value=settings):
        yield repo


# ── files.py — /git/status ────────────────────────────────────────────────


async def test_git_status_keeps_the_loop_running(jail):
    rate = await _idle_tick_rate()
    with patch.object(subprocess, "run", _blocking_run("main\n")):
        response, during, elapsed = await _run_with_ticker(lambda: files.git_status(path=str(jail)))

    # Two git invocations (rev-parse + status), so the stub slept twice — and
    # BOTH have to be offloaded. Asserting only that some ticks landed passes
    # when just one of them is.
    assert elapsed >= _BLOCK_SECONDS * 2
    _assert_loop_stayed_free(during, elapsed, rate, "git_status")
    assert response.is_git_repo is True
    assert response.branch == "main"


async def test_git_status_still_reports_a_subprocess_timeout(jail):
    """to_thread has to re-raise the original exception, not wrap it.

    The handler catches ``subprocess.TimeoutExpired`` by type. If the offload
    swallowed or wrapped it the call would fall through to the generic
    ``except Exception`` branch and the error string would change.
    """

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=10)

    with patch.object(subprocess, "run", _timeout):
        response = await files.git_status(path=str(jail))

    assert response.is_git_repo is True
    assert response.error == "Git operation timed out"


# ── files.py — /git/diff ──────────────────────────────────────────────────


async def test_git_diff_keeps_the_loop_running(jail):
    target = jail / "tracked.txt"
    rate = await _idle_tick_rate()

    with patch.object(subprocess, "run", _blocking_run("")):
        response, during, elapsed = await _run_with_ticker(lambda: files.git_diff(path=str(target)))

    # Two git invocations (diff + rev-parse) — both must be offloaded.
    assert elapsed >= _BLOCK_SECONDS * 2
    _assert_loop_stayed_free(during, elapsed, rate, "git_diff")
    assert response.is_git_repo is True


# ── files.py — /files/download-zip ────────────────────────────────────────


class _BlockingZipFile:
    """A ``zipfile.ZipFile`` stand-in whose ``write`` blocks like a deflate."""

    def __init__(self, buf, mode="w", compression=0):
        self._buf = buf

    def __enter__(self) -> _BlockingZipFile:
        return self

    def __exit__(self, *exc_info) -> bool:
        # Minimal end-of-central-directory record, so the response body is at
        # least shaped like an archive.
        self._buf.write(b"PK\x05\x06" + b"\x00" * 18)
        return False

    def write(self, filename, arcname=None) -> None:
        time.sleep(_BLOCK_SECONDS)


async def test_download_zip_keeps_the_loop_running(jail):
    # One eligible file in the directory → exactly one blocking write().
    with patch.object(files.zipfile, "ZipFile", _BlockingZipFile):
        response, during, elapsed = await _run_with_ticker(
            lambda: files.download_dir_as_zip(path=str(jail))
        )

    assert elapsed >= _BLOCK_SECONDS
    assert during >= _MIN_TICKS, f"loop was blocked: {during} ticks in {elapsed:.2f}s"
    assert response.media_type == "application/zip"


# ── cloud_projects.py — the clone uploader's tree walk ────────────────────


class _RecordingAdapter:
    """Storage adapter stand-in that drains each stream and records the key."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def put(self, key, stream, mime) -> None:
        async for _ in stream:
            pass
        self.keys.append(key)


async def test_upload_directory_walk_keeps_the_loop_running(tmp_path):
    base = tmp_path / "clone"
    base.mkdir()
    (base / "a.txt").write_bytes(b"a")

    def _blocking_walk(top, *args, **kwargs):
        time.sleep(_BLOCK_SECONDS)
        return [(str(base), [], ["a.txt"])]

    adapter = _RecordingAdapter()
    with (
        patch.object(cloud_projects.os, "walk", _blocking_walk),
        patch.object(cloud_projects, "_ADAPTER", adapter),
    ):
        _, during, elapsed = await _run_with_ticker(
            lambda: cloud_projects._upload_directory(str(base), "projects/w/u/p/")
        )

    assert elapsed >= _BLOCK_SECONDS
    assert during >= _MIN_TICKS, f"loop was blocked: {during} ticks in {elapsed:.2f}s"
    assert adapter.keys == ["projects/w/u/p/a.txt"]


def test_walk_repo_files_still_prunes_dot_git(tmp_path):
    """The .git prune works by mutating ``dirs`` mid-walk, so it has to live
    inside the helper that runs the walk rather than alongside it."""
    base = tmp_path / "repo"
    (base / ".git" / "objects").mkdir(parents=True)
    (base / ".git" / "config").write_text("x", encoding="utf-8")
    (base / "src").mkdir()
    (base / "src" / "main.py").write_text("y", encoding="utf-8")

    pruned = cloud_projects._walk_repo_files(base, include_git=False)
    kept = cloud_projects._walk_repo_files(base, include_git=True)

    assert [p.name for p in pruned] == ["main.py"]
    assert sorted(p.name for p in kept) == ["config", "main.py"]


# ── terminal.py — the lsof CWD probe ──────────────────────────────────────


@pytest.fixture
def terminal_module():
    """Import the terminal router, faking the POSIX-only modules it needs.

    ``pocketpaw.api.v1.terminal`` imports fcntl / pty / termios at module
    scope, so on Windows it cannot be imported at all — and this is a Windows
    dev machine. The fakes go in only when the real modules are missing, so on
    Linux the test exercises the genuine imports.
    """
    injected: list[str] = []

    try:
        importlib.import_module("fcntl")
    except ImportError:
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.F_GETFL = 3
        fake_fcntl.F_SETFL = 4
        fake_fcntl.fcntl = lambda *a, **k: 0
        fake_fcntl.ioctl = lambda *a, **k: 0
        sys.modules["fcntl"] = fake_fcntl
        injected.append("fcntl")

        fake_termios = types.ModuleType("termios")
        for name, value in (
            ("TIOCSWINSZ", 0x5414),
            ("TCSANOW", 0),
            ("OPOST", 0x1),
            ("ONLCR", 0x4),
            ("ICANON", 0x2),
            ("ECHO", 0x8),
            ("ECHOE", 0x10),
            ("ECHOK", 0x20),
            ("ECHONL", 0x40),
            ("ISIG", 0x80),
        ):
            setattr(fake_termios, name, value)
        fake_termios.tcgetattr = lambda fd: [0, 0, 0, 0, 0, 0, []]
        fake_termios.tcsetattr = lambda *a, **k: None
        sys.modules["termios"] = fake_termios
        injected.append("termios")

        fake_pty = types.ModuleType("pty")
        fake_pty.openpty = lambda: (0, 0)
        sys.modules["pty"] = fake_pty
        injected.append("pty")

    module = importlib.import_module("pocketpaw.api.v1.terminal")
    try:
        yield module
    finally:
        # Leaving fakes in sys.modules would let an unrelated import of a real
        # POSIX module silently pick them up later in the run.
        if injected:
            sys.modules.pop("pocketpaw.api.v1.terminal", None)
        for name in injected:
            sys.modules.pop(name, None)


async def test_terminal_lsof_probe_keeps_the_loop_running(terminal_module):
    """``ensure_running`` is awaited by the SSE, input and resize endpoints.

    Everything around the probe is faked out; the subject is the probe itself,
    a ``subprocess.run`` with a 5 s timeout that used to run inline.
    """
    terminal = terminal_module
    read_fd, write_fd = os.pipe()

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.returncode = None

    async def _fake_exec(*args, **kwargs):
        return fake_proc

    async def _noop_reader(self) -> None:
        return None

    session = terminal.ShellProcess()

    try:
        with (
            # openpty hands back a real pipe so the PROMPT_COMMAND os.write
            # lands somewhere; os.close is neutered so the read end survives
            # it and the write cannot raise BrokenPipeError.
            patch.object(terminal.pty, "openpty", lambda: (write_fd, read_fd)),
            patch.object(terminal.os, "close", lambda fd: None),
            patch.object(terminal.os, "O_NONBLOCK", 0, create=True),
            patch.object(terminal.fcntl, "ioctl", lambda *a, **k: 0),
            patch.object(terminal.fcntl, "fcntl", lambda *a, **k: 0),
            patch.object(terminal.termios, "tcgetattr", lambda fd: [0, 0, 0, 0, 0, 0, []]),
            patch.object(terminal.termios, "tcsetattr", lambda *a, **k: None),
            patch.object(terminal.asyncio, "create_subprocess_exec", _fake_exec),
            patch.object(terminal.ShellProcess, "_reader_loop", _noop_reader),
            patch.object(subprocess, "run", _blocking_run("p4242\nfcwdn/tmp\n")),
        ):
            _, during, elapsed = await _run_with_ticker(session.ensure_running)
    finally:
        if session._task is not None:
            session._task.cancel()
        os.close(read_fd)
        os.close(write_fd)

    assert elapsed >= _BLOCK_SECONDS
    assert during >= _MIN_TICKS, f"loop was blocked: {during} ticks in {elapsed:.2f}s"
    assert session._cwd == "/tmp"
