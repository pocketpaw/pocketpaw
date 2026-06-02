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
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pocketpaw_ee.sites.generator_client import (
    GeneratorClient,
    SmokeGateFailed,
    _gen_cmd_argv,
    _rewrite_ripple_dep,
    _ripple_dep_source,
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
    # Other deps are untouched.
    assert out["devDependencies"]["svelte"] == "^5.0.0"


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
