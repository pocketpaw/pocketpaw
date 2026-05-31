"""TOTP MFA service — secrets, codes, QR rendering, backup codes.

Stateless helpers used by ``auth/router.py`` endpoints. The secret is
stored in plaintext on the user document (TOTP needs it for HMAC) —
backup codes are stored as sha256 hashes and only ever returned to the
client once.
"""

from __future__ import annotations

import hashlib
import io
import secrets
from typing import TYPE_CHECKING

import pyotp
import qrcode
import qrcode.image.svg

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.user import User


def generate_secret() -> str:
    return pyotp.random_base32()


def build_otpauth_url(secret: str, email: str, issuer: str = "PocketPaw") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def build_qr_svg(otpauth_url: str) -> str:
    # SvgPathImage emits a single <path> with a viewBox; SvgImage emits
    # <svg:rect> with an XML namespace prefix that the HTML parser
    # misrenders when injected via Svelte's {@html ...}, leaving the QR
    # blank. Path form works in any browser without prefix juggling.
    img = qrcode.make(otpauth_url, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def verify_totp(secret: str, code: str, *, valid_window: int = 1) -> bool:
    # Why: valid_window=1 tolerates ±30s drift between client and server clocks.
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=valid_window)


def _normalize_backup_code(code: str) -> str:
    return code.strip().lower().replace("-", "")


def hash_backup_code(code: str) -> str:
    return hashlib.sha256(_normalize_backup_code(code).encode("utf-8")).hexdigest()


def generate_backup_codes(n: int = 10) -> tuple[list[str], list[str]]:
    plaintext: list[str] = []
    for _ in range(n):
        raw = secrets.token_hex(4)  # 8 hex chars
        plaintext.append(f"{raw[:4]}-{raw[4:]}")
    hashed = [hash_backup_code(c) for c in plaintext]
    return plaintext, hashed


def consume_backup_code(user: User, code: str) -> bool:
    target = hash_backup_code(code)
    if target in user.mfa_backup_codes:
        user.mfa_backup_codes = [c for c in user.mfa_backup_codes if c != target]
        return True
    return False


__all__ = [
    "build_otpauth_url",
    "build_qr_svg",
    "consume_backup_code",
    "generate_backup_codes",
    "generate_secret",
    "hash_backup_code",
    "verify_totp",
]
