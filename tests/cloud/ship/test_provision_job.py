# tests/cloud/ship/test_provision_job.py — the arq provision-job entry point.
#
# The lifecycle tests (test_provisioning_lifecycle.py) inject a fake provisioner
# straight into ``run_provision``; they never exercise ``job.py``'s client
# CONSTRUCTION step. This file covers that seam — specifically the regression
# where a missing Hetzner token raised ``ProvisionError`` from
# ``build_hcloud_client`` BEFORE ``run_provision`` ran, leaving the box hung in
# ``provisioning`` forever (surfaced by a live worker run, 2026-07-22). The job
# must turn that into a ``degraded`` box, honouring the same never-hang contract
# ``run_provision`` gives for failures it sees.

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud.ship import job, store

_KEY_ENV = "CLOUD_ENCRYPTION_KEY"
_TOKEN_ENV = "POCKETPAW_HCLOUD_TOKEN"
_PRIV = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"
_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST paw-ship"


@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, Fernet.generate_key().decode())


async def _make_box(workspace="ws-1"):
    return await store.create_provisioning_box(
        workspace_id=workspace,
        provider="hcloud",
        server_type="cx22",
        region="fsn1",
        ssh_private_key=_PRIV,
        ssh_public_key=_PUB,
    )


async def test_missing_token_degrades_the_box_not_hangs(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    """No Hetzner token → the box lands ``degraded``, never stuck ``provisioning``.

    Regression: ``build_hcloud_client`` raises before ``run_provision`` runs, so
    the job itself must catch it and mark the box degraded.
    """
    monkeypatch.delenv(_TOKEN_ENV, raising=False)
    box = await _make_box()

    result = await job.provision_box_job({}, str(box.id), "ws-1")

    assert result["status"] == "degraded"
    reloaded = await store.get_box("ws-1", str(box.id))
    assert reloaded is not None
    assert reloaded.status == "degraded"
    assert "token" in (reloaded.status_reason or "").lower()


async def test_unknown_box_is_a_no_op(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    monkeypatch.delenv(_TOKEN_ENV, raising=False)
    result = await job.provision_box_job({}, "6a60f31a511281a88103bb5f", "ws-1")
    assert result == {"ok": False, "reason": "box_not_found"}
