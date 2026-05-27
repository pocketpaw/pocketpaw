"""Tests for the password policy + HIBP breach check."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from fastapi_users.exceptions import InvalidPasswordException
from pocketpaw_ee.cloud.auth import password_policy


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    password_policy._hibp_cache.clear()


@pytest.fixture
def hibp_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_HIBP_ENABLED", "false")


async def test_too_short(hibp_off: None) -> None:
    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async("Aa1!aaaa", email="x@y.z")
    assert exc.value.reason == "too_short"


async def test_missing_uppercase(hibp_off: None) -> None:
    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async("strongpass123!", email="x@y.z")
    assert exc.value.reason == "missing_uppercase"


async def test_missing_lowercase(hibp_off: None) -> None:
    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async("STRONGPASS123!", email="x@y.z")
    assert exc.value.reason == "missing_lowercase"


async def test_missing_digit(hibp_off: None) -> None:
    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async("StrongPasss!!", email="x@y.z")
    assert exc.value.reason == "missing_digit"


async def test_missing_symbol(hibp_off: None) -> None:
    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async("StrongPass123", email="x@y.z")
    assert exc.value.reason == "missing_symbol"


async def test_email_local_part(hibp_off: None) -> None:
    # local part is "StrongPass123!" (case-insensitive match).
    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async("StrongPass123!", email="strongpass123!@y.z")
    assert exc.value.reason == "email_local_part"


async def test_email_local_part_substring_rejected(hibp_off: None) -> None:
    """``prakash@x.com`` mustn't be able to set ``Prakash123!`` — the
    local part still appears verbatim in the password, just with extra
    characters tacked on. The earlier exact-equality check missed this."""
    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async("Prakash-2026!", email="prakash@x.com")
    assert exc.value.reason == "email_local_part"


async def test_short_local_part_does_not_block_unrelated_password(hibp_off: None) -> None:
    """A 2-char local part like ``jo@x.com`` shouldn't poison every
    password that happens to contain ``jo``."""
    # No exception.
    await password_policy.validate_password_async("StrongPass123!", email="jo@x.com")


async def test_passing_with_hibp_disabled(hibp_off: None) -> None:
    await password_policy.validate_password_async("StrongPass123!", email="x@y.z")


async def test_hibp_hit_raises_breached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_HIBP_ENABLED", "true")
    password = "StrongPass123!"
    sha1 = hashlib.sha1(password.encode()).hexdigest().upper()
    suffix = sha1[5:]
    body = f"{suffix}:42\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:1\n"

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    with pytest.raises(InvalidPasswordException) as exc:
        await password_policy.validate_password_async(password, email="x@y.z")
    assert exc.value.reason == "breached"


async def test_hibp_miss_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_HIBP_ENABLED", "true")
    body = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:1\nBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB:2\n"

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    await password_policy.validate_password_async("StrongPass123!", email="x@y.z")


async def test_hibp_network_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_HIBP_ENABLED", "true")

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network down")

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    await password_policy.validate_password_async("StrongPass123!", email="x@y.z")
