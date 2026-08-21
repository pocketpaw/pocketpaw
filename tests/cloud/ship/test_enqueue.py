# tests/cloud/ship/test_enqueue.py — the web-process enqueue seam.
#
# Covers: enqueue_provision mints a keypair, inserts a ``provisioning`` box with
# the private key encrypted at rest, and enqueues ``provision_box_job``
# POSITIONALLY (the arq contract — a ``queue=`` kwarg would be forwarded to the
# job function and crash it, the pitfall jobs/service.py documents).

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud.ship import enqueue

_KEY_ENV = "CLOUD_ENCRYPTION_KEY"


@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, Fernet.generate_key().decode())


class FakePool:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, *args, **kwargs):
        self.enqueued.append((args, kwargs))
        return type("Job", (), {"job_id": "arq-1"})()


async def test_enqueue_creates_box_and_dispatches(mongo_db, enc_key):  # noqa: ARG001
    pool = FakePool()

    async def pool_factory():
        return pool

    box = await enqueue.enqueue_provision(
        workspace_id="ws-1",
        server_type="cx22",
        region="fsn1",
        pool_factory=pool_factory,
    )

    assert box.status == "provisioning"
    assert box.workspace == "ws-1"
    assert box.server_type == "cx22"
    assert box.region == "fsn1"
    # A real keypair was minted and the private half encrypted.
    assert box.ssh_public_key.startswith("ssh-ed25519 ")
    assert "PRIVATE KEY" not in box.ssh_private_key_enc

    # The job was enqueued positionally with (job_name, box_id, workspace_id)
    # and NO kwargs — arq forwards stray kwargs to the job function.
    (args, kwargs) = pool.enqueued[0]
    assert args == ("provision_box_job", str(box.id), "ws-1")
    assert kwargs == {}


async def test_enqueue_failure_propagates(mongo_db, enc_key):  # noqa: ARG001
    class BadPool:
        async def enqueue_job(self, *a, **k):
            raise RuntimeError("redis down")

    async def pool_factory():
        return BadPool()

    with pytest.raises(RuntimeError, match="redis down"):
        await enqueue.enqueue_provision(
            workspace_id="ws-1",
            server_type="cx22",
            region="fsn1",
            pool_factory=pool_factory,
        )
