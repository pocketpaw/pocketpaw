# mint.py — Enterprise license MINTING (the signing counterpart to license.py).
# Created: 2026-06-10 (sov/w0a-license).
#
# WHY: until now only license VERIFICATION existed; there was no way to
# produce a working POCKETPAW_LICENSE_KEY, so every governed EE endpoint 403'd.
# This module signs a LicensePayload with Ed25519 and emits a key in the EXACT
# shape ``license.validate_license_key`` consumes:
#     base64( payload_json + "." + signature_hex )
# The signature is Ed25519 over ``payload_json.encode()`` — byte-for-byte the
# same bytes the verifier feeds to ``_verify_signature`` — so a minted key
# round-trips and opens ``require_license``.
#
# Private-key resolution (operator key wins; dev seed is the zero-config
# fallback):
#   1. ``--private-key-file <path>`` (raw 32 bytes or 64-char hex), OR
#   2. ``POCKETPAW_LICENSE_PRIVATE_KEY`` env (64-char hex), OR
#   3. the committed DEV seed (``_dev_license_key.DEV_PRIVATE_KEY_HEX``) —
#      dev/test ONLY; pair it with the DEV public key (the default verifier).
#
# CLI (registered as the ``pocketpaw-ee-license`` console script):
#   pocketpaw-ee-license mint --org acme --plan enterprise --seats 100 --days 365
#   pocketpaw-ee-license mint --org acme --exp 2027-01-01 --private-key-file ./op.key
#   pocketpaw-ee-license generate-keypair          # new operator keypair
#   pocketpaw-ee-license verify <KEY>              # round-trip check

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.license import LicensePayload

# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------


def _normalize_seed(raw: bytes) -> bytes:
    """Accept either a raw 32-byte seed or a 64-char hex-encoded seed.

    Mirrors the leniency in ``pocketpaw.bundled_templates.bundler`` so operators
    can store keys either way on disk.
    """
    if len(raw) == 32:
        return raw
    text = raw.decode("ascii", errors="ignore").strip()
    try:
        decoded = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(
            "private key must be a raw 32-byte seed or a 64-char hex string"
        ) from exc
    if len(decoded) != 32:
        raise ValueError("hex private key must decode to exactly 32 bytes")
    return decoded


def resolve_private_seed(private_key_file: str | None = None) -> tuple[bytes, bool]:
    """Resolve the Ed25519 private seed for signing.

    Returns ``(seed_bytes, is_dev)``. ``is_dev`` is True when the committed
    DEV seed was used (so callers / the CLI can warn loudly).

    Precedence: ``--private-key-file`` → ``POCKETPAW_LICENSE_PRIVATE_KEY`` →
    the committed DEV seed.
    """
    if private_key_file:
        with open(private_key_file, "rb") as fh:
            return _normalize_seed(fh.read()), False

    env_hex = os.environ.get("POCKETPAW_LICENSE_PRIVATE_KEY", "").strip()
    if env_hex:
        return _normalize_seed(env_hex.encode("ascii")), False

    from pocketpaw_ee.cloud._dev_license_key import DEV_PRIVATE_KEY_HEX

    return _normalize_seed(DEV_PRIVATE_KEY_HEX.encode("ascii")), True


def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair. Returns ``(private_hex, public_hex)``."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    return sk.private_bytes_raw().hex(), sk.public_key().public_bytes_raw().hex()


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def _resolve_exp(exp: str | None, days: int | None) -> str:
    """Resolve the ISO ``exp`` date from either an explicit date or a day delta."""
    if exp:
        # Validate the format early so we fail before signing.
        datetime.fromisoformat(exp)
        return exp
    if days is None:
        days = 365
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%d")


