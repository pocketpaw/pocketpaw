# tests/cloud/ship/test_keygen.py — the per-box ed25519 keypair mint.
#
# Covers: a valid OpenSSH keypair is produced, the public key is a single line
# with the comment, distinct boxes get distinct keys, and the private half is an
# unencrypted OpenSSH key (the Fernet envelope is the store's job, not the
# keygen's).

from __future__ import annotations

from pocketpaw_ee.ship_engine.keygen import generate_box_keypair


def test_generates_valid_openssh_keypair():
    kp = generate_box_keypair(comment="paw-ship-abc")
    assert kp.private_key_openssh.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert kp.public_key_openssh.startswith("ssh-ed25519 ")
    assert kp.public_key_openssh.endswith(" paw-ship-abc")
    # The public key is a single line (authorized_keys entry).
    assert "\n" not in kp.public_key_openssh


def test_distinct_boxes_get_distinct_keys():
    a = generate_box_keypair()
    b = generate_box_keypair()
    assert a.private_key_openssh != b.private_key_openssh
    assert a.public_key_openssh != b.public_key_openssh


def test_comment_newlines_are_flattened():
    kp = generate_box_keypair(comment="line1\nline2")
    assert "\n" not in kp.public_key_openssh
