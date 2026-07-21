# test_websandbox_webcontainer.py — tests for the WebContainers boot-credential
# broker (RR-4, feat/webcontainer-credentials).
#
# Created 2026-07-21. The point of the broker is that the vendor key lives ONLY
# in server config and never in the frontend bundle. These tests pin that
# contract, and one thing beyond it that BrowserPod's equivalent has no reason
# to check: that this runtime is reachable THROUGH THE GENERIC DISPATCHER by its
# id, because that dispatcher is the only path the client actually calls.
#
#   * a configured deploy issues the key to an authenticated, workspace-scoped
#     caller (available=True)
#   * an UNCONFIGURED deploy answers available=False with a null key instead of
#     raising — for this runtime that means "no licensed key", NOT "cannot run",
#     and the client decides whether its origin may boot keyless
#   * whitespace-only / empty config counts as unconfigured
#   * the key is read at CALL time, so rotating server config takes effect
#     without redeploying the client
#   * ``runtimes.get_runtime_credentials("webcontainer", …)`` reaches this broker
#
# No network, no Daytona, no DB — the broker is pure config resolution.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.websandbox import runtimes, webcontainer

_ENV = "WEBCONTAINER_API_KEY"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an unconfigured deploy.

    Both sources must be neutralized. Clearing only the environment variable is
    not enough: the broker falls back to a ``.env`` file, and on a developer
    machine that file may hold a real key — so "unconfigured" tests would
    silently become "configured" ones and stop testing the fallback at all.
    """
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(webcontainer, "_dotenv_key", lambda: "")


async def test_issues_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "wc_api_key_123")

    result = await webcontainer.get_credentials("ws-1", "user-1")

    assert result.available is True
    assert result.apiKey == "wc_api_key_123"


async def test_reports_unavailable_when_unconfigured() -> None:
    # For WebContainers this specifically means "this deploy holds no licensed
    # key". The client still boots keyless on a permitted origin (localhost), so
    # this answer is a routing input, not a verdict on the runtime.
    result = await webcontainer.get_credentials("ws-1", "user-1")

    assert result.available is False
    assert result.apiKey is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_blank_config_counts_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # Handing a blank string to the client would fail inside StackBlitz's SDK
    # after the boot had already started, which is strictly worse than reporting
    # unavailable and letting the client choose keyless-or-Daytona up front.
    monkeypatch.setenv(_ENV, blank)

    result = await webcontainer.get_credentials("ws-1", "user-1")

    assert result.available is False
    assert result.apiKey is None


async def test_key_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "  wc_api_key_123\n")

    result = await webcontainer.get_credentials("ws-1", "user-1")

    assert result.apiKey == "wc_api_key_123"


async def test_falls_back_to_dotenv_when_env_var_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The var carries no POCKETPAW_ prefix, so nothing loads it automatically;
    # whether it reaches os.environ depends on the entrypoint. This fallback is
    # what stops a correctly-configured .env from looking like an outage.
    monkeypatch.setattr(webcontainer, "_dotenv_key", lambda: "wc_from_dotenv")

    result = await webcontainer.get_credentials("ws-1", "user-1")

    assert result.available is True
    assert result.apiKey == "wc_from_dotenv"


async def test_env_var_wins_over_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real deploy exports the key; a stray .env on the same host must not
    # shadow it.
    monkeypatch.setenv(_ENV, "wc_from_env")
    monkeypatch.setattr(webcontainer, "_dotenv_key", lambda: "wc_from_dotenv")

    result = await webcontainer.get_credentials("ws-1", "user-1")

    assert result.apiKey == "wc_from_env"


async def test_rotation_takes_effect_without_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "wc_old")
    first = await webcontainer.get_credentials("ws-1", "user-1")

    monkeypatch.setenv(_ENV, "wc_new")
    second = await webcontainer.get_credentials("ws-1", "user-1")

    assert first.apiKey == "wc_old"
    assert second.apiKey == "wc_new"


def test_enabled_flag_tracks_config(monkeypatch: pytest.MonkeyPatch) -> None:
    assert webcontainer.webcontainer_enabled() is False
    monkeypatch.setenv(_ENV, "wc_api_key_123")
    assert webcontainer.webcontainer_enabled() is True


async def test_reachable_through_the_generic_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client calls ``/runtimes/{id}/credentials``, never this module directly.

    Registering the broker is a one-line dict entry, and a missing entry does not
    fail loudly — the dispatcher answers ``available: false`` for an unknown id
    on purpose. So an unregistered runtime is INDISTINGUISHABLE from an
    unconfigured one at the wire, and the only thing that can tell them apart is
    a test that goes through the dispatcher with a key configured.
    """
    monkeypatch.setenv(_ENV, "wc_api_key_123")

    result = await runtimes.get_runtime_credentials("webcontainer", "ws-1", "user-1")

    assert result.available is True
    assert result.apiKey == "wc_api_key_123"
