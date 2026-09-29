"""``require_license`` caches "no key configured" the way it caches a valid key.

It runs on every enterprise request. Without the negative cache each call on an
unlicensed deployment re-ran ``load_dotenv()`` and the posture check. What must
hold: one load for many calls, the same 403 every time, and
``_cached_license = None`` (the invalidation the positive result uses, and what
the existing license tests reset) forces a fresh load.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pocketpaw_ee.cloud import license as lic_mod


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("POCKETPAW_LICENSE_KEY", "POCKETPAW_ENV", "POCKETPAW_AUTH_COOKIE_SECURE"):
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
