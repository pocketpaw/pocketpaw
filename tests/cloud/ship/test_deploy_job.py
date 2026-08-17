# tests/cloud/ship/test_deploy_job.py — the SHIP-3 deploy pipeline.
#
# Covers the arq job and the orchestrator behind it against SHIP-1's recorded
# Dokku transcripts (zero network): the full ``queued -> building -> releasing
# -> live`` walk with an event per transition, the failure path (the attempt is
# recorded ``failed`` with a REDACTED summary, never raised), and the
# workspace-scoped loads that make a cross-tenant job id a no-op.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

from pocketpaw_ee.cloud.ship import deploy_job, store

from tests.cloud.ship.conftest import (
    APP,
    FAILING_REPLIES,
    IMAGE,
    SECRET_MARKERS,
    install_fake_engine,
    install_refused_engine,
)


# The PEM header is ASSEMBLED, never written as a literal — the same idiom
# ``scripts/scan_secrets.py`` uses on itself (see ``_H`` there). Storing the
# five-hyphen run verbatim makes this fixture indistinguishable from a real
# leaked key to the secret scanner, and "it's only a test" is exactly what a
# real leak would also claim. No key material here: the body is a placeholder.
_H = "-" * 5
_PEM_BEGIN = f"{_H}BEGIN OPENSSH PRIVATE KEY{_H}"
_PEM_END = f"{_H}END OPENSSH PRIVATE KEY{_H}"

_PRIV = _PEM_BEGIN + "\nFAKEKEYBODY\n" + _PEM_END + "\n"
_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY paw-ship"


async def _ready_box(workspace="w1"):
    box = await store.create_provisioning_box(
        workspace_id=workspace,
        provider="hcloud",
        server_type="cx22",
        region="fsn1",
        ssh_private_key=_PRIV,
        ssh_public_key=_PUB,
    )
    return await store.mark_ready(box, server_id="srv-1", ip="203.0.113.9", price_monthly=8.25)


async def _app_and_deploy(workspace="w1", *, box=None):
    box = box or await _ready_box(workspace)
    app = await store.create_app(
        workspace_id=workspace,
        box_id=str(box.id),
        name=APP,
        build_path="dockerfile",
        git_ref="",
        image=IMAGE,
        env_refs=[],
        prod=False,
    )
    deploy = await store.create_deploy(workspace_id=workspace, app_id=str(app.id), image=IMAGE)
    return box, app, deploy


async def test_job_walks_the_deploy_to_live(mongo_db, enc_key, monkeypatch, recording_bus):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    _box, app, deploy = await _app_and_deploy()

    result = await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    assert result == {"ok": True, "status": "live"}
    landed = await store.get_deploy("w1", str(deploy.id))
    assert landed is not None
    assert landed.status == "live"
    assert landed.finished_at is not None

    refreshed = await store.get_app("w1", str(app.id))
    assert refreshed is not None
    assert refreshed.status == "live"
    assert refreshed.urls == ["http://demo.paw.example"]

    # Every transition announced itself, in order.
    transitions = [
        e.data["status"] for e in recording_bus.events if e.type == "ship.deploy.status_changed"
    ]
    assert transitions == ["building", "releasing", "live"]


async def test_engine_failure_is_recorded_not_raised(
    mongo_db,
    enc_key,
    monkeypatch,
    recording_bus,  # noqa: ARG001
):
    install_fake_engine(monkeypatch, FAILING_REPLIES)
    _box, app, deploy = await _app_and_deploy()

    result = await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    assert result == {"ok": False, "status": "failed"}
    landed = await store.get_deploy("w1", str(deploy.id))
    assert landed is not None
    assert landed.status == "failed"
    assert landed.finished_at is not None
    assert landed.log_summary  # an operator hint was recorded...
    for marker in SECRET_MARKERS:  # ...and it carries no secret material.
        assert marker not in landed.log_summary

    refreshed = await store.get_app("w1", str(app.id))
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.urls == []


async def test_cross_tenant_deploy_id_is_a_no_op(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    issued = install_fake_engine(monkeypatch)
    _box, _app, deploy = await _app_and_deploy("w1")

    result = await deploy_job.deploy_app_job({}, str(deploy.id), "w-attacker")

    assert result == {"ok": False, "reason": "deploy_not_found"}
    # The victim's attempt was never touched, and no command reached a box.
    untouched = await store.get_deploy("w1", str(deploy.id))
    assert untouched is not None
    assert untouched.status == "queued"
    assert issued == []


async def test_a_box_that_is_not_ready_fails_the_attempt(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    issued = install_fake_engine(monkeypatch)
    box = await _ready_box()
    await store.mark_degraded(box, reason="box did not become reachable")
    _box, app, deploy = await _app_and_deploy(box=box)

    result = await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    assert result["reason"] == "box_not_ready"
    landed = await store.get_deploy("w1", str(deploy.id))
    assert landed is not None
    assert landed.status == "failed"
    assert "degraded" in landed.log_summary
    assert (await store.get_app("w1", str(app.id))).status == "failed"
    assert issued == []


async def test_a_deleted_app_fails_the_attempt(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    _box, app, deploy = await _app_and_deploy()
    await app.delete()

    result = await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    assert result["reason"] == "app_not_found"
    landed = await store.get_deploy("w1", str(deploy.id))
    assert landed is not None
    assert landed.status == "failed"


async def test_the_deploy_pins_the_image_from_the_attempt_not_the_app(
    mongo_db,
    enc_key,
    monkeypatch,  # noqa: ARG001
):
    """A later app edit must not rewrite what an in-flight attempt ships."""
    issued = install_fake_engine(monkeypatch)
    _box, app, deploy = await _app_and_deploy()
    app.image = "registry.paw.example/demo:NEWER"
    await app.save()

    await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    assert f"dokku git:from-image {APP} {IMAGE}" in issued
    assert not any("NEWER" in cmd for cmd in issued)


async def test_an_unreachable_box_fails_the_attempt_without_leaking_its_address(
    mongo_db,
    enc_key,
    monkeypatch,  # noqa: ARG001
):
    """A box that stops answering is an operational failure, not a crash."""
    install_refused_engine(monkeypatch)
    _box, _app, deploy = await _app_and_deploy()

    result = await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    assert result == {"ok": False, "status": "failed"}
    landed = await store.get_deploy("w1", str(deploy.id))
    assert landed is not None
    # The summary names the failure class, never the box's address.
    assert landed.log_summary == "could not reach the box (ConnectionRefusedError)"
