# tests/ee/sites/test_generator_client_deploy.py — the deploy seam.
# Created 2026-06-06 (feat/1346-cf-deploy — Cloudflare deploy pipeline).
#
# Exercises GeneratorClient.build_and_deploy, the ONE call the publish path
# (#1345) makes: build → workerd smoke gate → deploy, returning a DeployResult
# {success, url, error}. The build subprocess calls are faked (the same _FakeRunner
# pattern as test_generator_client.py) and the deploy target is a fake — NOTHING
# spawns bun/workerd and NOTHING hits the network. Covers the captain's gaps:
#   * smoke-gate FAIL → deploy is NOT called; result is {success: False}.
#   * smoke-gate PASS → deploy invoked once → result carries the stable URL.
#   * deploy FAILURE → result is {success: False} ("Live" must not flip true).
#   * the LOCAL (no-CF) path: a local_deploy callable is used and returns a URL.
#   * no deploy target → {success: False} (a misconfig is surfaced, not raised).
from __future__ import annotations

import pytest
from pocketpaw_ee.sites.generator_client import DeployResult, GeneratorClient


class _FakeRunner:
    """Stands in for the generate + bun install + workerd smoke subprocesses."""

    def __init__(self, *, smoke_ok: bool = True, smoke_reason: str = "") -> None:
        self.smoke_ok = smoke_ok
        self.smoke_reason = smoke_reason
        self.calls: list[str] = []

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        return {"projectDir": "/tmp/site_build", "rippleVersion": "0.2.0"}

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("smoke")
        return self.smoke_ok, self.smoke_reason


class _FakeCF:
    """A fake CloudflareClient deploy surface — records the deploy and returns a
    stable URL, or raises to simulate a failed edge deploy."""

    def __init__(self, *, raise_with: Exception | None = None) -> None:
        self.raise_with = raise_with
        self.deployed: list[tuple[str, str]] = []

    async def deploy_site(self, *, script_name: str, project_dir: str) -> str:
        self.deployed.append((script_name, project_dir))
        if self.raise_with is not None:
            raise self.raise_with
        return f"https://paw-sites.workers.dev/{script_name}/"


_BUILD_KW = dict(
    ripple_spec={"type": "container"},
    theme={"primary": "#0A84FF"},
    site_id="site_1",
    title="Bright Smile",
    capture_api_base="https://api.paw.example",
    capture_signed_key="pp_tok_x",
)


@pytest.mark.asyncio
async def test_smoke_gate_fail_blocks_deploy():
    """The gate fails closed: the smoke render fails → the CF client's deploy is
    NEVER called, and the result is {success: False} with the reason."""
    runner = _FakeRunner(smoke_ok=False, smoke_reason="workerd SSR failure: window is not defined")
    cf = _FakeCF()
    result = await GeneratorClient(_runner=runner).build_and_deploy(cloudflare=cf, **_BUILD_KW)

    assert isinstance(result, DeployResult)
    assert result.success is False
    assert result.url is None
    assert "window is not defined" in (result.error or "")
    # Deploy was NOT invoked — the broken site never reached Cloudflare.
    assert cf.deployed == []
    # generate + install + smoke ran; nothing past the gate.
    assert runner.calls == ["generate", "install", "smoke"]


@pytest.mark.asyncio
async def test_smoke_gate_pass_deploys_and_returns_url():
    """Gate passes → deploy is invoked exactly once with the built project dir →
    the result is {success: True, url: <stable url>}."""
    runner = _FakeRunner(smoke_ok=True)
    cf = _FakeCF()
    result = await GeneratorClient(_runner=runner).build_and_deploy(cloudflare=cf, **_BUILD_KW)

    assert result.success is True
    assert result.url == "https://paw-sites.workers.dev/site_1/"
    assert result.error is None
    # Deploy invoked once, with the script name == site id and the build dir.
    assert cf.deployed == [("site_1", "/tmp/site_build")]


@pytest.mark.asyncio
async def test_deploy_failure_surfaces_as_unsuccessful():
    """A failed edge deploy (e.g. CF non-2xx → an exception from deploy_site) is
    reported as {success: False} — "Live" must not flip true."""
    runner = _FakeRunner(smoke_ok=True)
    cf = _FakeCF(raise_with=RuntimeError("Cloudflare API 500"))
    result = await GeneratorClient(_runner=runner).build_and_deploy(cloudflare=cf, **_BUILD_KW)

    assert result.success is False
    assert result.url is None
    assert "deploy failed" in (result.error or "")
    assert "Cloudflare API 500" in (result.error or "")
    # The deploy WAS attempted (gate passed) — it just failed.
    assert cf.deployed == [("site_1", "/tmp/site_build")]


@pytest.mark.asyncio
async def test_local_path_uses_local_deploy_callable():
    """The PAW_SITES_LOCAL (no-CF) path: with no cloudflare client but a
    local_deploy callable, build_and_deploy calls it and returns its URL. Proves
    local dev keeps working with zero CF creds."""
    runner = _FakeRunner(smoke_ok=True)
    seen: list[tuple[str, str]] = []

    def local_deploy(site_id: str, project_dir: str) -> str:
        seen.append((site_id, project_dir))
        return f"http://127.0.0.1:54321/{site_id}/"

    result = await GeneratorClient(_runner=runner).build_and_deploy(
        local_deploy=local_deploy, **_BUILD_KW
    )

    assert result.success is True
    assert result.url == "http://127.0.0.1:54321/site_1/"
    assert seen == [("site_1", "/tmp/site_build")]


@pytest.mark.asyncio
async def test_local_path_still_gated_by_smoke():
    """Even on the local path, a failed smoke gate blocks the (local) deploy."""
    runner = _FakeRunner(smoke_ok=False, smoke_reason="build failed (exit 1)")
    called = False

    def local_deploy(site_id: str, project_dir: str) -> str:
        nonlocal called
        called = True
        return "x"

    result = await GeneratorClient(_runner=runner).build_and_deploy(
        local_deploy=local_deploy, **_BUILD_KW
    )
    assert result.success is False
    assert called is False


@pytest.mark.asyncio
async def test_no_deploy_target_is_surfaced_not_raised():
    """No cloudflare client and no local_deploy → a misconfiguration, returned as
    {success: False} rather than an exception."""
    runner = _FakeRunner(smoke_ok=True)
    result = await GeneratorClient(_runner=runner).build_and_deploy(**_BUILD_KW)
    assert result.success is False
    assert "no deploy target" in (result.error or "")
