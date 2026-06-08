# SenseResolver tests — resolve disambiguation, preference, read-first gate, delegation.
# Created: 2026-06-08 — Sense tier chunk 2. Exercises the real Beanie layer
# (mongo_db fixture seeds enabled connectors + preferences through the actual
# service / preference paths) and mocks connectors_service.execute so we can
# assert the read-first gate never calls execute on a non-auto action, and that
# an auto action delegates with the resolved connector. Real connectors from
# repo /connectors back the registry: gmail (paw.email.v1, single provider),
# github+gitlab (paw.code.v1, ambiguous).

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import (
    EnableConnectorRequest,
    ExecuteActionResponse,
)
from pocketpaw_ee.cloud.senses import preference, resolver

from pocketpaw.senses import SenseValidationError

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "ws-sense-test"


async def _enable(name: str) -> None:
    await connectors_service.enable_connector(WS, name, EnableConnectorRequest(scope="workspace"))


# ---------------------------------------------------------------------------
# resolve — 0 / 1 / >1 providers
# ---------------------------------------------------------------------------


async def test_resolve_zero_providers_returns_none() -> None:
    # paw.email.v1 is fillable by gmail, but nothing is enabled for this ws.
    result = await resolver.resolve("paw.email.v1", WS)
    assert result is None


async def test_resolve_single_provider() -> None:
    await _enable("gmail")
    result = await resolver.resolve("paw.email.v1", WS)
    assert result is not None
    assert result.connector_name == "gmail"
    assert result.ambiguous is False
    assert result.candidates == ["gmail"]


async def test_resolve_ignores_disabled_connector() -> None:
    await _enable("gmail")
    await connectors_service.disable_connector(WS, "gmail")
    result = await resolver.resolve("paw.email.v1", WS)
    assert result is None


async def test_resolve_ambiguous_flag_when_no_preference() -> None:
    # paw.code.v1 -> github + gitlab. No preference -> deterministic first +
    # ambiguous flag set so the caller can ask the user to choose.
    await _enable("github")
    await _enable("gitlab")
    result = await resolver.resolve("paw.code.v1", WS)
    assert result is not None
    assert result.candidates == ["github", "gitlab"]
    assert result.ambiguous is True
    assert result.connector_name == "github"  # sorted-first deterministic pick


async def test_resolve_preference_wins() -> None:
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "gitlab")
    result = await resolver.resolve("paw.code.v1", WS)
    assert result is not None
    assert result.connector_name == "gitlab"
    assert result.ambiguous is False
    assert result.candidates == ["github", "gitlab"]


async def test_resolve_preference_not_a_candidate_falls_back_to_first() -> None:
    # A stale preference (provider no longer enabled) is ignored — we fall back
    # to the deterministic pick and re-flag ambiguous.
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "bitbucket")  # not enabled / not a candidate
    result = await resolver.resolve("paw.code.v1", WS)
    assert result is not None
    assert result.connector_name == "github"
    assert result.ambiguous is True


async def test_resolve_unknown_paw_sense_raises() -> None:
    with pytest.raises(SenseValidationError):
        await resolver.resolve("paw.telepathy.v1", WS)


# ---------------------------------------------------------------------------
# preference store get/set + pocket override
# ---------------------------------------------------------------------------


async def test_preference_get_set_roundtrip() -> None:
    assert await preference.get_preference(WS, "paw.code.v1") is None
    await preference.set_preference(WS, "paw.code.v1", "github")
    assert await preference.get_preference(WS, "paw.code.v1") == "github"
    # idempotent update, not a duplicate insert
    await preference.set_preference(WS, "paw.code.v1", "gitlab")
    assert await preference.get_preference(WS, "paw.code.v1") == "gitlab"


async def test_preference_pocket_overrides_workspace() -> None:
    await preference.set_preference(WS, "paw.code.v1", "github")
    await preference.set_preference(WS, "paw.code.v1", "gitlab", pocket_id="pk1")
    # pocket-scoped pref wins for that pocket
    assert await preference.get_preference(WS, "paw.code.v1", pocket_id="pk1") == "gitlab"
    # other pockets fall back to the workspace default
    assert await preference.get_preference(WS, "paw.code.v1", pocket_id="pk2") == "github"
    # workspace level untouched
    assert await preference.get_preference(WS, "paw.code.v1") == "github"


# ---------------------------------------------------------------------------
# execute_sense — read-first gate + delegation
# ---------------------------------------------------------------------------


async def test_execute_sense_no_provider_is_structured_error() -> None:
    result = await resolver.execute_sense("paw.email.v1", "gmail_search", {}, WS)
    assert result.ok is False
    assert result.error == "sense.no_provider"
    assert result.connector_name is None


async def test_execute_sense_blocks_confirm_action_never_calls_execute(monkeypatch) -> None:
    await _enable("gmail")
    spy = AsyncMock(return_value=ExecuteActionResponse(success=True))
    monkeypatch.setattr(connectors_service, "execute", spy)

    # gmail_send is trust_level=confirm -> must be blocked, execute never called.
    result = await resolver.execute_sense("paw.email.v1", "gmail_send", {"to": "x@y.com"}, WS)

    assert result.ok is False
    assert result.error == "sense.action_needs_approval"
    assert result.connector_name == "gmail"
    spy.assert_not_called()


async def test_execute_sense_blocks_unknown_action(monkeypatch) -> None:
    await _enable("gmail")
    spy = AsyncMock(return_value=ExecuteActionResponse(success=True))
    monkeypatch.setattr(connectors_service, "execute", spy)

    # An action with no trust_level in the def -> treated as not-auto -> blocked.
    result = await resolver.execute_sense("paw.email.v1", "gmail_nonexistent", {}, WS)

    assert result.ok is False
    assert result.error == "sense.action_needs_approval"
    spy.assert_not_called()


async def test_execute_sense_delegates_auto_action(monkeypatch) -> None:
    await _enable("gmail")
    spy = AsyncMock(
        return_value=ExecuteActionResponse(success=True, data={"messages": []}),
    )
    monkeypatch.setattr(connectors_service, "execute", spy)

    # gmail_search is trust_level=auto -> proceeds and delegates.
    result = await resolver.execute_sense(
        "paw.email.v1", "gmail_search", {"q": "is:unread"}, WS, user_id="u1"
    )

    assert result.ok is True
    assert result.connector_name == "gmail"
    assert result.data.success is True
    spy.assert_awaited_once()
    # delegated with the RESOLVED connector + right action/params.
    call = spy.await_args
    assert call.args[0] == WS
    assert call.args[1] == "gmail"  # resolved connector name
    req = call.args[2]
    assert req.action == "gmail_search"
    assert req.params == {"q": "is:unread"}
    assert call.kwargs.get("user_id") == "u1"


async def test_execute_sense_delegates_with_preferred_provider(monkeypatch) -> None:
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "github")
    spy = AsyncMock(return_value=ExecuteActionResponse(success=True, data=[]))
    monkeypatch.setattr(connectors_service, "execute", spy)

    # list_repos is trust_level=auto on github.
    result = await resolver.execute_sense("paw.code.v1", "list_repos", {}, WS)

    assert result.ok is True
    assert result.connector_name == "github"
    spy.assert_awaited_once()
    assert spy.await_args.args[1] == "github"
