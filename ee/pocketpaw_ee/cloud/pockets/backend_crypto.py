# backend_crypto.py — Encrypt / decrypt pocket-backend auth tokens at rest.
# Created: 2026-05-21 (RFC 04 alpha) — pure crypto helper for the per-pocket
#   backend credential store. No Beanie imports; the Beanie document
#   (`models/pocket_backend.py`) just carries the ciphertext bytes this
#   module produces.
#
#   Scheme: a per-document random 16-byte salt feeds HKDF-SHA256 (length 32)
#   over `AUTH_SECRET`, producing an AES-256 key used with AES-GCM. Each
#   encryption also draws a fresh 12-byte nonce. Storing the salt per row
#   means two pockets bound to the same backend with the same token still
#   get different ciphertext.
#
#   SECURITY: never log derived keys or plaintext tokens.

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Info label binds the derived key to this purpose + version. Bumping the
# suffix (v2, …) would invalidate every existing ciphertext on purpose.
_HKDF_INFO = b"pocket-backend-credentials-v1"
_KEY_LENGTH = 32  # AES-256
_SALT_LENGTH = 16
_NONCE_LENGTH = 12


def _auth_secret() -> bytes:
    """Return the configured ``AUTH_SECRET`` as bytes.

    Raises a clear error when it is unset — credential encryption must
    never silently fall back to an empty / predictable key.
    """
    secret = os.environ.get("AUTH_SECRET")
    if not secret:
        raise RuntimeError(
            "AUTH_SECRET is not set — pocket backend credential encryption "
            "requires it. Set AUTH_SECRET in the environment before "
            "configuring a pocket backend."
        )
    return secret.encode()


def _derive_key(salt: bytes) -> bytes:
    """Derive a 32-byte AES key from ``AUTH_SECRET`` + ``salt`` via HKDF-SHA256."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH,
        salt=salt,
        info=_HKDF_INFO,
    )
    return hkdf.derive(_auth_secret())


def encrypt_token(token: str) -> tuple[bytes, bytes, bytes]:
    """Encrypt ``token`` and return ``(ciphertext, nonce, salt)``.

    Every call draws a fresh random salt and nonce, so encrypting the same
    token twice yields different ciphertext.
    """
    salt = os.urandom(_SALT_LENGTH)
    nonce = os.urandom(_NONCE_LENGTH)
    key = _derive_key(salt)
    ciphertext = AESGCM(key).encrypt(nonce, token.encode(), None)
    return ciphertext, nonce, salt


def decrypt_token(ciphertext: bytes, nonce: bytes, salt: bytes) -> str:
    """Decrypt a token produced by :func:`encrypt_token`.

    Raises ``cryptography.exceptions.InvalidTag`` if the ciphertext, nonce,
    salt, or ``AUTH_SECRET`` does not match what produced the ciphertext.
    """
    key = _derive_key(salt)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    return plaintext.decode()


__all__ = ["encrypt_token", "decrypt_token"]
