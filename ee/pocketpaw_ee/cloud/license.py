"""Enterprise license validation for cloud features.

License keys are validated on startup and checked per-request via a FastAPI
dependency. Keys are signed with Ed25519 — the public key is embedded here,
the private key lives only on the license server.

Key format: base64(payload_json + "." + signature_hex)
Payload: {"org": "acme-inc", "plan": "team", "seats": 10, "exp": "2027-01-01"}

Changes:
  - 2026-06-10 (sov/w1a-deploy): Added a production-posture guard so a
    tenant can't silently run on the BYPASSABLE committed DEV public key.
    ``_using_dev_public_key()`` reports whether the active verifier is the
    baked-in DEV key (i.e. no operator ``POCKETPAW_LICENSE_PUBLIC_KEY`` is
    set). ``enforce_license_key_posture()`` (mirrors W0e's AUTH_SECRET gate
    in ``ee/cloud/auth/core.py`` and reuses its ``_is_production()`` helper
    when importable, with a local fallback) RAISES under production posture
    if the DEV key is in use, and is invoked from ``load_license()`` /
    ``get_license()`` so the EE gate refuses to validate a license against a
    public key anyone in the repo can forge against. Dev/test posture is
    unchanged (warns only).
  - 2026-06-10 (sov/w0a-license): Replaced the "Replace with your actual
    public key" placeholder with a real, baked-in DEV Ed25519 public key
    (``_DEV_PUBLIC_KEY_HEX``). Verification now resolves the public key in
    this order: ``POCKETPAW_LICENSE_PUBLIC_KEY`` env (production / operator
    key) → the baked-in DEV key. The HMAC-SHA256 fallback only fires when
    *no* Ed25519 public key resolves AND ``POCKETPAW_LICENSE_SECRET`` is
    set, so a default install now verifies Ed25519-minted keys out of the
    box. The matching private key for the DEV public key lives in
    ``ee/pocketpaw_ee/cloud/_dev_license_key.py`` (dev-only, clearly
    marked); production minting supplies its own operator private key. See
    ``ee/pocketpaw_ee/cloud/mint.py`` for the minting path.
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
#
# DEV key: the matching private seed ships (clearly marked dev-only) in
# ``ee/pocketpaw_ee/cloud/_dev_license_key.py`` so a fresh checkout can mint
# and verify licenses with zero setup. PRODUCTION operators MUST override
# this with their own key by exporting ``POCKETPAW_LICENSE_PUBLIC_KEY`` (the
# matching private key is supplied to ``mint`` out-of-band and never enters
# the repo).
_DEV_PUBLIC_KEY_HEX = "42a718ecd6f1fc3d1b6709ccfc1318d1ca80fe7ce238c737308e761d7263d5c6"


def _resolve_public_key_hex() -> str:
    """Resolve the active Ed25519 verification public key (hex).

    Operator-supplied env var wins over the baked-in DEV key so production
    deployments verify against their own keypair. Read at call time (not
    import time) so tests can patch the env between cases.
    """
    return os.environ.get("POCKETPAW_LICENSE_PUBLIC_KEY", "").strip() or _DEV_PUBLIC_KEY_HEX


def _using_dev_public_key() -> bool:
    """True when license verification is still using the committed DEV key.

    The DEV key is a license BYPASS: its matching private seed is committed
    in the open (``_dev_license_key.DEV_PRIVATE_KEY_HEX``), so anyone can
    mint a key the DEV public key accepts. An operator escapes the bypass by
    exporting their own ``POCKETPAW_LICENSE_PUBLIC_KEY``; until they do, the
    resolver falls back to ``_DEV_PUBLIC_KEY_HEX`` and we report True here.
    """
    return _resolve_public_key_hex() == _DEV_PUBLIC_KEY_HEX


def _is_production() -> bool:
    """Production-posture detector, reusing W0e's auth gate when importable.

    Mirrors the AUTH_SECRET fail-fast in ``ee/cloud/auth/core.py`` so the two
    "don't boot insecurely" gates agree on what "production" means. We import
    that helper lazily (top-level import of ``auth.core`` runs its own
    ``SECRET = _resolve_secret()`` at module load, which is itself a prod
    gate — importing it eagerly here would couple license loading to
    AUTH_SECRET being set first). If the auth layer isn't importable we fall
    back to the identical two-signal check: POCKETPAW_ENV in {production,
    prod} OR POCKETPAW_AUTH_COOKIE_SECURE=true.
    """
    try:
        from pocketpaw_ee.cloud.auth.core import _is_production as _auth_is_production

        return _auth_is_production()
    except Exception:
        env = os.environ.get("POCKETPAW_ENV", "").strip().lower()
        if env in {"production", "prod"}:
            return True
        return os.environ.get("POCKETPAW_AUTH_COOKIE_SECURE", "false").strip().lower() == "true"


def enforce_license_key_posture() -> None:
    """Refuse to run on the bypassable DEV public key in production.

    A production tenant that verifies licenses against the committed DEV key
    is not actually licensed — anyone can forge a key for it. We treat that
    exactly like W0e treats the placeholder AUTH_SECRET: fail fast with an
    actionable error instead of silently running ownable.

    Called from ``load_license()`` / ``get_license()`` (the boot + per-load
    paths). Dev/test posture is a no-op so the zero-config DEV loop still
    works. Raises ``RuntimeError`` under production posture.
    """
    if _is_production() and _using_dev_public_key():
        raise RuntimeError(
            "POCKETPAW_LICENSE_PUBLIC_KEY is unset, so license verification is "
            "using the committed DEV public key. That key is a BYPASS: its "
            "private seed ships in the open (ee/cloud/_dev_license_key.py), so "
            "anyone can mint a license this tenant accepts. Refusing to run in "
            "production on a forgeable license key. Generate your own keypair "
            "and export the public key, e.g.\n"
            "  python -m pocketpaw_ee.cloud.mint generate-keypair\n"
            '  export POCKETPAW_LICENSE_PUBLIC_KEY="<public-hex>"\n'
            "then mint the tenant license with the matching private key "
            "(--private-key-file or POCKETPAW_LICENSE_PRIVATE_KEY)."
        )


_cached_license: LicensePayload | None = None
_license_error: str | None = None


def _verify_signature(payload_bytes: bytes, signature_hex: str) -> bool:
    """Verify a license signature. Returns False on any failure.

    Tries Ed25519 first against the resolved public key (operator env →
    baked-in DEV key). If that fails AND a shared ``POCKETPAW_LICENSE_SECRET``
    is configured, falls back to HMAC-SHA256 (the simpler self-hosted setup
    the e2e suite and some self-hosters rely on). With neither path available
    the signature is rejected.
    """
    public_key_hex = _resolve_public_key_hex()
    if public_key_hex:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
            pub_key.verify(bytes.fromhex(signature_hex), payload_bytes)
            return True
        except Exception:
            # An OPERATOR-configured Ed25519 key that rejects the signature is a
            # HARD reject — do NOT fall through to the weaker HMAC path. Without
            # this, any deployment that also set POCKETPAW_LICENSE_SECRET could
            # verify a forged HMAC-signed key even though the real Ed25519 key
            # rejected it, defeating the whole minting/posture scheme. Only the
            # baked-in DEV key (dev/legacy posture — production boot is already
            # blocked by enforce_license_key_posture() when the dev key is in
            # use) may fall back to HMAC, for the e2e suite / legacy self-hosters.
            if not _using_dev_public_key():
                return False

    # HMAC-SHA256 fallback with a shared secret (simpler self-hosted setup).
    secret = os.environ.get("POCKETPAW_LICENSE_SECRET", "")
    if not secret:
        return False
    expected = hashlib.sha256(f"{secret}:{payload_bytes.decode()}".encode()).hexdigest()
    return expected == signature_hex


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

    # Production posture must not verify against the bypassable DEV public key
    # (mirrors W0e's AUTH_SECRET gate). Checked before the no-key short-circuit
    # so a prod tenant that hasn't set an operator key fails loudly at boot
    # rather than appearing "unlicensed but harmless" — it is actually
    # forgeable. Raises RuntimeError under production posture.
    enforce_license_key_posture()

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
