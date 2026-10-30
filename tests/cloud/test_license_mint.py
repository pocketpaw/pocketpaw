# test_license_mint.py — Tests for the license MINTING path (sov/w0a-license).
# Created: 2026-06-10.
#
# Proves the new mint → verify → gate loop end to end:
#   * an Ed25519 key minted with the DEV key VERIFIES against the default
#     (DEV) public key and OPENS the ``require_license`` gate;
#   * a tampered payload is REJECTED;
#   * an expired key is REJECTED at the gate (and refused at mint time);
#   * an operator-supplied keypair (env POCKETPAW_LICENSE_PUBLIC_KEY /
#     POCKETPAW_LICENSE_PRIVATE_KEY) round-trips, and a DEV-signed key does
#     NOT verify once an operator public key is configured;
#   * the minted claim set (org/plan/seats/exp) matches what was requested.
#
# These tests are hermetic — they exercise ``require_license`` directly
# (it depends only on env / the module cache, not on MongoDB), so they run
# without a live Mongo, unlike test_e2e_api.py's HTTP fixtures.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

# The cloud tree imports beanie on load; skip cleanly if extras absent.
pytest.importorskip("cryptography")

from pocketpaw_ee.cloud import license as lic_mod  # noqa: E402
from pocketpaw_ee.cloud import mint  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_license_env(monkeypatch):
    """Isolate every test: clear license env + the module cache.

    The license module caches the parsed license and the resolution reads
    env vars, so a leaked env var or stale cache from another test would
    cross-contaminate. Reset both around each case.
    """
    for var in (
        "POCKETPAW_LICENSE_KEY",
        "POCKETPAW_LICENSE_PUBLIC_KEY",
        "POCKETPAW_LICENSE_PRIVATE_KEY",
        "POCKETPAW_LICENSE_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    lic_mod._cached_license = None
    lic_mod._license_error = None
    yield
    lic_mod._cached_license = None
    lic_mod._license_error = None


# ---------------------------------------------------------------------------
# Mint → verify round-trip
# ---------------------------------------------------------------------------


def test_minted_key_verifies_with_dev_key():
    key = mint.mint_license(org="acme", plan="enterprise", seats=100, days=365)
    payload = lic_mod.validate_license_key(key)
    assert payload.org == "acme"
    assert payload.plan == "enterprise"
    assert payload.seats == 100
    assert not payload.expired


def test_minted_claims_match_request():
    key = mint.mint_license(
        org="globex", plan="pro", seats=42, exp="2030-12-31", features=["foo", "bar"]
    )
    payload = lic_mod.validate_license_key(key)
    assert payload.org == "globex"
    assert payload.plan == "pro"
    assert payload.seats == 42
    assert payload.exp == "2030-12-31"
    assert payload.features == ["foo", "bar"]


async def test_minted_key_opens_require_license_gate(monkeypatch):
    """A minted key set as POCKETPAW_LICENSE_KEY opens the EE gate."""
    key = mint.mint_license(org="acme", plan="enterprise", seats=10, days=365)
    monkeypatch.setenv("POCKETPAW_LICENSE_KEY", key)
    lic_mod._cached_license = None  # force a fresh load

    payload = await lic_mod.require_license()  # raises HTTPException(403) on failure
    assert payload.org == "acme"
    assert payload.plan == "enterprise"


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


def test_tampered_payload_is_rejected():
    key = mint.mint_license(org="acme", plan="enterprise", seats=10, days=365)
    raw = base64.b64decode(key).decode()
    payload_str, sig = raw.rsplit(".", 1)
    data = json.loads(payload_str)
    data["seats"] = 99999  # bump seats but keep the original signature
    tampered_raw = f"{json.dumps(data)}.{sig}"
    tampered_key = base64.b64encode(tampered_raw.encode()).decode()

    with pytest.raises(ValueError, match="signature"):
        lic_mod.validate_license_key(tampered_key)


def test_mint_refuses_already_expired():
    with pytest.raises(ValueError, match="expired"):
        mint.mint_license(org="acme", exp="2000-01-01")


def test_expired_key_rejected_at_gate(monkeypatch):
    """An expired (but correctly signed) key fails validation.

    We bypass ``mint_license``'s expiry guard by signing a past-dated payload
    directly, then prove the verifier rejects it.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from pocketpaw_ee.cloud._dev_license_key import DEV_PRIVATE_KEY_HEX

    past = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d")
    payload_str = json.dumps({"org": "acme", "plan": "go", "seats": 5, "exp": past})
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(DEV_PRIVATE_KEY_HEX))
    sig = sk.sign(payload_str.encode()).hex()
    expired_key = base64.b64encode(f"{payload_str}.{sig}".encode()).decode()

    with pytest.raises(ValueError, match="expired"):
        lic_mod.validate_license_key(expired_key)


def test_garbage_key_rejected():
    with pytest.raises(ValueError):
        lic_mod.validate_license_key("not-a-real-key")


# ---------------------------------------------------------------------------
# Operator key precedence (production path)
# ---------------------------------------------------------------------------


def test_operator_keypair_round_trips(monkeypatch, tmp_path):
    """A freshly generated operator keypair mints + verifies, and a
    DEV-signed key does NOT verify once the operator public key is set."""
    priv_hex, pub_hex = mint.generate_keypair()
    key_file = tmp_path / "operator.key"
    key_file.write_text(priv_hex)

    # Configure the verifier to trust ONLY the operator public key.
    monkeypatch.setenv("POCKETPAW_LICENSE_PUBLIC_KEY", pub_hex)

    op_key = mint.mint_license(
        org="acme", plan="enterprise", seats=50, days=90, private_key_file=str(key_file)
    )
    payload = lic_mod.validate_license_key(op_key)
    assert payload.org == "acme"
    assert payload.seats == 50

    # A key signed with the DEV seed must NOT verify against the operator key.
    dev_key = mint.mint_license(org="acme", plan="enterprise", seats=50, days=90)
    with pytest.raises(ValueError, match="signature"):
        lic_mod.validate_license_key(dev_key)


def test_private_key_resolution_precedence(monkeypatch, tmp_path):
    """File > env > DEV. Env hex is used when no file is passed."""
    priv_hex, _pub_hex = mint.generate_keypair()
    monkeypatch.setenv("POCKETPAW_LICENSE_PRIVATE_KEY", priv_hex)
    seed, is_dev = mint.resolve_private_seed(None)
    assert seed == bytes.fromhex(priv_hex)
    assert is_dev is False

    # The committed DEV seed is the zero-config fallback.
    monkeypatch.delenv("POCKETPAW_LICENSE_PRIVATE_KEY", raising=False)
    _seed, is_dev = mint.resolve_private_seed(None)
    assert is_dev is True


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_mint_emits_verifiable_key(capsys, monkeypatch):
    rc = mint.main(["mint", "--org", "acme", "--plan", "go", "--seats", "7", "--days", "30"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = lic_mod.validate_license_key(out)
    assert payload.org == "acme"
    assert payload.seats == 7


def test_cli_verify_roundtrip(capsys):
    key = mint.mint_license(org="acme", plan="go", seats=5, days=30)
    rc = mint.main(["verify", key])
    assert rc == 0
    assert "VALID" in capsys.readouterr().out


def test_cli_verify_rejects_garbage(capsys):
    rc = mint.main(["verify", "garbage"])
    assert rc == 1


def test_cli_generate_keypair(capsys):
    rc = mint.main(["generate-keypair"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PUBLIC" in out
    # public key is 64 hex chars
    pub = out.split(":")[-1].strip()
    assert len(bytes.fromhex(pub)) == 32
