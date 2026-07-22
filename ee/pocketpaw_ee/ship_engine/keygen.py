# ee/pocketpaw_ee/ship_engine/keygen.py — mint the per-box SSH keypair.
#
# Each provisioned box gets its OWN ed25519 keypair (blast radius = one box).
# ``generate_box_keypair`` returns the OpenSSH private key (PEM, unencrypted —
# it is Fernet-encrypted at rest by the caller before it touches Mongo) and the
# matching single-line OpenSSH public key (authorized on the box via cloud-init;
# not secret).
#
# ed25519 over RSA: smaller, modern, and asyncssh + Dokku both accept it. No
# passphrase — the at-rest protection is the Fernet envelope on the ShipBox
# doc, not a key passphrase we would then also have to store.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


@dataclass(frozen=True)
class BoxKeypair:
    """A freshly minted box keypair.

    ``private_key_openssh`` is an unencrypted OpenSSH private key (the caller
    Fernet-encrypts it before persistence). ``public_key_openssh`` is the
    single-line authorized-keys entry.
    """

    private_key_openssh: str
    public_key_openssh: str


def generate_box_keypair(*, comment: str = "paw-ship") -> BoxKeypair:
    """Generate a new ed25519 keypair for a box.

    ``comment`` is appended to the public key line (operator legibility in the
    box's authorized_keys). Never logs or persists the private half — that is
    the caller's Fernet-encrypt-then-store responsibility.
    """
    private_key = ed25519.Ed25519PrivateKey.generate()

    private_openssh = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_openssh = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )

    safe_comment = comment.replace("\n", " ").strip()
    return BoxKeypair(
        private_key_openssh=private_openssh,
        public_key_openssh=f"{public_openssh} {safe_comment}",
    )
