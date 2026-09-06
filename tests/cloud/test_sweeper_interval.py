# tests/cloud/test_sweeper_interval.py — the sweep heartbeat's cadence knob.
#
# This loop is the only thing between a finished run and a visible charge:
# nothing bills at run completion, so a customer sees a charge one tick after the
# run ends. The interval was hardcoded at 300 while every sibling sweeper in the
# codebase already read its own from env, which meant tuning billing latency
# required editing source.
#
# The floor is the part worth testing rather than the happy path. Each tick
# queries unbilled runs, iterates every provisioned tenant, and in the LiteLLM
# cutover modes calls the proxy admin API once per tenant — so a one-second
# interval hammers the proxy instead of speeding anything up.
#
# Created 2026-09-02 (chore/sweeper-interval): new test.

import pytest
from pocketpaw_ee.extensions import (
    _DEFAULT_SWEEP_INTERVAL_SECONDS,
    _ENV_SWEEP_INTERVAL,
    _MIN_SWEEP_INTERVAL_SECONDS,
    _sweep_interval_seconds,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(_ENV_SWEEP_INTERVAL, raising=False)


def test_unset_keeps_the_historical_cadence():
    # 300s is what the loop did before it was configurable; making it tunable
    # must not silently change any existing deployment's behaviour.
    assert _sweep_interval_seconds() == 300
    assert _DEFAULT_SWEEP_INTERVAL_SECONDS == 300


def test_a_valid_value_is_honoured(monkeypatch):
    monkeypatch.setenv(_ENV_SWEEP_INTERVAL, "60")
    assert _sweep_interval_seconds() == 60


def test_below_the_floor_is_clamped_not_honoured(monkeypatch):
    monkeypatch.setenv(_ENV_SWEEP_INTERVAL, "1")
    assert _sweep_interval_seconds() == _MIN_SWEEP_INTERVAL_SECONDS


def test_exactly_the_floor_is_allowed(monkeypatch):
    monkeypatch.setenv(_ENV_SWEEP_INTERVAL, str(_MIN_SWEEP_INTERVAL_SECONDS))
    assert _sweep_interval_seconds() == _MIN_SWEEP_INTERVAL_SECONDS


@pytest.mark.parametrize("raw", ["abc", "", "   ", "5m", "300.5"])
def test_an_unparseable_value_falls_back_rather_than_crashing_boot(monkeypatch, raw):
    # This runs at process start. A typo in an env var must not stop the sweeps
    # from running at all — that would silently halt billing.
    monkeypatch.setenv(_ENV_SWEEP_INTERVAL, raw)
    assert _sweep_interval_seconds() == _DEFAULT_SWEEP_INTERVAL_SECONDS


def test_a_longer_interval_is_allowed(monkeypatch):
    # There is no ceiling: a deployment that wants an hourly sweep may have one.
    monkeypatch.setenv(_ENV_SWEEP_INTERVAL, "3600")
    assert _sweep_interval_seconds() == 3600
