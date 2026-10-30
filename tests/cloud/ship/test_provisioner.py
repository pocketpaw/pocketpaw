# tests/cloud/ship/test_provisioner.py — HcloudProvisioner over a FAKE client.
#
# Covers SHIP-2 acceptance: create_server returns a targetable BoxHandle + the
# captured monthly price, maps ANY provider failure to a safe ProvisionError
# string (never a raw payload), and passes cloud-init that installs Dokku. Zero
# network — the hcloud client is a Protocol fake.

from __future__ import annotations

import pytest
from pocketpaw_ee.ship_engine.hcloud import HcloudProvisioner, ProvisionError
from pocketpaw_ee.ship_engine.port import BoxSpec


class FakeHcloudClient:
    """In-memory stand-in for the hcloud SDK slice the provisioner uses."""

    def __init__(self, *, price=7.5, fail_on=None, created=None):
        self.price = price
        self.fail_on = fail_on  # a method name to blow up on
        self.created = created if created is not None else []
        self.user_data_seen = None

    def _maybe_fail(self, name):
        if self.fail_on == name:
            raise RuntimeError(f"boom-{name} secret-token-abc123")

    def ensure_ssh_key(self, *, name, public_key):
        self._maybe_fail("ensure_ssh_key")
        return {"name": name}

    def ensure_firewall(self, *, name, ports):
        self._maybe_fail("ensure_firewall")
        return {"name": name}

    def price_monthly(self, server_type):
        self._maybe_fail("price_monthly")
        return self.price

    def create_server(self, *, name, server_type, image, location, ssh_key, firewall, user_data):
        self._maybe_fail("create_server")
        self.user_data_seen = user_data
        self.created.append(name)
        return ("srv-123", "203.0.113.7")


_SPEC = BoxSpec(name="paw-ship-box", region="fsn1", size="cx22")


def test_create_server_returns_handle_and_price():
    client = FakeHcloudClient(price=11.9)
    prov = HcloudProvisioner(client)
    result = prov.create_server(_SPEC, ssh_public_key="ssh-ed25519 AAAA x", key_name="k1")

    assert result.server_id == "srv-123"
    assert result.ip == "203.0.113.7"
    assert result.price_monthly == 11.9
    assert result.handle.box_id == "srv-123"
    assert result.handle.host == "203.0.113.7"
    assert result.handle.ssh_user == "root"
    assert result.server_type == "cx22"
    assert result.region == "fsn1"


def test_create_server_passes_dokku_cloudinit():
    client = FakeHcloudClient()
    prov = HcloudProvisioner(client)
    prov.create_server(_SPEC, ssh_public_key="ssh-ed25519 AAAA x", key_name="k1")
    assert client.user_data_seen is not None
    assert "DOKKU_TAG=v" in client.user_data_seen


@pytest.mark.parametrize("fail_on", ["ensure_ssh_key", "ensure_firewall", "create_server"])
def test_provider_failure_maps_to_safe_error(fail_on):
    client = FakeHcloudClient(fail_on=fail_on)
    prov = HcloudProvisioner(client)
    with pytest.raises(ProvisionError) as exc:
        prov.create_server(_SPEC, ssh_public_key="ssh-ed25519 AAAA x", key_name="k1")
    # The safe string carries only the exception TYPE, never the raw payload
    # (which here embedded a fake secret token).
    assert "secret-token-abc123" not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


def test_empty_server_id_is_rejected():
    class NoIdClient(FakeHcloudClient):
        def create_server(self, **kw):
            return ("", "")

    prov = HcloudProvisioner(NoIdClient())
    with pytest.raises(ProvisionError, match="no server id"):
        prov.create_server(_SPEC, ssh_public_key="ssh-ed25519 AAAA x", key_name="k1")
