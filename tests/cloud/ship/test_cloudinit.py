# tests/cloud/ship/test_cloudinit.py — the cloud-init user-data template renders
# a Dokku-ready box and never leaks private-key material.
#
# Covers SHIP-2 acceptance: pinned Dokku version, Docker install, nixpacks, and
# the PUBLIC key all present; a malformed/multi-line public key is rejected; no
# private-key material anywhere in the rendered output.

from __future__ import annotations

import pytest
from pocketpaw_ee.ship_engine.cloudinit import (
    DOKKU_VERSION,
    NIXPACKS_VERSION,
    render_user_data,
)

_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY paw-ship"


def test_renders_required_blocks():
    out = render_user_data(ssh_public_key=_PUBKEY)
    assert out.startswith("#cloud-config")
    # Docker install
    assert "get.docker.com" in out
    # Pinned Dokku bootstrap — the version is the pin, not "latest".
    assert f"install/v{DOKKU_VERSION}/bootstrap.sh" in out
    assert f"DOKKU_TAG=v{DOKKU_VERSION}" in out
    # nixpacks, pinned
    assert "nixpacks.com/install.sh" in out
    assert f"VERSION={NIXPACKS_VERSION}" in out
    # The public key authorized for root AND the dokku user.
    assert _PUBKEY in out
    assert "dokku ssh-keys:add admin" in out


def test_rejects_multiline_public_key():
    with pytest.raises(ValueError, match="single-line OpenSSH public key"):
        render_user_data(ssh_public_key="ssh-ed25519 AAAA...\nssh-ed25519 BBBB...")


def test_rejects_non_openssh_public_key():
    with pytest.raises(ValueError, match="single-line OpenSSH public key"):
        render_user_data(ssh_public_key="-----BEGIN OPENSSH PRIVATE KEY-----")


def test_no_private_key_material_in_output():
    # A PRIVATE key body must never appear — the template only ever gets the
    # public half, but assert the guard so a future edit that passes the wrong
    # value is caught.
    out = render_user_data(ssh_public_key=_PUBKEY)
    assert "PRIVATE KEY" not in out
    assert "BEGIN OPENSSH" not in out
