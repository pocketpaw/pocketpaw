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

# The PEM header is ASSEMBLED, never written as a literal — the same idiom
# ``scripts/scan_secrets.py`` uses on itself (see ``_H`` there). Storing the
# five-hyphen run verbatim makes this fixture indistinguishable from a real
# leaked key to the secret scanner, and "it's only a test" is exactly what a
# real leak would also claim. No key material here: the body is a placeholder.
_H = "-" * 5
_PEM_BEGIN = f"{_H}BEGIN OPENSSH PRIVATE KEY{_H}"
_PEM_END = f"{_H}END OPENSSH PRIVATE KEY{_H}"


_KEY_ENV = "CLOUD_ENCRYPTION_KEY"
_TOKEN_ENV = "POCKETPAW_HCLOUD_TOKEN"
_PRIV = _PEM_BEGIN + "\nFAKE\n" + _PEM_END + "\n"
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
