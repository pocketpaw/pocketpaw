"""``require_license`` caches a failed load (no key, or an invalid key) for 60 s.

It runs on every enterprise request. Without the negative cache each call on an
unlicensed deployment re-ran ``load_dotenv()`` and the posture check, and an
invalid key was signature-checked on every request. What must
hold: one load for many calls within the TTL, the same 403 every time,
``_cached_license = None`` (the invalidation the positive result uses, and what
the existing license tests reset) forces a fresh load, and a key added after the
TTL opens the gate without a restart.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pocketpaw_ee.cloud import license as lic_mod


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "POCKETPAW_LICENSE_KEY",
        "POCKETPAW_LICENSE_PUBLIC_KEY",
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


async def _denied() -> str:
    with pytest.raises(HTTPException) as exc:
        await lic_mod.require_license()
    assert exc.value.status_code == 403
    return exc.value.detail


async def test_no_key_is_loaded_once_across_requests() -> None:
    with (
        patch("dotenv.load_dotenv") as dotenv,
        patch.object(lic_mod, "enforce_license_key_posture") as posture,
    ):
        details = [await _denied() for _ in range(5)]
    assert dotenv.call_count == 1
    assert posture.call_count == 1
    assert set(details) == {"No license key configured (set POCKETPAW_LICENSE_KEY)"}
    assert lic_mod.get_license() is None
    assert lic_mod.get_license_info().valid is False


async def test_resetting_the_cache_forces_a_fresh_load() -> None:
    with patch("dotenv.load_dotenv") as dotenv:
        await _denied()
        await _denied()
        assert dotenv.call_count == 1
        lic_mod._cached_license = None
        await _denied()
        assert dotenv.call_count == 2


async def test_a_key_added_after_the_ttl_opens_the_gate_without_a_restart(monkeypatch) -> None:
    from pocketpaw_ee.cloud import mint

    with patch("dotenv.load_dotenv") as dotenv:
        await _denied()
        # The no-key result is trusted for the TTL, and no longer.
        remaining = lic_mod._no_license_until - time.monotonic()
        assert lic_mod._NO_LICENSE_TTL_SECONDS - 5 < remaining <= lic_mod._NO_LICENSE_TTL_SECONDS

        # Inside the TTL a key that appears in the env is not looked at.
        monkeypatch.setenv(
            "POCKETPAW_LICENSE_KEY",
            mint.mint_license(org="late", plan="enterprise", seats=5, days=30),
        )
        await _denied()
        assert dotenv.call_count == 1

        # Past it, the next request re-checks once and the gate opens.
        lic_mod._no_license_until = time.monotonic() - 1
        lic = await lic_mod.require_license()
        assert lic.org == "late"
        assert dotenv.call_count == 2
        assert (await lic_mod.require_license()) is lic
        assert dotenv.call_count == 2


async def test_an_invalid_key_is_cached_for_the_ttl_then_re_verified(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_LICENSE_KEY", "not-a-license")
    with patch.object(lic_mod, "validate_license_key", wraps=lic_mod.validate_license_key) as v:
        details = [await _denied() for _ in range(5)]
        assert v.call_count == 1
        assert len(set(details)) == 1 and "Invalid license key" in details[0]
        remaining = lic_mod._no_license_until - time.monotonic()
        assert lic_mod._NO_LICENSE_TTL_SECONDS - 5 < remaining <= lic_mod._NO_LICENSE_TTL_SECONDS

        lic_mod._no_license_until = time.monotonic() - 1
        await _denied()
        assert v.call_count == 2
