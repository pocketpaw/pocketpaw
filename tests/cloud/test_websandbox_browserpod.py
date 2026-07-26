# test_websandbox_browserpod.py — tests for the BrowserPod boot-credential
# broker (BP-1b, feat/code-mode).
#
# The point of the broker is that the vendor key lives ONLY in server config and
# never in the frontend bundle. These tests pin that contract:
#
#   * a configured deploy issues the key to an authenticated, workspace-scoped
#     caller (available=True)
#   * an UNCONFIGURED deploy answers available=False with a null key instead of
#     raising — the client must be able to fall back to Daytona cleanly
#   * whitespace-only / empty config counts as unconfigured (a blank env var must
#     never be handed out as a "key")
#   * the key is read from the environment at call time, so rotating server
#     config takes effect without a redeploy of the client
#
# No network, no Daytona, no DB — the broker is pure config resolution.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.websandbox import browserpod

_ENV = "BROWSERPOD_API_KEY"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an unconfigured deploy.

    Both sources must be neutralized. Clearing only the environment variable is
    not enough now that the broker falls back to a ``.env`` file: on a developer
    machine that file holds a real key, so "unconfigured" tests would silently
    become "configured" ones and stop testing the fallback path at all.
    """
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(browserpod, "_dotenv_key", lambda: "")


async def test_issues_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "bp_live_key_123")

    result = await browserpod.get_credentials("ws-1", "user-1")

    assert result.available is True
    assert result.apiKey == "bp_live_key_123"


async def test_reports_unavailable_when_unconfigured() -> None:
    # A missing optional runtime is a FALLBACK condition, not a failure: the
    # frontend routes the project to Daytona instead of surfacing an error.
    result = await browserpod.get_credentials("ws-1", "user-1")

    assert result.available is False
    assert result.apiKey is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_blank_config_counts_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # A deploy that sets the var to an empty string must not hand a useless
    # "key" to the client and claim the runtime is available.
    monkeypatch.setenv(_ENV, blank)

    result = await browserpod.get_credentials("ws-1", "user-1")

    assert result.available is False
    assert result.apiKey is None


async def test_key_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "  bp_live_key_123\n")

    result = await browserpod.get_credentials("ws-1", "user-1")

    assert result.apiKey == "bp_live_key_123"


async def test_falls_back_to_dotenv_when_env_var_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key in .env counts as configured.

    ``BROWSERPOD_API_KEY`` carries no ``POCKETPAW_`` prefix, so pydantic-settings
    never loads it; whether it reaches ``os.environ`` depends on the entrypoint.
    When it doesn't, the failure is invisible — the broker reports unavailable,
    the client quietly opens on Daytona, and nothing logs an error.
    """
    monkeypatch.setattr(browserpod, "_dotenv_key", lambda: "bp_from_dotenv")

    result = await browserpod.get_credentials("ws-1", "user-1")

    assert result.available is True
    assert result.apiKey == "bp_from_dotenv"


async def test_env_var_wins_over_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real deploy exports the key; a stale .env on the box must not shadow it."""
    monkeypatch.setenv(_ENV, "bp_from_env")
    monkeypatch.setattr(browserpod, "_dotenv_key", lambda: "bp_from_dotenv")

    result = await browserpod.get_credentials("ws-1", "user-1")

    assert result.apiKey == "bp_from_env"


def test_enabled_flag_tracks_config(monkeypatch: pytest.MonkeyPatch) -> None:
    assert browserpod.browserpod_enabled() is False

    monkeypatch.setenv(_ENV, "bp_live_key_123")
    # Read at call time, so rotating server config needs no client redeploy.
    assert browserpod.browserpod_enabled() is True
