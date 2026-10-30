# tests/cloud/billing/test_gateway_error_mapping.py — proves a gateway refusal
# reaches the client as the refusal it is, not as a 500.
#
# THE BUG. Not one call in the Dodo provider caught the SDK's exceptions. That
# exception is not a ``CloudError``, so ``cloud_error_handler`` never saw it, the
# router had no handler for it, and it fell all the way through to uvicorn.
# Changing a site's plan while the previous change was still awaiting payment is
# what surfaced it in production:
#
#     ConflictError: Error code: 409 - {'code': 'PENDING_PLAN_CHANGE_EXISTS',
#     'message': 'A pending plan change already exists for this subscription.
#     Please wait for the current payment to complete.'}
#     POST /api/v1/sites/publish  ->  500 Internal Server Error
#
# The gateway had said exactly what was wrong and exactly what to do about it,
# and the buyer got "Internal Server Error" and a stack trace in the logs.
#
# Two things are asserted, because fixing only the first leaves the same hole for
# the next gateway code nobody anticipated:
#
#   * the pending-plan-change conflict maps to a 409 whose message tells the buyer
#     to wait, rather than inviting a retry that fails identically; and
#   * EVERY SDK failure becomes a CloudError, so no gateway response can reach the
#     client as an unhandled exception again.
#
# An unclassifiable failure deliberately stays a 500. This is about the CONTRACT
# being honoured, not about relabelling our own outages as the caller's fault.
#
# Created 2026-09-02 (fix/site-plan-change-gateway-conflict): new test module.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Internal,
    RateLimited,
    ValidationError,
)
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider

SUB_ID = "sub_0NmcG4qDOOs4hvA9Z5ssk"


def _provider() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=None,
        credit_product_id="prod_credits_sku",
        plan_products={},
    )


def _status_error(status: int, *, code: str = "", message: str = ""):
    """A real SDK status error, built the way the SDK builds one."""
    from dodopayments import APIStatusError

    body: dict = {}
    if code:
        body["code"] = code
    if message:
        body["message"] = message
    request = httpx.Request("POST", f"https://test.dodopayments.com/subscriptions/{SUB_ID}")
    response = httpx.Response(status, json=body, request=request)
    return APIStatusError(f"Error code: {status}", response=response, body=body or None)


def _sdk_raising(exc: Exception, monkeypatch) -> MagicMock:
    client = MagicMock()
    client.subscriptions.change_plan = AsyncMock(side_effect=exc)
    client.subscriptions.update = AsyncMock(side_effect=exc)
    client.checkout_sessions.create = AsyncMock(side_effect=exc)
    client.payments.create = AsyncMock(side_effect=exc)

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=client))
    return client


async def _change_plan():
    await _provider().change_plan(
        subscription_id=SUB_ID, product_id="prod_site_business", plan_key="business", addons=[]
    )


# ===========================================================================
# The production failure.
# ===========================================================================


async def test_a_pending_plan_change_is_a_conflict_not_a_server_error(monkeypatch):
    """The exact 409 that made publish return 500."""
    _sdk_raising(
        _status_error(
            409,
            code="PENDING_PLAN_CHANGE_EXISTS",
            message="A pending plan change already exists for this subscription. "
            "Please wait for the current payment to complete.",
        ),
        monkeypatch,
    )

    with pytest.raises(ConflictError) as caught:
        await _change_plan()

    assert caught.value.status_code == 409
    assert caught.value.code == "billing.plan_change_pending"


async def test_the_pending_change_message_says_to_wait_not_to_retry(monkeypatch):
    """Retrying fails identically, so the copy must not suggest it."""
    _sdk_raising(
        _status_error(409, code="PENDING_PLAN_CHANGE_EXISTS", message="whatever the gateway said"),
        monkeypatch,
    )

    with pytest.raises(ConflictError) as caught:
        await _change_plan()

    assert "settle" in caught.value.message.lower()


async def test_the_sdk_exception_is_kept_as_the_cause(monkeypatch):
    """The gateway's own error stays in the logs even though the client sees ours."""
    original = _status_error(409, code="PENDING_PLAN_CHANGE_EXISTS")
    _sdk_raising(original, monkeypatch)

    with pytest.raises(ConflictError) as caught:
        await _change_plan()

    assert caught.value.__cause__ is original


# ===========================================================================
# The general hole: no SDK failure may escape as an unhandled exception.
# ===========================================================================


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ValidationError),
        (404, ValidationError),
        (422, ValidationError),
        (409, ConflictError),
        (429, RateLimited),
        (401, Internal),
        (403, Internal),
        (500, Internal),
        (502, Internal),
    ],
)
async def test_every_gateway_status_becomes_a_cloud_error(status, expected, monkeypatch):
    _sdk_raising(_status_error(status, message="gateway said no"), monkeypatch)

    with pytest.raises(expected):
        await _change_plan()


async def test_a_credential_rejection_is_ours_not_the_buyers(monkeypatch):
    """A 403 here would tell a paying customer they lack permission they have."""
    _sdk_raising(_status_error(403, message="invalid api key"), monkeypatch)

    with pytest.raises(Internal) as caught:
        await _change_plan()

    assert caught.value.status_code == 500
    assert caught.value.code == "billing.gateway_unauthorized"
    assert "invalid api key" not in caught.value.message, "our key material stays out of the body"


async def test_a_transport_failure_is_still_a_cloud_error(monkeypatch):
    """Not every SDK failure is a status error; a dropped connection is not."""
    _sdk_raising(RuntimeError("connection reset"), monkeypatch)

    with pytest.raises(CloudError):
        await _change_plan()


async def test_the_gateway_message_survives_on_the_caller_facing_statuses(monkeypatch):
    """It is the only specific thing we know about the refusal."""
    _sdk_raising(_status_error(422, message="product_id is not sellable"), monkeypatch)

    with pytest.raises(ValidationError) as caught:
        await _change_plan()

    assert "product_id is not sellable" in caught.value.message


async def test_cancellation_failures_are_mapped_too(monkeypatch):
    _sdk_raising(_status_error(409, message="already cancelled"), monkeypatch)

    with pytest.raises(ConflictError):
        await _provider().cancel_subscription(SUB_ID)


async def test_a_successful_change_plan_still_returns_none(monkeypatch):
    """The wrapper must not change the happy path."""
    client = _sdk_raising(_status_error(409), monkeypatch)
    client.subscriptions.change_plan = AsyncMock(return_value=None)

    assert await _change_plan() is None
