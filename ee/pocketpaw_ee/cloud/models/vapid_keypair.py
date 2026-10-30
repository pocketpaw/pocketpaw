# VapidKeypair Beanie document — per-workspace VAPID (Voluntary Application
# Server Identification) keypair for Web Push.
# Created: 2026-06-09 (feat/push-subscription-store, pocketpaw#1391) — a
# browser subscribing to Web Push needs the application server's VAPID public
# key (the ``applicationServerKey``). Each workspace (tenant) gets its own
# P-256 keypair, generated once and reused on every read.
#
# Storage discipline:
#   - ``public_key`` is the base64url-encoded uncompressed P-256 point the
#     browser consumes. Public by design — served by the key endpoint.
#   - ``private_pem_encrypted`` is the PKCS#8 PEM private key encrypted at
#     rest with the deployment-wide Fernet key (``_core.crypto``). It NEVER
#     leaves the backend and never appears in any wire response. Only the
#     send path (#1392) decrypts it to sign the VAPID JWT.
#
# One row per workspace; ``workspace`` is unique-indexed so the
# generate-once / read-many path is O(1) and the service can upsert
# idempotently. Only ``ee.cloud.push.service`` may import this module —
# enforced by the import-linter contract in ``ee/pyproject.toml``.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class VapidKeypair(TimestampedDocument):
    """Per-workspace VAPID keypair. Private key stored Fernet-encrypted."""

    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    public_key: str  # base64url uncompressed P-256 point — safe to serve
    private_pem_encrypted: str  # Fernet ciphertext of the PKCS#8 PEM private key

    class Settings:
        name = "vapid_keypairs"
