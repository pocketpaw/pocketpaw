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


# ---------------------------------------------------------------------------
# Shared provider credential (fix/ship-review-p0)
# ---------------------------------------------------------------------------


async def test_shared_operator_token_is_refused_in_multi_tenant_cloud(
    mongo_db, enc_key, monkeypatch
):  # noqa: ARG001
    """A process-global Hetzner token must not create servers for tenants.

    connectors/ship.yaml declares a PER-WORKSPACE HCLOUD_TOKEN precisely so the
    central project never holds a shared infrastructure credential. Reading the
    operator's env var in multi-tenant cloud would create and bill every
    tenant's servers on one account, so the job fails closed and the box is
    marked degraded with an actionable reason.
    """
    from pocketpaw_ee.cloud.ship import job as ship_job

    monkeypatch.setenv("POCKETPAW_HCLOUD_TOKEN", "operator-token")
    monkeypatch.setattr(ship_job, "is_multi_tenant_cloud", lambda: True)
    box = await _make_box(workspace="w1")

    result = await ship_job.provision_box_job({}, str(box.id), "w1")

    assert result == {"ok": False, "reason": "shared_provider_token_refused"}
    refreshed = await store.get_box("w1", str(box.id))
    assert refreshed is not None and refreshed.status == "degraded"
    assert "per-workspace" in (refreshed.status_reason or "")
