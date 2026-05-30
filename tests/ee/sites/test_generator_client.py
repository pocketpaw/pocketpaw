# Tests for the Sites generator client (RFC 12, Task 2.3).
# Created: 2026-05-30 (feat/paw-sites-backend) — exercises the Python bridge to
# the paw-sites-gen CLI + workerd smoke gate. The subprocess calls are isolated
# behind a fake _runner so the orchestration is unit-testable WITHOUT Bun/workerd
# (no node/bun is ever spawned here):
#   * build runs generate then smoke, in that order, and returns the project dir.
#   * build raises SmokeGateFailed when the workerd smoke render fails — proving
#     the gate fails closed and the site is NOT allowed past it to deploy.
from __future__ import annotations

import pytest
from pocketpaw_ee.sites.generator_client import GeneratorClient, SmokeGateFailed


class _FakeRunner:
    """Stands in for the bun/paw-sites-gen + workerd subprocess calls."""

    def __init__(self, *, generate_result: dict, smoke_ok: bool, smoke_reason: str = "") -> None:
        self.generate_result = generate_result
        self.smoke_ok = smoke_ok
        self.smoke_reason = smoke_reason
        self.calls: list[str] = []

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        return self.generate_result

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("smoke")
        return self.smoke_ok, self.smoke_reason


@pytest.mark.asyncio
async def test_build_runs_generate_then_smoke_then_returns_dir():
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
    assert runner.calls == ["generate", "smoke"]


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
    # smoke ran, but we must NOT have proceeded past it.
    assert runner.calls == ["generate", "smoke"]
