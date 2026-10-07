# tests/ee/sites/test_local_switch_wins.py
# PAW_SITES_LOCAL=1 is the "never deploy to Cloudflare" switch, so it has to beat an
# explicit PAW_CF_DEPLOY_MODE. A local run that also picked up PAW_CF_DEPLOY_MODE=workers
# (from a parent directory's .env) published a test site to the real workers.dev
# account, because the mode was read first and the switch never consulted.
#
# Three legs: the resolver answers "local", the static publish path calls the local
# deployer and never the workers deployer, and the dynamic provision job refuses to
# build a Cloudflare client at all (it would otherwise create a real D1 and deploy).

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import service as sites_service


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


def test_deploy_mode_is_local_when_the_switch_and_workers_are_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")

    assert sites_service._deploy_mode() == "local"


async def test_publish_under_the_switch_never_calls_the_workers_deployer(
    beanie_test_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    workers_calls: list[str] = []
    local_calls: list[str] = []

    async def _workers(site_id: str, project_dir: str, **kw) -> str:
        workers_calls.append(site_id)
        return f"https://{site_id}.workers.dev"

    def _local(site_id: str, project_dir: str) -> str:
        local_calls.append(site_id)
        return f"http://127.0.0.1:1/{site_id}/"

    site = await sites_service.publish(
        workspace_id="ws-local-switch",
        user_id="u1",
        pocket_id="pk-local-switch",
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        name="Local Only",
        _generator=_FakeGenerator(),
        _cloudflare=None,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_local,
        _workers_deploy=_workers,
    )

    assert workers_calls == [], "PAW_SITES_LOCAL=1 still reached the workers.dev deploy"
    assert local_calls == [str(site.id)]
    assert site.url.startswith("http://127.0.0.1")


def test_provision_job_cannot_build_a_cloudflare_client_under_the_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "planted-account-not-a-credential")
    monkeypatch.setenv("PAW_CF_API_TOKEN", "planted-token-not-a-credential")
    monkeypatch.setenv("PAW_CF_ZONE_ID", "planted-zone-not-a-credential")

    with pytest.raises(ValidationError) as err:
        sites_service.provision_cf_client()
    assert err.value.code == "sites.local_mode"
