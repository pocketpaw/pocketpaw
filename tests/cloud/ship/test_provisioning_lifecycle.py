# tests/cloud/ship/test_provisioning_lifecycle.py — the ShipBox lifecycle.
#
# The core SHIP-2 coverage: ``run_provision`` walks a real ShipBox doc (Beanie on
# mongomock) from ``provisioning`` to ``ready``, records ``degraded`` on a
# provider failure AND on readiness exhaustion, and NEVER creates a second
# server when a prior attempt already recorded one (idempotency). Also asserts
# the at-rest secret contract: the private key is stored encrypted, decrypts
# back through the store seam, and never appears in the doc's plaintext fields.
#
# Zero network, zero real box: the provisioner and the readiness probe are both
# injected fakes, and ``sleep`` is a no-op so the poll loop runs instantly.

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud.ship import provisioning, store
from pocketpaw_ee.ship_engine.hcloud import ProvisionError
from pocketpaw_ee.ship_engine.port import BoxHandle


# The PEM header is ASSEMBLED, never written as a literal — the same idiom
# ``scripts/scan_secrets.py`` uses on itself (see ``_H`` there). Storing the
# five-hyphen run verbatim makes this fixture indistinguishable from a real
# leaked key to the secret scanner, and "it's only a test" is exactly what a
# real leak would also claim. No key material here: the body is a placeholder.
_H = "-" * 5
_PEM_BEGIN = f"{_H}BEGIN OPENSSH PRIVATE KEY{_H}"
_PEM_END = f"{_H}END OPENSSH PRIVATE KEY{_H}"


_KEY_ENV = "CLOUD_ENCRYPTION_KEY"
_PRIV = _PEM_BEGIN + "\nFAKEKEYBODY\n" + _PEM_END + "\n"
_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY paw-ship"


@pytest.fixture
def enc_key(monkeypatch):
    """A valid Fernet key so the store's at-rest encryption works."""
    monkeypatch.setenv(_KEY_ENV, Fernet.generate_key().decode())


