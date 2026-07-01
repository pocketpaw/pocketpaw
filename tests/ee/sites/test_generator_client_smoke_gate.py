# tests/ee/sites/test_generator_client_smoke_gate.py
# Created: 2026-06-18 (feat/sites-smoke-at-publish, PERF-4) — covers the
# smoke-gate-only-at-publish behaviour:
#   * build(smoke=False) (the preview/edit/arm path) does NOT run the workerd
#     smoke render — it is per-edit overhead only needed before a LIVE deploy.
#   * build() defaults to smoke=True (the live publish path) and DOES run smoke,
#     keeping the rollback-on-SmokeGateFailed gate unchanged for publish.
#   * A preview build (smoke=False) is no longer blocked by a would-fail smoke
#     render (acceptable — publish still gates + rolls back).
# Asserted via the fake runner's smoke call-count, so no real bun/workerd spawns.
from __future__ import annotations

import pytest
from pocketpaw_ee.sites.generator_client import (
    GeneratorClient,
    SmokeGateFailed,
)


class _CountingRunner:
    """Fake runner that records call order so the smoke decision can be asserted
    without bun/workerd. ``smoke_ok`` controls whether the smoke render passes."""

    def __init__(self, *, smoke_ok: bool = True, smoke_reason: str = "") -> None:
        self.smoke_ok = smoke_ok
        self.smoke_reason = smoke_reason
        self.calls: list[str] = []
        self.smoke_count = 0

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        return {"projectDir": out_dir, "rippleVersion": "0.2.0"}

    def install_inputs_hash(self, project_dir: str) -> str:
        return "h1"

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("smoke")
        self.smoke_count += 1
        return self.smoke_ok, self.smoke_reason


def _build_kwargs(tmp_path) -> dict:
    return dict(
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        site_id="site_1",
        title="Bright Smile",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
        pocket_id="pocket_abc",
    )


@pytest.mark.asyncio
async def test_preview_build_skips_smoke(tmp_path, monkeypatch):
    """PERF-4 core: a PREVIEW build (smoke=False) does NOT run the workerd smoke
    render — the gate is per-edit overhead only needed before a live deploy."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner(smoke_ok=True)
    client = GeneratorClient(_runner=runner)

    result = await client.build(**_build_kwargs(tmp_path), smoke=False)

    # Smoke was SKIPPED — generate + install ran, no smoke.
    assert runner.smoke_count == 0
    assert "smoke" not in runner.calls
    assert runner.calls == ["generate", "install"]
    # The build still produced a BuildResult.
    assert result.project_dir == str(tmp_path / "pocket_abc")


@pytest.mark.asyncio
async def test_publish_build_runs_smoke_by_default(tmp_path, monkeypatch):
    """PERF-4: build() defaults to smoke=True (the live publish path) and DOES run
    the smoke render, so the publish gate is unchanged."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner(smoke_ok=True)
    client = GeneratorClient(_runner=runner)

    await client.build(**_build_kwargs(tmp_path))  # smoke defaults to True

    assert runner.smoke_count == 1
    assert runner.calls == ["generate", "install", "smoke"]


@pytest.mark.asyncio
async def test_publish_build_still_gates_on_smoke_failure(tmp_path, monkeypatch):
    """PERF-4: a publish build (smoke=True) that fails the smoke render still raises
    SmokeGateFailed — the live gate + rollback behaviour is unchanged."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner(
        smoke_ok=False, smoke_reason="workerd SSR failure: window is not defined"
    )
    client = GeneratorClient(_runner=runner)

    with pytest.raises(SmokeGateFailed) as exc:
        await client.build(**_build_kwargs(tmp_path), smoke=True)
    assert "window is not defined" in str(exc.value)
    assert runner.smoke_count == 1


@pytest.mark.asyncio
async def test_preview_build_not_blocked_by_would_fail_smoke(tmp_path, monkeypatch):
    """PERF-4: a preview build (smoke=False) is NOT blocked even when the smoke
    render WOULD fail — smoke never runs, so no SmokeGateFailed is raised
    (acceptable; the live publish still gates + rolls back)."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner(
        smoke_ok=False, smoke_reason="workerd SSR failure: window is not defined"
    )
    client = GeneratorClient(_runner=runner)

    # No raise — the preview build completes despite a would-fail smoke render.
    result = await client.build(**_build_kwargs(tmp_path), smoke=False)
    assert result.project_dir == str(tmp_path / "pocket_abc")
    assert runner.smoke_count == 0
