# Tests for the Sites generator client (RFC 12, Task 2.3).
# Created: 2026-05-30 (feat/paw-sites-backend) — exercises the Python bridge to
# the paw-sites-gen CLI + workerd smoke gate. The subprocess calls are isolated
# behind a fake _runner so the orchestration is unit-testable WITHOUT Bun/workerd
# (no node/bun is ever spawned here):
#   * build runs generate, install, then smoke, in that order, and returns the dir.
#   * build raises SmokeGateFailed when the workerd smoke render fails — proving
#     the gate fails closed and the site is NOT allowed past it to deploy.
# Updated 2026-06-01 (Phase 3 Gap 1): the _runner gained an install() step (the
# generated project's `bun install` with the @ripple-ui/svelte dep rewritten to a
# resolvable source). The fake records it so order is asserted, and a new test
# proves a failed install fails the gate closed (no smoke, no deploy). The dep
# rewrite + gen-cmd resolution are covered by direct helper tests so the env knobs
# are pinned without spawning bun/node.
# Updated 2026-07-09 (fix/sites-gen-windows-process-kill): added tests for the
# build-timeout kill path on Windows. The old _kill_process_group unconditionally
# called os.killpg/os.getpgid (POSIX-only), so on a Windows host a build TIMEOUT
# crashed with AttributeError inside the timeout handler — masking _BuildTimeout
# and escaping publish_pocket as an unhandled 500. These tests simulate win32
# (deleting the POSIX os attrs so any reference explodes) and assert the killer
# uses taskkill /T for the child tree instead, and that _communicate_bounded still
# raises _BuildTimeout end-to-end. A linux-branch test guards the POSIX path.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pocketpaw_ee.sites import generator_client as gc
from pocketpaw_ee.sites.generator_client import (
    GeneratorClient,
    SmokeGateFailed,
    _BuildTimeout,
    _communicate_bounded,
    _gen_cmd_argv,
    _kill_process_group,
    _rewrite_ripple_dep,
    _ripple_dep_source,
    _ripple_motion_dep,
)


class _FakeRunner:
    """Stands in for the bun/paw-sites-gen + bun install + workerd subprocess
    calls."""

    def __init__(
        self,
        *,
        generate_result: dict,
        smoke_ok: bool,
        smoke_reason: str = "",
        install_ok: bool = True,
        install_reason: str = "",
    ) -> None:
        self.generate_result = generate_result
        self.smoke_ok = smoke_ok
        self.smoke_reason = smoke_reason
        self.install_ok = install_ok
        self.install_reason = install_reason
        self.calls: list[str] = []

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        return self.generate_result

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return self.install_ok, self.install_reason

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("smoke")
        return self.smoke_ok, self.smoke_reason


@pytest.mark.asyncio
async def test_build_runs_generate_install_smoke_then_returns_dir():
    runner = _FakeRunner(
        generate_result={"projectDir": "/tmp/s", "rippleVersion": "0.2.0"}, smoke_ok=True
    )
    client = GeneratorClient(_runner=runner)
    result = await client.build(
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        site_id="site_1",
        title="Bright Smile",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )
    assert result.project_dir == "/tmp/s"
    # install runs between generate and smoke so deps resolve before the build.
    assert runner.calls == ["generate", "install", "smoke"]


@pytest.mark.asyncio
async def test_build_raises_when_smoke_gate_fails():
    runner = _FakeRunner(
        generate_result={"projectDir": "/tmp/s", "rippleVersion": "0.2.0"},
        smoke_ok=False,
        smoke_reason="workerd SSR failure: window is not defined",
    )
    client = GeneratorClient(_runner=runner)
    with pytest.raises(SmokeGateFailed) as exc:
        await client.build(
            ripple_spec={"type": "container"},
            theme={},
            site_id="s",
            title="t",
            capture_api_base="x",
            capture_signed_key="k",
        )
    assert "window is not defined" in str(exc.value)
    # generate + install ran, smoke ran, but we must NOT have proceeded past it.
    assert runner.calls == ["generate", "install", "smoke"]


