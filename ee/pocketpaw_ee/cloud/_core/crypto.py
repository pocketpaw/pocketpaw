# Cloud — at-rest encryption for secret values stored in Mongo.
#
# A deployment-wide Fernet key (AES-128-CBC + HMAC-SHA256) encrypts
# third-party secrets before they're written to Mongo — meeting-provider
# credentials today, and other connector / integration tokens as they
# land. This is cross-cutting cloud infrastructure, hence its home in
# _core alongside errors / http / deps.
#
# Why an explicit env key (and NOT pocketpaw's file-based CredentialStore):
# CredentialStore derives its key from machine identity, so its ciphertext
# only decrypts on the same host. Ciphertext in Mongo must survive
# container rebuilds and host migration — that demands a stable,
# operator-provided key.
#
# Generate one once:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# then set CLOUD_ENCRYPTION_KEY in the deployment environment.

from __future__ import annotations

import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from pocketpaw_ee.cloud._core.errors import ValidationError

_ENV_KEY = "CLOUD_ENCRYPTION_KEY"


def is_configured() -> bool:
    """True when an encryption key is set — i.e. encrypted storage is usable.

    When False, callers that persist secrets should surface a clear setup
    error; callers with an env-var fallback (e.g. meetings) can degrade to
    that instead.
    """
    return bool(os.environ.get(_ENV_KEY, "").strip())


def _fernet() -> Fernet:
    raw = os.environ.get(_ENV_KEY, "").strip()
    if not raw:
        raise ValidationError(
            "cloud.encryption_key_missing",
            "Encrypted storage is disabled — set CLOUD_ENCRYPTION_KEY in the "
            'environment. Generate one with: python -c "from cryptography.fernet '
            'import Fernet; print(Fernet.generate_key().decode())"',
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            "cloud.encryption_key_invalid",
            "CLOUD_ENCRYPTION_KEY is not a valid Fernet key (expected a "
            "44-character urlsafe-base64 string).",
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a string into a Fernet token string."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token back into a string. An empty token yields ''."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValidationError(
            "cloud.value_undecryptable",
            "A stored encrypted value could not be decrypted — the encryption "
            "key changed. Re-enter the affected secrets.",
        ) from exc


def encrypt_json(value: Any) -> str:
    """Encrypt a JSON-serializable value (dict, list, …) into a Fernet token."""
    return encrypt(json.dumps(value))


def decrypt_json(token: str) -> Any:
    """Decrypt a token written by ``encrypt_json``. An empty token yields ``{}``."""
    if not token:
        return {}
    return json.loads(decrypt(token))
