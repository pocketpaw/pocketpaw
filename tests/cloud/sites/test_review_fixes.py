# tests/cloud/sites/test_review_fixes.py — covers two Sites review fixes:
#   * C1 — _cf_client() raises a CloudError (ValidationError, 422), NOT a raw
#     KeyError (an unhandled 500), when a required PAW_CF_* env var is missing.
#     add_domain calls _cf_client directly, so an unconfigured Cloudflare must
#     surface a clean mapped error instead of crashing.
#   * S2 — DomainRequest.hostname is validated with a permissive DNS-hostname
#     pattern: real hostnames pass; obvious garbage (spaces, bad chars,
#     single-label names, leading/trailing/double dots) is rejected at the DTO.
#
# Both checks are synchronous and DB-free — _cf_client only reads env, and
# DomainRequest is a pure Pydantic model. No mongo fixture needed.
#
# Created 2026-06-24 (integration/billing-credits review fixes C1 + S2).

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import DomainRequest
from pydantic import ValidationError as PydanticValidationError

_CF_VARS = ("PAW_CF_ACCOUNT_ID", "PAW_CF_API_TOKEN", "PAW_CF_ZONE_ID")


# ---------------------------------------------------------------------------
# C1 — _cf_client() guards on a missing env var (CloudError, not KeyError).
# ---------------------------------------------------------------------------


def test_cf_client_missing_account_id_raises_cloud_error_not_keyerror(monkeypatch):
    # All CF vars unset → _cf_client must raise a mapped CloudError, NOT KeyError.
    for var in _CF_VARS:
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(CloudError) as exc:
        sites_service._cf_client()

    # It is the 422 ValidationError carrying the "not configured" message — and it
    # is a CloudError (so the cloud error handler maps it), never a raw KeyError.
    assert isinstance(exc.value, ValidationError)
    assert exc.value.status_code == 422
    assert exc.value.code == "sites.cloudflare_unconfigured"
    assert "Cloudflare is not configured" in exc.value.message


def test_cf_client_partial_config_still_raises_cloud_error(monkeypatch):
    # Account id present but token + zone missing → still a clean CloudError.
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_123")
    monkeypatch.delenv("PAW_CF_API_TOKEN", raising=False)
    monkeypatch.delenv("PAW_CF_ZONE_ID", raising=False)

    with pytest.raises(ValidationError) as exc:
        sites_service._cf_client()
    assert exc.value.code == "sites.cloudflare_unconfigured"


def test_cf_client_does_not_raise_keyerror(monkeypatch):
    # Explicitly assert the OLD failure mode (KeyError) no longer occurs.
    for var in _CF_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(CloudError):
        sites_service._cf_client()  # must not be KeyError (which CloudError isn't)


# ---------------------------------------------------------------------------
# S2 — DomainRequest.hostname validation (permissive DNS hostname).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostname",
    [
        "example.com",
        "www.example.com",
        "sub.domain.example.co.uk",
        "my-site.example.com",
        "a.bc",
        "EXAMPLE.COM",  # case-insensitive
        "example.com.",  # trailing-dot FQDN form is normalized away
    ],
)
def test_valid_hostnames_accepted(hostname):
    req = DomainRequest(hostname=hostname)
    # The trailing dot is normalized off; everything else round-trips.
    assert req.hostname == hostname.rstrip(".")


@pytest.mark.parametrize(
    "hostname",
    [
        "",  # empty
        "   ",  # whitespace
        "localhost",  # single label (no TLD)
        "example",  # single label
        "exa mple.com",  # space
        "-example.com",  # leading hyphen label
        "example-.com",  # trailing hyphen label
        "example..com",  # double dot / empty label
        ".example.com",  # leading dot
        "example.com/path",  # path / illegal char
        "http://example.com",  # scheme / illegal chars
        "exam$ple.com",  # illegal char
    ],
)
def test_invalid_hostnames_rejected(hostname):
    with pytest.raises(PydanticValidationError):
        DomainRequest(hostname=hostname)
