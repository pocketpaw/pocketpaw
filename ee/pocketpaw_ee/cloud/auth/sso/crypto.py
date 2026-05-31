"""Fernet wrapper for client-secret encryption at rest.

Production deployments set ``POCKETPAW_SSO_ENCRYPTION_KEY`` to a raw
base64 Fernet key. When unset we derive a key from ``AUTH_SECRET`` via
PBKDF2HMAC + a fixed salt; that path logs a one-time warning so the
operator gets nudged toward a dedicated key without crashing the
deployment.
"""

from __future__ import annotations

import base64
import logging
import os
import threading

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_FIXED_SALT = b"pocketpaw-sso-v1"
_KDF_ITERATIONS = 200_000
_warned = False
_lock = threading.Lock()
_fernet: Fernet | None = None


def _derive_from_auth_secret() -> bytes:
    auth_secret = os.environ.get("AUTH_SECRET", "change-me-in-production-please")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_FIXED_SALT,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(auth_secret.encode("utf-8")))


def _get_fernet() -> Fernet:
    global _fernet, _warned
    with _lock:
        if _fernet is not None:
            return _fernet
        explicit = os.environ.get("POCKETPAW_SSO_ENCRYPTION_KEY", "").strip()
        if explicit:
            key = explicit.encode("utf-8") if isinstance(explicit, str) else explicit
        else:
            key = _derive_from_auth_secret()
            if not _warned:
                logger.warning(
                    "Using derived SSO encryption key; set POCKETPAW_SSO_ENCRYPTION_KEY in prod"
                )
                _warned = True
        _fernet = Fernet(key)
        return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def _reset_for_tests() -> None:
    global _fernet, _warned
    with _lock:
        _fernet = None
        _warned = False
