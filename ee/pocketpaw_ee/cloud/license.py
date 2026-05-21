"""Enterprise license validation for cloud features.

License keys are validated on startup and checked per-request via a FastAPI
dependency. Keys are signed with Ed25519 — the public key is embedded here,
the private key lives only on the license server.

Key format: base64(payload_json + "." + signature_hex)
Payload: {"org": "acme-inc", "plan": "team", "seats": 10, "exp": "2027-01-01"}
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# License payload
# ---------------------------------------------------------------------------


class LicensePayload(BaseModel):
    org: str
    plan: str = "team"  # team | business | enterprise
    seats: int = 5
    exp: str  # ISO date "2027-01-01"
    features: list[str] = Field(default_factory=list)  # optional feature flags

    @property
    def expired(self) -> bool:
        try:
            return datetime.now(UTC) > datetime.fromisoformat(self.exp).replace(tzinfo=UTC)
        except Exception:
            return True

    def has_feature(self, feature: str) -> bool:
        return feature in self.features or self.plan == "enterprise"


# ---------------------------------------------------------------------------
# Key validation
# ---------------------------------------------------------------------------

# Ed25519 public key for license verification (hex-encoded).
# Replace with your actual public key.
_PUBLIC_KEY_HEX = os.environ.get("POCKETPAW_LICENSE_PUBLIC_KEY", "")

_cached_license: LicensePayload | None = None
_license_error: str | None = None


def _verify_signature(payload_bytes: bytes, signature_hex: str) -> bool:
    """Verify Ed25519 signature. Returns False if key is missing or invalid."""
    if not _PUBLIC_KEY_HEX:
        # No public key configured — accept key based on HMAC-SHA256 with a
        # shared secret (simpler setup for self-hosted deployments).
        secret = os.environ.get("POCKETPAW_LICENSE_SECRET", "")
        if not secret:
            return False
        expected = hashlib.sha256(f"{secret}:{payload_bytes.decode()}".encode()).hexdigest()
        return expected == signature_hex

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(_PUBLIC_KEY_HEX))
        pub_key.verify(bytes.fromhex(signature_hex), payload_bytes)
        return True
    except Exception:
        return False


def validate_license_key(key: str) -> LicensePayload:
    """Parse and validate a license key string. Raises ValueError on failure."""
    try:
        decoded = base64.b64decode(key).decode()
    except Exception as exc:
        raise ValueError(f"Invalid license key encoding: {exc}") from exc

    if "." not in decoded:
        raise ValueError("Invalid license key format")

    payload_str, sig = decoded.rsplit(".", 1)

    if not _verify_signature(payload_str.encode(), sig):
        raise ValueError("Invalid license key signature")

    try:
        data = json.loads(payload_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid license key payload: {exc}") from exc

    payload = LicensePayload(**data)
    if payload.expired:
        raise ValueError(f"License expired on {payload.exp}")

    return payload


def load_license() -> LicensePayload | None:
    """Load license from env var POCKETPAW_LICENSE_KEY. Returns None if absent/invalid."""
    global _cached_license, _license_error

    if _cached_license is not None:
        return _cached_license

    # Ensure .env is loaded
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    key = os.environ.get("POCKETPAW_LICENSE_KEY", "").strip()
    if not key:
        _license_error = "No license key configured (set POCKETPAW_LICENSE_KEY)"
        # Don't log on every check — only first time
        return None

    try:
        _cached_license = validate_license_key(key)
        logger.info(
            "Enterprise license loaded: org=%s plan=%s seats=%d exp=%s",
            _cached_license.org,
            _cached_license.plan,
            _cached_license.seats,
            _cached_license.exp,
        )
        return _cached_license
    except ValueError as exc:
        _license_error = str(exc)
        logger.warning("Enterprise license invalid: %s", exc)
        return None


def get_license() -> LicensePayload | None:
    """Return cached license or None."""
    if _cached_license is not None:
        return _cached_license
    return load_license()


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def require_license() -> LicensePayload:
    """Dependency that gates enterprise endpoints behind a valid license."""
    lic = get_license()
    if lic is None:
        raise HTTPException(
            status_code=403,
            detail=_license_error or "Enterprise license required. Set POCKETPAW_LICENSE_KEY.",
        )
    if lic.expired:
        raise HTTPException(status_code=403, detail=f"Enterprise license expired on {lic.exp}")
    return lic


def require_feature(feature: str):
    """Dependency factory that checks for a specific licensed feature."""

    async def _check(license: LicensePayload = Depends(require_license)) -> LicensePayload:
        if not license.has_feature(feature):
            raise HTTPException(
                status_code=403,
                detail=f"Feature '{feature}' not included in your {license.plan} plan.",
            )
        return license

    return _check


# ---------------------------------------------------------------------------
# License info endpoint (added to router externally)
# ---------------------------------------------------------------------------


class LicenseInfo(BaseModel):
    valid: bool
    org: str | None = None
    plan: str | None = None
    seats: int | None = None
    exp: str | None = None
    error: str | None = None


def get_license_info() -> LicenseInfo:
    """Return license status for the settings UI."""
    lic = get_license()
    if lic:
        return LicenseInfo(
            valid=not lic.expired,
            org=lic.org,
            plan=lic.plan,
            seats=lic.seats,
            exp=lic.exp,
            error="License expired" if lic.expired else None,
        )
    return LicenseInfo(valid=False, error=_license_error)