def mint_license(
    *,
    org: str,
    plan: str = "team",
    seats: int = 5,
    exp: str | None = None,
    days: int | None = None,
    features: list[str] | None = None,
    private_key_file: str | None = None,
) -> str:
    """Mint a signed, base64-encoded license key.

    The returned string is a drop-in ``POCKETPAW_LICENSE_KEY`` value. It is
    validated against ``LicensePayload`` (so it can never mint a payload the
    verifier would reject for shape) and signed with Ed25519 over the exact
    JSON bytes the verifier re-checks.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    resolved_exp = _resolve_exp(exp, days)

    # Build + validate through the SAME model the verifier uses. This both
    # normalizes the payload and guarantees a minted key satisfies the schema.
    payload = LicensePayload(
        org=org,
        plan=plan,
        seats=seats,
        exp=resolved_exp,
        features=features or [],
    )
    if payload.expired:
        raise ValueError(f"refusing to mint an already-expired license (exp={resolved_exp})")

    # Match the verifier's claim set exactly. ``validate_license_key`` does
    # ``json.loads(payload_str)`` then ``LicensePayload(**data)``; extra keys
    # would be ignored, missing keys default, but we emit the full set for
    # clarity and forward-compat.
    payload_dict: dict = {
        "org": payload.org,
        "plan": payload.plan,
        "seats": payload.seats,
        "exp": payload.exp,
    }
    if payload.features:
        payload_dict["features"] = payload.features

    payload_str = json.dumps(payload_dict)

    seed, _is_dev = resolve_private_seed(private_key_file)
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    signature_hex = sk.sign(payload_str.encode()).hex()

    raw = f"{payload_str}.{signature_hex}"
    return base64.b64encode(raw.encode()).decode()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pocketpaw-ee-license",
        description="Mint and inspect PocketPaw Enterprise license keys.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    mint_p = sub.add_parser("mint", help="Mint a signed license key")
    mint_p.add_argument("--org", required=True, help="Organisation / customer name")
    mint_p.add_argument(
        "--plan",
        default="team",
        choices=["team", "business", "enterprise"],
        help="License plan (default: team)",
    )
    mint_p.add_argument("--seats", type=int, default=5, help="Seat count (default: 5)")
    exp_group = mint_p.add_mutually_exclusive_group()
    exp_group.add_argument("--exp", default=None, help="Explicit ISO expiry date, e.g. 2027-01-01")
    exp_group.add_argument(
        "--days", type=int, default=None, help="Days until expiry (default 365 if --exp omitted)"
    )
    mint_p.add_argument(
        "--feature",
        action="append",
        default=None,
        dest="features",
        help="Optional feature flag (repeatable)",
    )
    mint_p.add_argument(
        "--private-key-file",
        default=None,
        help=(
            "Path to the Ed25519 signing key (raw 32 bytes or 64-char hex). "
            "Falls back to POCKETPAW_LICENSE_PRIVATE_KEY, then the DEV key."
        ),
    )

    gen_p = sub.add_parser(
        "generate-keypair", help="Generate a new operator Ed25519 keypair (hex)"
    )
    gen_p.add_argument(
        "--out", default=None, help="Write the private key (hex) to this file instead of stdout"
    )

    verify_p = sub.add_parser("verify", help="Verify a license key round-trips and is unexpired")
    verify_p.add_argument("key", help="The base64 license key to verify")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "mint":
        try:
            key = mint_license(
                org=args.org,
                plan=args.plan,
                seats=args.seats,
                exp=args.exp,
                days=args.days,
                features=args.features,
                private_key_file=args.private_key_file,
            )
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        # Warn loudly when the dev key was used so nobody ships a dev-signed
        # key to a production verifier (which uses a different public key).
        _seed_unused, is_dev = resolve_private_seed(args.private_key_file)
        if is_dev:
            print(
                "warning: signed with the DEV key — valid only against the DEV "
                "public key (the default verifier). Use --private-key-file or "
                "POCKETPAW_LICENSE_PRIVATE_KEY for production.",
                file=sys.stderr,
            )
        print(key)
        return 0

    if args.command == "generate-keypair":
        priv_hex, pub_hex = generate_keypair()
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(priv_hex + "\n")
            os.chmod(args.out, 0o600)
            print(f"private key written to {args.out} (mode 600)", file=sys.stderr)
        else:
            print(f"PRIVATE (keep secret): {priv_hex}", file=sys.stderr)
        print(f"PUBLIC  (bake into POCKETPAW_LICENSE_PUBLIC_KEY): {pub_hex}")
        return 0

    if args.command == "verify":
        from pocketpaw_ee.cloud.license import validate_license_key

        try:
            payload = validate_license_key(args.key)
        except ValueError as exc:
            print(f"INVALID: {exc}", file=sys.stderr)
            return 1
        print(
            f"VALID org={payload.org} plan={payload.plan} "
            f"seats={payload.seats} exp={payload.exp}"
        )
        return 0

    return 2  # unreachable — subparser is required


if __name__ == "__main__":
    raise SystemExit(main())
