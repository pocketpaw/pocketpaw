# pocket_backend.py — Beanie document for per-pocket backend credentials.
# Created: 2026-05-21 (RFC 04 alpha) — A Pocket can be bound to ONE external
#   backend (base URL + auth credential). The credential is stored here, in a
#   SEPARATE collection (`pocket_backend_credentials`) — never inside the
#   `Pocket` document and never inside `rippleSpec`. Keeping it out of the
#   pocket keeps the spec shareable and secret-free.
#
#   The auth token is encrypted at rest: `encrypted_token` / `nonce` / `salt`
#   are produced by `pockets/backend_crypto.py` (AES-GCM + HKDF-SHA256).
#   The plaintext token never touches this document.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class PocketBackendCredential(TimestampedDocument):
    """Per-pocket backend binding: base URL + (encrypted) auth credential.

    One row per pocket. The run-sources executor decrypts the token at call
    time; every other read path returns only `base_url` / `auth_type` /
    `configured` so the secret never leaves this collection.
    """

    pocket_id: Indexed(str)  # type: ignore[valid-type]
    workspace_id: Indexed(str)  # type: ignore[valid-type]
    base_url: str
    # bearer | api_key | basic | none
    auth_type: str = "none"
    # Custom header name for the api_key auth type. Defaults to "X-Api-Key".
    auth_header: str | None = None
    # Encrypted token material. All three are None when auth_type == "none".
    encrypted_token: bytes | None = None
    nonce: bytes | None = None
    salt: bytes | None = None

    class Settings:
        name = "pocket_backend_credentials"