class FakeProvisioner:
    """Stands in for HcloudProvisioner: records calls, returns scripted facts."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def create_server(self, spec, *, ssh_public_key, key_name):
        self.calls += 1
        if self.fail:
            raise ProvisionError("provider create failed (RuntimeError)")
        from pocketpaw_ee.ship_engine.hcloud import ProvisionResult

        return ProvisionResult(
            handle=BoxHandle(box_id="srv-9", host="203.0.113.9"),
            server_id="srv-9",
            ip="203.0.113.9",
            price_monthly=8.25,
            server_type=spec.size,
            region=spec.region,
        )


def probe_script(*results):
    """A readiness probe that returns each scripted result in turn."""
    seq = list(results)

    async def _probe(handle: BoxHandle, key: str) -> bool:
        return seq.pop(0) if seq else False

    return _probe


async def _noop_sleep(_seconds: float) -> None:
    return None


async def _make_box(workspace="ws-1"):
    return await store.create_provisioning_box(
        workspace_id=workspace,
        provider="hcloud",
        server_type="cx22",
        region="fsn1",
        ssh_private_key=_PRIV,
        ssh_public_key=_PUB,
    )


async def test_provisioning_to_ready(mongo_db, enc_key):  # noqa: ARG001 — fixtures init state
    box = await _make_box()
    assert box.status == "provisioning"

    prov = FakeProvisioner()
    updated = await provisioning.run_provision(
        box,
        provisioner=prov,
        ssh_public_key=_PUB,
        ssh_private_key=_PRIV,
        probe=probe_script(False, True),  # still booting, then ready
        sleep=_noop_sleep,
    )

    assert updated.status == "ready"
    assert updated.server_id == "srv-9"
    assert updated.ip == "203.0.113.9"
    assert updated.price_monthly == 8.25
    assert updated.status_reason is None
    assert prov.calls == 1


async def test_provider_failure_marks_degraded(mongo_db, enc_key):  # noqa: ARG001
    box = await _make_box()
    updated = await provisioning.run_provision(
        box,
        provisioner=FakeProvisioner(fail=True),
        ssh_public_key=_PUB,
        ssh_private_key=_PRIV,
        probe=probe_script(True),
        sleep=_noop_sleep,
    )

    assert updated.status == "degraded"
    assert "provider create failed" in (updated.status_reason or "")
    # No server was recorded, so a retry starts clean.
    assert updated.server_id == ""


async def test_readiness_exhaustion_marks_degraded(mongo_db, enc_key):  # noqa: ARG001
    box = await _make_box()
    updated = await provisioning.run_provision(
        box,
        provisioner=FakeProvisioner(),
        ssh_public_key=_PUB,
        ssh_private_key=_PRIV,
        probe=probe_script(False, False, False),  # never ready
        sleep=_noop_sleep,
        max_probe_attempts=3,
    )

    assert updated.status == "degraded"
    assert "did not become reachable" in (updated.status_reason or "")
    # The server id IS recorded — a retry must reuse it, not orphan a second box.
    assert updated.server_id == "srv-9"


async def test_probe_exception_is_tolerated_then_ready(mongo_db, enc_key):  # noqa: ARG001
    """A connection error while the box boots is expected, not fatal."""
    box = await _make_box()
    calls = {"n": 0}

    async def flaky_probe(handle, key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionRefusedError("still booting")
        return True

    updated = await provisioning.run_provision(
        box,
        provisioner=FakeProvisioner(),
        ssh_public_key=_PUB,
        ssh_private_key=_PRIV,
        probe=flaky_probe,
        sleep=_noop_sleep,
    )
    assert updated.status == "ready"


async def test_retry_never_creates_a_second_server(mongo_db, enc_key):  # noqa: ARG001
    """The idempotency guarantee: a box that already carries a server_id skips
    the create entirely on a re-run (the exact double-spend defect)."""
    box = await _make_box()

    # First attempt: create lands, readiness never arrives → degraded w/ server_id.
    first = await provisioning.run_provision(
        box,
        provisioner=FakeProvisioner(),
        ssh_public_key=_PUB,
        ssh_private_key=_PRIV,
        probe=probe_script(False),
        sleep=_noop_sleep,
        max_probe_attempts=1,
    )
    assert first.server_id == "srv-9"

    # Second attempt on the SAME box: the provisioner must not be called again.
    prov2 = FakeProvisioner()
    second = await provisioning.run_provision(
        first,
        provisioner=prov2,
        ssh_public_key=_PUB,
        ssh_private_key=_PRIV,
        probe=probe_script(True),
        sleep=_noop_sleep,
    )

    assert prov2.calls == 0, "a retry must reuse the existing server, never create a second"
    assert second.status == "ready"
    assert second.server_id == "srv-9"


async def test_ssh_key_is_encrypted_at_rest(mongo_db, enc_key):  # noqa: ARG001
    box = await _make_box()

    # The stored field is ciphertext — the private key body never appears.
    assert box.ssh_private_key_enc
    assert "FAKEKEYBODY" not in box.ssh_private_key_enc
    assert "BEGIN OPENSSH PRIVATE KEY" not in box.ssh_private_key_enc
    # ...and the whole serialized doc carries no plaintext key material.
    assert "FAKEKEYBODY" not in box.model_dump_json()
    # The store seam round-trips it back for driver use.
    assert store.decrypt_ssh_key(box) == _PRIV
    # The PUBLIC key is stored as-is (not secret).
    assert box.ssh_public_key == _PUB


async def test_get_box_is_workspace_scoped(mongo_db, enc_key):  # noqa: ARG001
    box = await _make_box(workspace="ws-owner")
    assert await store.get_box("ws-owner", str(box.id)) is not None
    # A different tenant may not read it, even with the right id.
    assert await store.get_box("ws-attacker", str(box.id)) is None
