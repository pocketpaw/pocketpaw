# tests/cloud/test_pocket_backend_crypto.py — RFC 04 alpha.
# Created: 2026-05-21 — Coverage for the pocket-backend credential crypto
# helper and the strict external-URL validator.
#
# What this pins:
#   1. encrypt_token -> decrypt_token round-trips.
#   2. Two encryptions of the same token use different salts/nonces and
#      therefore produce different ciphertext.
#   3. decrypt with a mismatched salt fails (InvalidTag).
#   4. Missing AUTH_SECRET raises a clear error.
#   5. validate_external_url_strict rejects http://, localhost, loopback,
#      the EC2 metadata IP, RFC1918 ranges, and empty input — and accepts
#      a normal https host.

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from pocketpaw.security.url_validators import validate_external_url_strict


@pytest.fixture
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "unit-test-auth-secret-value")


def test_encrypt_decrypt_round_trip(auth_secret):
    from pocketpaw_ee.cloud.pockets import backend_crypto

    token = "ghp_supersecrettoken1234567890"
    ciphertext, nonce, salt = backend_crypto.encrypt_token(token)
    assert backend_crypto.decrypt_token(ciphertext, nonce, salt) == token


def test_different_salts_produce_different_ciphertext(auth_secret):
    from pocketpaw_ee.cloud.pockets import backend_crypto

    token = "the-same-token"
    ct1, n1, s1 = backend_crypto.encrypt_token(token)
    ct2, n2, s2 = backend_crypto.encrypt_token(token)

    assert s1 != s2
    assert n1 != n2
    assert ct1 != ct2
    # Both still decrypt back to the original.
    assert backend_crypto.decrypt_token(ct1, n1, s1) == token
    assert backend_crypto.decrypt_token(ct2, n2, s2) == token


def test_decrypt_with_wrong_salt_fails(auth_secret):
    from pocketpaw_ee.cloud.pockets import backend_crypto

    ct, nonce, _salt = backend_crypto.encrypt_token("secret")
    _, _, other_salt = backend_crypto.encrypt_token("other")
    with pytest.raises(InvalidTag):
        backend_crypto.decrypt_token(ct, nonce, other_salt)


def test_missing_auth_secret_raises(monkeypatch):
    from pocketpaw_ee.cloud.pockets import backend_crypto

    monkeypatch.delenv("AUTH_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        backend_crypto.encrypt_token("secret")


# ---------------------------------------------------------------------------
# validate_external_url_strict
# ---------------------------------------------------------------------------


def test_strict_url_accepts_normal_https():
    assert validate_external_url_strict("https://api.example.com") == "https://api.example.com"
    assert (
        validate_external_url_strict("https://api.example.com/v1") == "https://api.example.com/v1"
    )


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://api.example.com",  # plain http rejected
        "https://localhost",
        "https://127.0.0.1",
        "https://127.5.5.5",
        "https://169.254.169.254",  # EC2 metadata
        "https://10.0.0.5",
        "https://192.168.1.1",
        "https://172.16.0.1",
        "ftp://api.example.com",
    ],
)
def test_strict_url_rejects_unsafe(bad_url):
    with pytest.raises(ValueError):
        validate_external_url_strict(bad_url)


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_strict_url_rejects_empty(empty):
    with pytest.raises(ValueError):
        validate_external_url_strict(empty)  # type: ignore[arg-type]
