# test_license_posture.py — Tests for the production DEV-key bypass gate.
# Created: 2026-06-10 (sov/w1a-deploy).
# Updated: 2026-06-10 (security R2b review — staging-posture blind spot):
#   added TestStagingPostureWarning + _is_ambiguous_nonprod_label() coverage —
#   a non-dev/non-prod POCKETPAW_ENV label (e.g. staging) still on the DEV key
#   now emits a LOUD warning (no raise); dev/unset stays silent; prod still
#   raises.
#
# W0a baked a committed DEV Ed25519 public key into license verification so a
# fresh checkout can mint + verify with zero setup. That key is a license
# BYPASS in production: its private seed ships in the open, so anyone can forge
# a license the DEV public key accepts. This suite proves the W1a guard that
# mirrors W0e's AUTH_SECRET fail-fast:
#   * production posture + DEV key in use  -> RuntimeError (refuse to run);
#   * production posture + operator key set -> no raise (escaped the bypass);
#   * dev/test posture + DEV key in use     -> no raise (zero-config loop OK);
#   * staging-label posture + DEV key       -> LOUD warning, no raise (R2b);
#   * _using_dev_public_key() reports the DEV-vs-operator state correctly;
#   * the gate fires from load_license() at boot, even with no license key set.
#
# Hermetic — exercises license.py directly (env + module cache only, no Mongo).

from __future__ import annotations

import logging

import pytest

pytest.importorskip("cryptography")

from pocketpaw_ee.cloud import license as lic_mod  # noqa: E402
from pocketpaw_ee.cloud import mint  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_license_env(monkeypatch):
    """Clear license + posture env and the module cache around each case."""
    for var in (
        "POCKETPAW_LICENSE_KEY",
        "POCKETPAW_LICENSE_PUBLIC_KEY",
        "POCKETPAW_LICENSE_PRIVATE_KEY",
        "POCKETPAW_LICENSE_SECRET",
        "POCKETPAW_ENV",
        "POCKETPAW_AUTH_COOKIE_SECURE",
    ):
        monkeypatch.delenv(var, raising=False)
    lic_mod._cached_license = None
    lic_mod._license_error = None
    yield
    lic_mod._cached_license = None
    lic_mod._license_error = None


# ---------------------------------------------------------------------------
# DEV-key detection
# ---------------------------------------------------------------------------


def test_using_dev_public_key_true_by_default(monkeypatch):
    monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)
    assert lic_mod._using_dev_public_key() is True


def test_using_dev_public_key_false_with_operator_key(monkeypatch):
    _priv, pub = mint.generate_keypair()
    monkeypatch.setenv("POCKETPAW_LICENSE_PUBLIC_KEY", pub)
    assert lic_mod._using_dev_public_key() is False


# ---------------------------------------------------------------------------
# Posture detection mirrors W0e
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["production", "PRODUCTION", "prod", "Prod"])
def test_is_production_true_for_env(monkeypatch, value):
    monkeypatch.setenv("POCKETPAW_ENV", value)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    assert lic_mod._is_production() is True


def test_is_production_true_for_secure_cookie(monkeypatch):
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.setenv("POCKETPAW_AUTH_COOKIE_SECURE", "true")
    assert lic_mod._is_production() is True


@pytest.mark.parametrize("env", ["", "development", "dev", "test", "staging"])
def test_is_production_false_for_non_prod(monkeypatch, env):
    monkeypatch.setenv("POCKETPAW_ENV", env)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    assert lic_mod._is_production() is False


# ---------------------------------------------------------------------------
# The gate — the core "no silent dev-key in prod" fix
# ---------------------------------------------------------------------------


def test_prod_with_dev_key_raises(monkeypatch):
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BYPASS"):
        lic_mod.enforce_license_key_posture()


def test_prod_with_operator_key_does_not_raise(monkeypatch):
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    _priv, pub = mint.generate_keypair()
    monkeypatch.setenv("POCKETPAW_LICENSE_PUBLIC_KEY", pub)
    # Escaped the bypass — must not raise.
    lic_mod.enforce_license_key_posture()


def test_dev_posture_with_dev_key_does_not_raise(monkeypatch):
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)
    # Zero-config dev loop must keep working.
    lic_mod.enforce_license_key_posture()


def test_secure_cookie_posture_with_dev_key_raises(monkeypatch):
    """The TLS signal alone (no POCKETPAW_ENV) is production posture too."""
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.setenv("POCKETPAW_AUTH_COOKIE_SECURE", "true")
    monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="forgeable|BYPASS"):
        lic_mod.enforce_license_key_posture()


# ---------------------------------------------------------------------------
# The gate fires from the boot path (load_license), even with no key set
# ---------------------------------------------------------------------------


def test_load_license_enforces_gate_in_prod_without_key(monkeypatch):
    """A prod tenant on the DEV key trips at boot before the no-key short-circuit."""
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    monkeypatch.delenv("POCKETPAW_LICENSE_KEY", raising=False)
    monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)
    lic_mod._cached_license = None
    with pytest.raises(RuntimeError, match="BYPASS"):
        lic_mod.load_license()


def test_load_license_ok_in_prod_with_operator_key(monkeypatch):
    """With an operator keypair, a prod tenant loads a real minted license."""
    priv, pub = mint.generate_keypair()
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    monkeypatch.setenv("POCKETPAW_LICENSE_PUBLIC_KEY", pub)
    monkeypatch.setenv("POCKETPAW_LICENSE_PRIVATE_KEY", priv)
    key = mint.mint_license(org="acme", plan="enterprise", seats=10, days=365)
    monkeypatch.setenv("POCKETPAW_LICENSE_KEY", key)
    lic_mod._cached_license = None

    payload = lic_mod.load_license()
    assert payload is not None
    assert payload.org == "acme"
    assert payload.plan == "enterprise"


