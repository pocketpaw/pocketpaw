# tests/cloud/auth/test_guest_cap_env_floor.py — the env FLOOR under the guest
# caps (POCKETPAW_GUEST_SESSIONS / POCKETPAW_GUEST_TURNS_PER_DAY).
#
# Created 2026-09-10 (feat/guest-cap-env-override).
#
# The knob exists so a dev box can raise the caps out of the way. The tests
# that matter are the ones where it must NOT fire: unset, garbage, zero and
# negative all have to leave the stored cap standing, because every one of
# those is a way an operator turns the gate off by accident. Zero has its own
# test — ``try_spend_turn`` reads cap <= 0 as "refuse", so an operator writing
# 0 for "unlimited" would block every guest turn.
#
# Mutations that must break these: tests/mutations/guest_cap_env_floor.json.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.auth import guest_budget
from pocketpaw_ee.cloud.models.user import GuestLimits


def _guest(sessions: int = 2, turns: int = 40):
    """Only ``.guest_limits`` is read here, so a stand-in keeps these tests off
    the database — constructing a real ``User`` needs beanie initialized, which
    is a dependency of the fixture and not of the thing under test. The seams
    that DO drive a real guest row live in test_guest_gates.py."""
    return SimpleNamespace(guest_limits=GuestLimits(sessions=sessions, turns_per_day=turns))


def test_unset_env_keeps_stored_caps(monkeypatch):
    monkeypatch.delenv(guest_budget.SESSIONS_ENV, raising=False)
    monkeypatch.delenv(guest_budget.TURNS_ENV, raising=False)
    limits = guest_budget.limits_for(_guest())
    assert (limits.sessions, limits.turns_per_day) == (2, 40)


def test_no_stored_limits_falls_back_to_defaults(monkeypatch):
    monkeypatch.delenv(guest_budget.SESSIONS_ENV, raising=False)
    monkeypatch.delenv(guest_budget.TURNS_ENV, raising=False)
    user = _guest()
    user.guest_limits = None
    limits = guest_budget.limits_for(user)
    assert limits.sessions == guest_budget.DEFAULT_LIMITS.sessions
    assert limits.turns_per_day == guest_budget.DEFAULT_LIMITS.turns_per_day


def test_env_raises_both_caps(monkeypatch):
    monkeypatch.setenv(guest_budget.SESSIONS_ENV, "500")
    monkeypatch.setenv(guest_budget.TURNS_ENV, "9000")
    limits = guest_budget.limits_for(_guest())
    assert (limits.sessions, limits.turns_per_day) == (500, 9000)


def test_env_is_read_per_call_not_at_import(monkeypatch):
    """A module-level read would freeze the value at import and this would fail."""
    monkeypatch.setenv(guest_budget.SESSIONS_ENV, "7")
    assert guest_budget.limits_for(_guest()).sessions == 7
    monkeypatch.setenv(guest_budget.SESSIONS_ENV, "9")
    assert guest_budget.limits_for(_guest()).sessions == 9


@pytest.mark.parametrize("bad", ["0", "-1", "", "lots", "40.5", " "])
def test_unusable_env_leaves_the_stored_cap(monkeypatch, bad):
    monkeypatch.setenv(guest_budget.SESSIONS_ENV, bad)
    monkeypatch.setenv(guest_budget.TURNS_ENV, bad)
    limits = guest_budget.limits_for(_guest())
    assert (limits.sessions, limits.turns_per_day) == (2, 40), (
        f"env {bad!r} must be ignored, never applied — 0 and negatives read as "
        "'refuse every turn' downstream"
    )


def test_env_never_lowers_a_raised_per_user_cap(monkeypatch):
    """The captain can still lift one guest via their row with env deployed."""
    monkeypatch.setenv(guest_budget.SESSIONS_ENV, "3")
    monkeypatch.setenv(guest_budget.TURNS_ENV, "50")
    limits = guest_budget.limits_for(_guest(sessions=100, turns=1000))
    assert (limits.sessions, limits.turns_per_day) == (100, 1000)
