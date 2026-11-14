# tests/ee/sites/test_publish_gate_async_lane.py — the publish gate on the async build
# lane (react and static svelte), where the build happens in a worker and the deploy
# runs later in ``deploy_prebuilt_site``.
#
# The flag that says "the gate had no stored build to read, check the one you make"
# rides the job's deploy_inputs to the worker, ``_deploy_site_doc`` checks the prebuilt
# tree before anything deploys, and a refusal there settles the row as
# ``deploy_failed:publish_blocked`` (terminal, republishable, not a crash) with the
# owner-worded findings on the site's build status, like a sync 409.
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import publish_gate
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.faults import FaultyDaytonaClient, clean_artifact
from tests.ee.sites.test_async_publish import _deploy_inputs, _FakeRunner

from .test_verify_checks import _doc

_CAFE = _doc("<p>Find us at [Address].</p>")


async def test_the_flag_rides_to_the_async_lane(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _enqueue(**kw: Any) -> str:
        captured.update(kw)
        return "queued-doc"

    monkeypatch.setattr(sites_service, "_enqueue_static_build", _enqueue)
    monkeypatch.setattr(sites_service, "build_runs_async", lambda *_a, **_k: True)
    out = await sites_service._deploy_site_doc(
        workspace_id="w",
        user_id="u",
        pocket_id="p1",
        site_id="507f1f77bcf86cd799439011",
        signed_key="k",
        site_name="Saltline",
        ripple_spec=None,
        theme={},
        engine="react",
        source={"src/App.tsx": "x"},
        gate_built_output=True,
    )
    assert out == "queued-doc"
    assert captured["gate_built_output"] is True


async def test_the_job_carries_the_flag_in_its_deploy_inputs(beanie_test_db, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _enqueue_site_build(_doc: Any, **kw: Any) -> str:
        captured.update(kw["deploy_inputs"])
        return "job-1"

    async def _no_kwargs(*_a: Any, **_k: Any) -> dict:
        return {}

    monkeypatch.setattr(bj, "enqueue_site_build", _enqueue_site_build)
    monkeypatch.setattr(sites_service, "_turnstile_build_kwargs", _no_kwargs)
    await sites_service._enqueue_static_build(
        workspace_id="w",
        user_id="u",
        pocket_id="p1",
        site_id="507f1f77bcf86cd799439011",
        signed_key="k",
        site_name="Saltline",
        ripple_spec=None,
        theme={},
        engine="react",
        source={"src/App.tsx": "x"},
        assets=None,
        pattern=None,
        builder_origin=None,
        keeps_client_bundle=True,
        gate_built_output=True,
    )
    assert captured["gate_built_output"] is True


async def test_the_worker_deploy_checks_the_prebuilt_tree(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text(_CAFE, encoding="utf-8")

    async def _embed(**_k: Any) -> None:
        raise AssertionError("reached the deploy steps with [Address] on the page")

    monkeypatch.setattr(sites_service, "_embed_concierge_bar", _embed)
    with pytest.raises(publish_gate.PublishBlocked):
        await sites_service.deploy_prebuilt_site(
            project_dir=str(tmp_path),
            deploy_inputs={**_deploy_inputs("507f1f77bcf86cd799439011"), "gate_built_output": True},
        )


async def test_a_worker_refusal_settles_the_row_and_reaches_the_owner(beanie_test_db) -> None:
    site = Site(workspace="ws1", pocket_id="pk1", owner="u1", name="x", build_status="queued")
    await site.insert()
    findings = await publish_gate.blocking_findings(
        {"engine": "html", "source": {"index.html": _CAFE}}, pocket_id="pk1"
    )

    async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
        raise publish_gate.refusal(findings)

    # Not re-raised: a refusal is an outcome, not a crash for the job to retry.
    await bj.run_site_build(
        {},
        "ws1",
        str(site.id),
        {"engine": "react", "siteConfig": {}},
        "react",
        600,
        deploy_inputs=_deploy_inputs(str(site.id)),
        _runner=_FakeRunner(),
        _client=FaultyDaytonaClient(artifact=clean_artifact()),
        _deployer=_deploy,
    )
    fresh = await Site.get(site.id)
    assert fresh.build_status == "failed"
    assert fresh.build_reason == "deploy_failed:publish_blocked"
    state = await sites_service.site_build_status(workspace_id="ws1", pocket_id="pk1")
    blocked = state["publish_blocked"]
    assert blocked["override"] == "confirm_placeholders"
    assert blocked["findings"][0]["text"] == "[Address]"
