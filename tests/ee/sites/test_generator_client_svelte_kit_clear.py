# tests/ee/sites/test_generator_client_svelte_kit_clear.py
# Created: 2026-07-09 (fix/sites-svelte-kit-ebusy-windows) — reproduce-first coverage
# for the "every RE-publish 500s on Windows" bug.
#
# Root cause: PERF-3 reuses a per-pocket build dir to cache node_modules, so a prior
# build's `.svelte-kit/cloudflare` survives into the next build. adapter-cloudflare's
# adapt() begins by rimraf-ing that dir, and on Windows a lingering handle (a
# just-exited workerd, or real-time AV scanning the freshly written `_worker.js`)
# makes the rmdir fail `EBUSY: resource busy or locked` -> `bun run build` exits 1 ->
# build_static returns (False, ...) -> SmokeGateFailed -> sites.generator_failed. The
# FIRST publish into a fresh dir works; every reuse hit the stale leftover.
#
# Fix: build_static now wipes `.svelte-kit` (keeping node_modules) BEFORE spawning the
# build, so the adapter's own rimraf has nothing contended to remove. These tests
# exercise the REAL _SubprocessRunner but patch asyncio.create_subprocess_exec to a
# fast exit-0 (or exit-1) child, so no bun/node/workerd is required. They assert:
#   * the reused dir's stale `.svelte-kit` is GONE at the moment the build spawns,
#   * `node_modules` (the install cache) is PRESERVED,
#   * a non-zero build exit surfaces the stderr TAIL in the reason (the opacity fix).
from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites.generator_client import _SubprocessRunner  # noqa: E402

# A child that exits 0 immediately — stands in for a successful `bun run build`.
_OK_ARGV = [sys.executable, "-c", "pass"]


def _seed_reused_dir(project_dir) -> None:
    """Make ``project_dir`` look like a REUSED PERF-3 build dir: a prior build's
    ``.svelte-kit/cloudflare`` output (the leftover the adapter's rimraf trips on)
    plus a populated ``node_modules`` (the expensive install cache that MUST survive)."""
    stale = project_dir / ".svelte-kit" / "cloudflare"
    stale.mkdir(parents=True)
    (stale / "_worker.js").write_text("// stale output from a prior build")
    nm = project_dir / "node_modules" / "svelte"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text("{}")


@pytest.mark.asyncio
async def test_build_static_clears_stale_svelte_kit_before_spawning(tmp_path, monkeypatch):
    """The fix: build_static wipes a reused dir's stale ``.svelte-kit`` BEFORE it
    spawns ``bun run build`` — so adapter-cloudflare never has to rimraf a leftover
    that a lingering Windows handle has locked (EBUSY). ``node_modules`` is preserved
    so PERF-3's install cache still holds."""
    _seed_reused_dir(tmp_path)
    assert (tmp_path / ".svelte-kit").exists()  # precondition: the stale leftover is there

    observed: dict[str, bool] = {}
    real_exec = asyncio.create_subprocess_exec

    async def _fake_exec(*_args, **kwargs):
        # Capture the dir state at the exact moment the build would spawn.
        observed["svelte_kit_present"] = (tmp_path / ".svelte-kit").exists()
        observed["node_modules_present"] = (tmp_path / "node_modules").exists()
        return await real_exec(
            *_OK_ARGV,
            stdout=kwargs.get("stdout", asyncio.subprocess.PIPE),
            stderr=kwargs.get("stderr", asyncio.subprocess.PIPE),
            cwd=kwargs.get("cwd"),
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = _SubprocessRunner()

    ok, msg = await asyncio.wait_for(runner.build_static(str(tmp_path), gate=True), timeout=10)

    # The build ran cleanly (exit 0) once the contended leftover was cleared out of
    # the way — this is the reproduced failure now passing.
    assert ok is True, msg
    # The core assertion: the stale .svelte-kit was gone BEFORE the build spawned, so
    # the adapter's own rimraf had nothing locked to trip on.
    assert observed["svelte_kit_present"] is False, (
        "build_static spawned the build without first clearing the reused dir's stale "
        ".svelte-kit — adapter-cloudflare's rimraf will hit EBUSY on Windows"
    )
    # ...and node_modules (the install cache) was NOT wiped.
    assert observed["node_modules_present"] is True, "the install cache was wrongly cleared"
    assert (tmp_path / "node_modules" / "svelte" / "package.json").exists()


@pytest.mark.asyncio
async def test_build_static_noop_clear_when_no_svelte_kit(tmp_path, monkeypatch):
    """A FRESH dir (no prior .svelte-kit — the first-ever publish, or the throwaway
    tempfile dir) is unaffected: the clear is a no-op and the build still runs."""
    real_exec = asyncio.create_subprocess_exec

    async def _fake_exec(*_args, **kwargs):
        return await real_exec(
            *_OK_ARGV,
            stdout=kwargs.get("stdout", asyncio.subprocess.PIPE),
            stderr=kwargs.get("stderr", asyncio.subprocess.PIPE),
            cwd=kwargs.get("cwd"),
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = _SubprocessRunner()

    ok, _msg = await asyncio.wait_for(runner.build_static(str(tmp_path), gate=False), timeout=10)
    assert ok is True


@pytest.mark.asyncio
async def test_build_failure_reason_surfaces_stderr_tail(tmp_path, monkeypatch):
    """The opacity fix: a non-zero build exit now carries the captured stderr TAIL in
    the failure reason (mirrors the install path), so a future build failure is
    diagnosable from the SmokeGateFailed log instead of an opaque ``exit 1`` — the
    very reason this bug needed a hand repro to root-cause."""
    marker = "PRERENDER_BOOM_a11y_or_ssr_detail"
    fail_argv = [sys.executable, "-c", f"import sys; sys.stderr.write({marker!r}); sys.exit(1)"]
    real_exec = asyncio.create_subprocess_exec

    async def _fake_exec(*_args, **kwargs):
        return await real_exec(
            *fail_argv,
            stdout=kwargs.get("stdout", asyncio.subprocess.PIPE),
            stderr=kwargs.get("stderr", asyncio.subprocess.PIPE),
            cwd=kwargs.get("cwd"),
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = _SubprocessRunner()

    ok, msg = await asyncio.wait_for(runner.build_static(str(tmp_path), gate=True), timeout=10)
    assert ok is False
    assert "exit 1" in msg
    assert marker in msg, f"stderr tail not surfaced in failure reason: {msg!r}"
