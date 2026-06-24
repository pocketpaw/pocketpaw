# tests/ee/sites/test_generator_client_cache.py
# Created: 2026-06-18 (feat/sites-cached-build, PERF-3) — covers the persistent
# per-pocket build dir + cached node_modules behaviour:
#   * Building the SAME pocket twice with an UNCHANGED dep-hash runs `bun install`
#     only ONCE (the second build reuses the cached node_modules). Asserted via the
#     fake runner's install() call count.
#   * Changing the dep-hash (a new install-input fingerprint) triggers a reinstall.
#   * The build dir is STABLE per pocket: the same pocket builds into the same dir
#     across builds (so node_modules persists), under a configurable build_home().
#   * Correctness: a build still produces a BuildResult with the project dir.
# The gen/install/smoke/hash subprocess calls are faked behind the injected
# _runner seam, so no real bun/workerd is spawned. The dep-hash itself is supplied
# by the runner's install_inputs_hash() so a test can flip it deterministically.
from __future__ import annotations

import pytest
from pocketpaw_ee.sites.generator_client import (
    GeneratorClient,
    SmokeGateFailed,
    build_home,
)


class _CountingRunner:
    """Fake runner that records call order + counts and serves a controllable
    install-input hash, so the cache decision can be asserted without bun.

    ``hash_value`` is what install_inputs_hash() returns; flip it between builds
    to simulate a changed dependency set (which must force a reinstall)."""

    def __init__(self, *, hash_value: str = "h1") -> None:
        self.hash_value = hash_value
        self.calls: list[str] = []
        self.install_count = 0

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        # The real generator writes into out_dir and reports the project dir; the
        # persistent-dir contract is that projectDir == the stable build dir.
        return {"projectDir": out_dir, "rippleVersion": "0.2.0"}

    def install_inputs_hash(self, project_dir: str) -> str:
        return self.hash_value

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        self.install_count += 1
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("smoke")
        return True, "ok"


def _build_kwargs():
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
async def test_second_build_unchanged_deps_skips_install(tmp_path, monkeypatch):
    """PERF-3 core: two builds of the same pocket with an UNCHANGED dep-hash run
    `bun install` exactly ONCE. The second build reuses the cached node_modules."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner(hash_value="h1")
    client = GeneratorClient(_runner=runner)

    first = await client.build(**_build_kwargs())
    second = await client.build(**_build_kwargs())

    # Install ran on the first build, was SKIPPED on the second (hash unchanged).
    assert runner.install_count == 1
    # Both builds still generate + smoke; only install is cached.
    assert runner.calls == ["generate", "install", "smoke", "generate", "smoke"]
    # Both builds produced a BuildResult at the SAME stable per-pocket dir.
    assert first.project_dir == second.project_dir


@pytest.mark.asyncio
async def test_changed_dep_hash_triggers_reinstall(tmp_path, monkeypatch):
    """A changed install-input fingerprint (new deps / lockfile) forces a reinstall
    so a stale node_modules never serves old deps."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner(hash_value="h1")
    client = GeneratorClient(_runner=runner)

    await client.build(**_build_kwargs())
    assert runner.install_count == 1

    # Dependency set changed → new hash → must reinstall.
    runner.hash_value = "h2-changed"
    await client.build(**_build_kwargs())
    assert runner.install_count == 2


@pytest.mark.asyncio
async def test_build_dir_is_stable_per_pocket(tmp_path, monkeypatch):
    """The build dir is derived from pocket_id under build_home(), so the same
    pocket always materializes into the same dir (node_modules persists there)."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner()
    client = GeneratorClient(_runner=runner)

    result = await client.build(**_build_kwargs())
    expected = build_home() / "pocket_abc"
    assert result.project_dir == str(expected)


@pytest.mark.asyncio
async def test_different_pockets_get_different_dirs(tmp_path, monkeypatch):
    """Two distinct pockets must not share a build dir (their node_modules and
    sources are independent)."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _CountingRunner()
    client = GeneratorClient(_runner=runner)

    a = await client.build(**{**_build_kwargs(), "pocket_id": "pocket_a"})
    b = await client.build(**{**_build_kwargs(), "pocket_id": "pocket_b"})
    assert a.project_dir != b.project_dir


def test_build_home_is_env_overridable(tmp_path, monkeypatch):
    """build_home() mirrors sites_home(): a configurable base dir, env-overridable
    via PAW_SITES_BUILD_DIR, created on demand."""
    target = tmp_path / "custom-build-home"
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(target))
    home = build_home()
    assert home == target
    assert home.is_dir()


class _FailingInstallRunner(_CountingRunner):
    """A runner whose install() fails — exercises the cache fail-closed path."""

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        self.install_count += 1
        return False, "bun install failed (exit 1): could not resolve @ripple-ui/svelte"


@pytest.mark.asyncio
async def test_failed_install_does_not_poison_cache(tmp_path, monkeypatch):
    """A failed install must (a) fail the gate closed (SmokeGateFailed, smoke never
    runs) and (b) NOT record the hash sentinel, so the NEXT build retries the
    install rather than skipping it on a never-installed dir."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _FailingInstallRunner(hash_value="h1")
    client = GeneratorClient(_runner=runner)

    with pytest.raises(SmokeGateFailed):
        await client.build(**_build_kwargs())
    # Install ran and failed; smoke must NOT have run.
    assert runner.calls == ["generate", "install"]

    # Same deps, but because the prior install failed (no sentinel recorded), the
    # next build retries the install instead of treating it as cached.
    with pytest.raises(SmokeGateFailed):
        await client.build(**_build_kwargs())
    assert runner.install_count == 2
