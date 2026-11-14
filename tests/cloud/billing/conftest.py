# tests/cloud/billing/conftest.py — the billing suites exercise paid flows, so
# payments are open (live mode) for every test here. test_payments_closed.py
# overrides the posture per test to prove the closed default.

import pytest


@pytest.fixture(autouse=True)
def _billing_payments_open(payments_open):  # noqa: ARG001
    return None
