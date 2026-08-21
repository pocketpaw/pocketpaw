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

# A bare hyphen in its own constant, so no five-hyphen run — and therefore no
# PEM header — exists as a literal in this file. The repo's secret scanner
# (scripts/scan_secrets.py) has a LIVE PEM pattern and uses this same idiom to
# avoid matching itself; a fake key spelled out longhand trips it and fails CI.
_H = "-"
_PEM_BEGIN = f"{_H * 5}BEGIN OPENSSH PRIVATE KEY{_H * 5}"
_PEM_END = f"{_H * 5}END OPENSSH PRIVATE KEY{_H * 5}"

_PRIV = f"{_PEM_BEGIN}\nFAKEKEYBODY\n{_PEM_END}\n"
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


# ---------------------------------------------------------------------------
# The git source path (SHIP-14): source_kind="git" builds from a repo via
# git:sync, decrypting the private-repo token ONLY at deploy and never leaking
# it into an event / status record.
# ---------------------------------------------------------------------------

_GIT_REPO = "https://github.com/paw-demo/app.git"
_GIT_REF = "release"
_GIT_TOKEN = "ghp_S3cr3tDeployTokenNeverLeakXYZ789"


async def _git_app_and_deploy(workspace="w1", *, token=None):
    """An app whose source is a git repo, plus a queued deploy for it."""
    box = await _ready_box(workspace)
    app = await store.create_app(
        workspace_id=workspace,
        box_id=str(box.id),
        name=APP,
        build_path="dockerfile",
        git_ref="",
        image="",
        env_refs=[],
        prod=False,
    )
    await store.set_app_source(
        app,
        source_kind="git",
        repo_url=_GIT_REPO,
        repo_ref=_GIT_REF,
        repo_token=token,
    )
    deploy = await store.create_deploy(workspace_id=workspace, app_id=str(app.id), image="")
    return box, app, deploy


def _git_replies(*, token=None, fail=False):
    """Fake-engine replies covering the git:sync command the driver issues."""
    from tests.cloud.ship.conftest import SHIP3_REPLIES

    url = f"https://x-access-token:{token}@github.com/paw-demo/app.git" if token else _GIT_REPO
    transcript = "git_sync_build_fail.txt" if fail else "git_sync.txt"
    return {**SHIP3_REPLIES, f"dokku git:sync --build {APP} {url} {_GIT_REF}": transcript}


async def test_git_source_deploys_via_git_sync(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    """A git-source app builds through git:sync — NOT git:from-image."""
    issued = install_fake_engine(monkeypatch, replies=_git_replies())
    _box, _app, deploy = await _git_app_and_deploy()

    await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    assert any(f"dokku git:sync --build {APP} {_GIT_REPO} {_GIT_REF}" == c for c in issued)
    assert not any("git:from-image" in c for c in issued)
    landed = await store.get_deploy("w1", str(deploy.id))
    assert landed is not None and landed.status == "live"


async def test_git_source_private_token_never_leaks(
    mongo_db,
    enc_key,
    monkeypatch,
    recording_bus,
):
    """The private-repo token is decrypted only at deploy — never in the deploy
    record, an emitted event, or (via the driver's redaction) a log line."""
    issued = install_fake_engine(monkeypatch, replies=_git_replies(token=_GIT_TOKEN))
    _box, _app, deploy = await _git_app_and_deploy(token=_GIT_TOKEN)

    await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    # The token reached the driver (a tokenized clone URL was issued)...
    assert any("x-access-token" in c for c in issued)
    # ...but never the persisted attempt.
    landed = await store.get_deploy("w1", str(deploy.id))
    blob = landed.model_dump_json() if landed is not None else ""
    assert _GIT_TOKEN not in blob
    # ...and never an emitted event payload.
    for event in recording_bus.events:
        assert _GIT_TOKEN not in str(getattr(event, "data", event))


async def test_git_source_build_failure_is_recorded_not_raised(
    mongo_db,
    enc_key,
    monkeypatch,  # noqa: ARG001
):
    """A failed build (bad Dockerfile / buildpack) → a ``failed`` attempt with the
    log tail, never a hang or a raise."""
    install_fake_engine(monkeypatch, replies=_git_replies(fail=True))
    _box, _app, deploy = await _git_app_and_deploy()

    await deploy_job.deploy_app_job({}, str(deploy.id), "w1")

    landed = await store.get_deploy("w1", str(deploy.id))
    assert landed is not None and landed.status == "failed"


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