class TestHmacFallbackHardening:
    """An operator-configured Ed25519 key must HARD-reject a bad signature —
    never silently fall through to the weaker HMAC path (which would let a
    forged HMAC-signed key pass on any deployment that also set
    POCKETPAW_LICENSE_SECRET). The dev-key legacy HMAC path stays intact.
    Regression for the review finding on license.py:_verify_signature."""

    _PAYLOAD = b'{"org":"X","plan":"enterprise","seats":5,"exp":"2027-01-01"}'

    def _hmac_sig(self, secret: str) -> str:
        import hashlib

        return hashlib.sha256(f"{secret}:{self._PAYLOAD.decode()}".encode()).hexdigest()

    def test_operator_key_hard_rejects_forged_hmac(self, monkeypatch):
        _priv, pub = mint.generate_keypair()
        monkeypatch.setenv("POCKETPAW_LICENSE_PUBLIC_KEY", pub)
        monkeypatch.setenv("POCKETPAW_LICENSE_SECRET", "legacy-secret")
        # Ed25519 rejects (sig is an HMAC digest) AND an operator key is set →
        # must NOT fall through to HMAC.
        assert lic_mod._verify_signature(self._PAYLOAD, self._hmac_sig("legacy-secret")) is False

    def test_dev_key_still_allows_legacy_hmac(self, monkeypatch):
        monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)  # dev key
        monkeypatch.setenv("POCKETPAW_LICENSE_SECRET", "legacy-secret")
        assert lic_mod._verify_signature(self._PAYLOAD, self._hmac_sig("legacy-secret")) is True

    def test_operator_key_accepts_valid_ed25519(self, monkeypatch):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv, pub = mint.generate_keypair()
        monkeypatch.setenv("POCKETPAW_LICENSE_PUBLIC_KEY", pub)
        sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv)).sign(self._PAYLOAD).hex()
        assert lic_mod._verify_signature(self._PAYLOAD, sig) is True


# ---------------------------------------------------------------------------
# Staging-posture blind spot — ambiguous label warns on the dev-key path
# (R2b review fix). The autouse _clean_license_env fixture clears env + cache.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["staging", "qa", "preprod", "uat"])
def test_is_ambiguous_nonprod_label_true_for_unknown_labels(monkeypatch, label):
    monkeypatch.setenv("POCKETPAW_ENV", label)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    assert lic_mod._is_ambiguous_nonprod_label() is True


@pytest.mark.parametrize("label", ["", "dev", "development", "local", "test"])
def test_is_ambiguous_nonprod_label_false_for_dev_or_unset(monkeypatch, label):
    if label == "":
        monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    else:
        monkeypatch.setenv("POCKETPAW_ENV", label)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    assert lic_mod._is_ambiguous_nonprod_label() is False


@pytest.mark.parametrize("label", ["production", "prod"])
def test_is_ambiguous_nonprod_label_false_for_prod(monkeypatch, label):
    monkeypatch.setenv("POCKETPAW_ENV", label)
    assert lic_mod._is_ambiguous_nonprod_label() is False


class TestStagingPostureWarning:
    """A non-dev/non-prod label (e.g. staging) still on the bypassable DEV
    license key must WARN loudly without raising — boot is unchanged, only the
    signal is added. Production still raises; explicit dev/unset stays silent."""

    def test_staging_with_dev_key_warns_no_raise(self, monkeypatch, caplog):
        monkeypatch.setenv("POCKETPAW_ENV", "staging")
        monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
        monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)  # dev key
        with caplog.at_level(logging.WARNING, logger=lic_mod.logger.name):
            # Must NOT raise — staging is not prod.
            lic_mod.enforce_license_key_posture()
        assert any(
            "non-dev, non-prod label" in r.getMessage()
            and "staging" in r.getMessage()
            and "BYPASS" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    @pytest.mark.parametrize("label", ["", "dev", "development", "local", "test"])
    def test_dev_or_unset_with_dev_key_does_not_warn(self, monkeypatch, caplog, label):
        if label == "":
            monkeypatch.delenv("POCKETPAW_ENV", raising=False)
        else:
            monkeypatch.setenv("POCKETPAW_ENV", label)
        monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
        monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)
        with caplog.at_level(logging.WARNING, logger=lic_mod.logger.name):
            lic_mod.enforce_license_key_posture()
        assert not any("non-dev, non-prod label" in r.getMessage() for r in caplog.records)

    def test_staging_with_operator_key_does_not_warn(self, monkeypatch, caplog):
        """An escaped-the-bypass staging deployment (operator key set) is fine."""
        monkeypatch.setenv("POCKETPAW_ENV", "staging")
        _priv, pub = mint.generate_keypair()
        monkeypatch.setenv("POCKETPAW_LICENSE_PUBLIC_KEY", pub)
        with caplog.at_level(logging.WARNING, logger=lic_mod.logger.name):
            lic_mod.enforce_license_key_posture()
        assert not any("non-dev, non-prod label" in r.getMessage() for r in caplog.records)

    def test_prod_with_dev_key_still_raises_not_warns(self, monkeypatch):
        """Explicit prod must still hard-fail, never fall to the warn-only path."""
        monkeypatch.setenv("POCKETPAW_ENV", "production")
        monkeypatch.delenv("POCKETPAW_LICENSE_PUBLIC_KEY", raising=False)
        with pytest.raises(RuntimeError, match="BYPASS"):
            lic_mod.enforce_license_key_posture()
