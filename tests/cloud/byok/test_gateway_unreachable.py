# tests/cloud/byok/test_gateway_unreachable.py — a gateway this server cannot
# reach is refused clearly, not as a 500.
#
# Created 2026-09-13 (fix/byok-gateway-unreachable). Found on the live kiosk an
# hour into launch day: saving a custom gateway key returned "Internal server
# error — see server logs for details". The address resolved and passed the
# egress guard, then httpx could not open a connection, and nothing caught it.
#
# The validator's own docstring says network trouble is not a bad key and lets
# the transport error out "so the caller can decide". No caller decided: the
# route has no handler, so it reached the generic 500 page. These tests pin the
# deciding.
#
# Mutations: tests/mutations/byok_gateway_unreachable.json.

from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.byok import service as byok_service

pytestmark = pytest.mark.asyncio

_KEY = "sk-" + "x" * 40
_BASE = "https://api.example-gateway.test/v1"


class _Boom:
    """An httpx client whose post always fails at the transport layer."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, *_a, **_k):
        raise self._exc


@pytest.fixture
def _egress_ok(monkeypatch):
    """The address resolves and passes the guard — the real failure's shape."""

    async def _ok(base_url):
        from pocketpaw.security.url_validators import EgressTarget

        base = base_url.rstrip("/")
        return EgressTarget(
            url=base, host="api.example-gateway.test", port=443, pinned_ip="93.184.216.34"
        )

    monkeypatch.setattr(byok_service, "assert_gateway_egress", _ok)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("read timed out"),
    ],
)
async def test_an_unreachable_gateway_is_a_clear_refusal(_egress_ok, monkeypatch, exc):
    """Every transport failure names the address instead of 500ing.

    Parametrised because the live failure was a timeout and the first fix only
    caught ConnectError. httpx.TransportError is the shared base and is what
    the code catches.

    Mutation that must break this: remove the except.
    """
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: _Boom(exc))

    with pytest.raises(ValidationError) as err:
        await byok_service.validate_key(
            _KEY, provider="openai_compatible", base_url=_BASE, model="some-model"
        )

    assert err.value.code == "byok.gateway_unreachable"
    assert _BASE in err.value.message


async def test_the_refusal_says_the_key_was_not_judged(_egress_ok, monkeypatch):
    """The person is holding a key that may be perfectly good. Telling them it
    was rejected sends them to re-copy something that was never the problem.
    """
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: _Boom(httpx.ConnectError("nope")))

    with pytest.raises(ValidationError) as err:
        await byok_service.validate_key(
            _KEY, provider="openai_compatible", base_url=_BASE, model="m"
        )

    msg = err.value.message.lower()
    assert "not saved" in msg and "not rejected" in msg


async def test_anthropic_unreachable_is_also_a_refusal(monkeypatch):
    """The same hole existed on the anthropic path, with its own code: that URL
    is ours, not the user's, so it points at our egress rather than their key.
    """
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: _Boom(httpx.ConnectTimeout("t")))

    with pytest.raises(ValidationError) as err:
        await byok_service.validate_key(_KEY, provider="anthropic")

    assert err.value.code == "byok.provider_unreachable"


async def test_a_real_rejection_still_reads_as_a_rejection(_egress_ok, monkeypatch):
    """The default path. Without this, catching everything as unreachable would
    tell someone with a revoked key that the gateway is down.
    """

    class _Rejects(_Boom):
        def __init__(self):
            pass

        async def post(self, *_a, **_k):
            return httpx.Response(401, json={"error": "bad key"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: _Rejects())

    with pytest.raises(ValidationError) as err:
        await byok_service.validate_key(
            _KEY, provider="openai_compatible", base_url=_BASE, model="m"
        )

    assert err.value.code in {"byok.key_rejected", "byok.base_url_rejected"}