@pytest.mark.asyncio
async def test_build_raises_when_install_fails_and_skips_smoke():
    """Gap 1: if `bun install` on the generated project fails, the gate fails
    closed — smoke never runs and the site is NOT allowed past to deploy."""
    runner = _FakeRunner(
        generate_result={"projectDir": "/tmp/s", "rippleVersion": "0.2.0"},
        smoke_ok=True,
        install_ok=False,
        install_reason="bun install failed (exit 1): could not resolve @ripple-ui/svelte",
    )
    client = GeneratorClient(_runner=runner)
    with pytest.raises(SmokeGateFailed) as exc:
        await client.build(
            ripple_spec={"type": "container"},
            theme={},
            site_id="s",
            title="t",
            capture_api_base="x",
            capture_signed_key="k",
        )
    assert "bun install failed" in str(exc.value)
    # install failed → smoke must NOT have run.
    assert runner.calls == ["generate", "install"]


def test_rewrite_ripple_dep_points_at_resolvable_source(tmp_path: Path):
    """Gap 1: the generated package.json pins an unpublished @ripple-ui/svelte;
    the rewrite swaps just that one dep to a resolvable source, leaving the rest
    untouched, so `bun install` can resolve it."""
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "name": "paw-site-x",
                "dependencies": {"@ripple-ui/svelte": "0.2.0"},
                "devDependencies": {"svelte": "^5.0.0"},
            }
        )
    )
    _rewrite_ripple_dep(str(tmp_path), "file:/tmp/ripple-ui-svelte-0.2.0.tgz")
    out = json.loads(pkg.read_text())
    assert out["dependencies"]["@ripple-ui/svelte"] == "file:/tmp/ripple-ui-svelte-0.2.0.tgz"
    # motion.dev is injected too — ripple's runtime lazy-loads it and the file:
    # ripple dep won't hoist it, so the generated build can't resolve it without
    # an explicit dep. (Same break that hit paw-enterprise.)
    assert "motion" in out["dependencies"]
    # Other deps are untouched.
    assert out["devDependencies"]["svelte"] == "^5.0.0"


def test_rewrite_keeps_an_explicit_motion_pin(tmp_path: Path):
    """If the template already declares motion, the rewrite respects that pin
    rather than clobbering it."""
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "name": "paw-site-x",
                "dependencies": {"@ripple-ui/svelte": "0.2.0", "motion": "^12.99.0"},
            }
        )
    )
    _rewrite_ripple_dep(str(tmp_path), "file:/tmp/ripple.tgz")
    out = json.loads(pkg.read_text())
    assert out["dependencies"]["motion"] == "^12.99.0"


def test_gen_cmd_and_ripple_dep_env_knobs(monkeypatch):
    """Gap 1: the generator command + ripple dep source are env-overridable so a
    fresh box with no installed bin / unpublished ripple still builds."""
    # Defaults.
    monkeypatch.delenv("PAW_SITES_GEN_CMD", raising=False)
    monkeypatch.delenv("PAW_SITES_RIPPLE_DEP", raising=False)
    assert _gen_cmd_argv() == ["paw-sites-gen"]
    assert _ripple_dep_source() == "0.2.0"
    # Overrides: a multi-token "node <path>" command is tokenised into argv.
    monkeypatch.setenv("PAW_SITES_GEN_CMD", "node /abs/paw-sites/dist/cli.js")
    monkeypatch.setenv("PAW_SITES_RIPPLE_DEP", "file:/tmp/ripple-ui-svelte-0.2.0.tgz")
    assert _gen_cmd_argv() == ["node", "/abs/paw-sites/dist/cli.js"]
    assert _ripple_dep_source() == "file:/tmp/ripple-ui-svelte-0.2.0.tgz"
    # motion dep spec is env-overridable too, defaulting to ripple's pin.
    monkeypatch.delenv("PAW_SITES_MOTION_DEP", raising=False)
    assert _ripple_motion_dep() == "^12.40.0"
    monkeypatch.setenv("PAW_SITES_MOTION_DEP", "^12.41.0")
    assert _ripple_motion_dep() == "^12.41.0"


# --- build-timeout kill path: cross-platform (fix/sites-gen-windows-process-kill) ---


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process: a pid plus the sync
    kill()/terminate() and async wait() the kill path touches."""

    def __init__(self, pid: int | None = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = -9
        return -9


def _simulate_windows(monkeypatch) -> None:
    """Pretend we're on Windows AND remove the POSIX-only os attrs, so any code
    that still reaches for os.killpg/os.getpgid explodes exactly as it does on a
    real Windows host (that AttributeError is the bug under test)."""
    monkeypatch.setattr(gc.sys, "platform", "win32", raising=False)
    monkeypatch.delattr(gc.os, "killpg", raising=False)
    monkeypatch.delattr(gc.os, "getpgid", raising=False)


def test_kill_process_group_on_windows_uses_taskkill_not_killpg(monkeypatch):
    """On Windows there are no setsid process groups; the killer must fall back to
    `taskkill /F /T` (which reaps the leaked workerd child tree) and must NOT touch
    os.killpg — the AttributeError that crashed publish on a Windows host."""
    _simulate_windows(monkeypatch)
    ran: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        ran.append(argv)

        class _Completed:
            returncode = 0

        return _Completed()

    monkeypatch.setattr(gc.subprocess, "run", _fake_run)

    # Must not raise AttributeError (the pre-fix crash).
    _kill_process_group(_FakeProc(pid=4242))

    assert len(ran) == 1, "taskkill should be invoked exactly once"
    argv = ran[0]
    assert argv[0] == "taskkill"
    assert "/T" in argv and "/F" in argv, "must kill the whole child tree, forcibly"
    assert "4242" in argv, "must target the launched proc's pid"


def test_kill_process_group_windows_taskkill_missing_falls_back_to_kill(monkeypatch):
    """If taskkill is unavailable/blocked, degrade to a best-effort parent kill
    rather than letting the exception escape the timeout handler."""
    _simulate_windows(monkeypatch)

    def _boom(*a, **k):
        raise FileNotFoundError("taskkill not found")

    monkeypatch.setattr(gc.subprocess, "run", _boom)
    proc = _FakeProc(pid=4242)

    _kill_process_group(proc)  # must not raise

    assert proc.killed is True


def test_kill_process_group_on_posix_uses_killpg(monkeypatch):
    """Guard the Unix path: on POSIX we still SIGKILL the process GROUP so the
    leaked workerd child dies with the bun parent."""
    monkeypatch.setattr(gc.sys, "platform", "linux", raising=False)
    # signal.SIGKILL doesn't exist on a Windows dev host, so inject it — this keeps
    # the POSIX-branch assertion runnable regardless of where the suite executes.
    monkeypatch.setattr(gc.signal, "SIGKILL", 9, raising=False)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(gc.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(gc.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)), raising=False)

    _kill_process_group(_FakeProc(pid=4242))

    assert calls == [(4242, 9)]


@pytest.mark.asyncio
async def test_communicate_bounded_timeout_raises_buildtimeout_on_windows(monkeypatch):
    """End-to-end reproduction of the crash from the field log: a build subprocess
    that hangs past the timeout, on a Windows host, must surface _BuildTimeout — the
    step's real failure contract — NOT an AttributeError from the kill path."""
    _simulate_windows(monkeypatch)
    monkeypatch.setattr(gc.subprocess, "run", lambda *a, **k: None)

    class _HangingProc(_FakeProc):
        async def communicate(self):
            import asyncio

            await asyncio.sleep(3600)  # never returns within the timeout

    with pytest.raises(_BuildTimeout) as excinfo:
        await _communicate_bounded(_HangingProc(pid=4242), timeout_s=0.05, label="bun install")

    assert excinfo.value.label == "bun install"
