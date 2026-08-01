# SenseResolver tests — resolve disambiguation, preference, read-first gate, delegation.
# Created: 2026-06-08 — Sense tier chunk 2. Exercises the real Beanie layer
# (mongo_db fixture seeds enabled connectors + preferences through the actual
# service / preference paths) and mocks connectors_service.execute so we can
# assert the read-first gate never calls execute on a non-auto action, and that
# an auto action delegates with the resolved connector. Real connectors from
# repo /connectors back the registry: gmail (paw.email.v1, single provider),
# github+gitlab (paw.code.v1, ambiguous).
# Updated: 2026-06-08 (sense-tier efficiency fix) — coverage for resolve_many:
# all-resolve, partial-None, ambiguity preserved, and a query-count assertion
# that the enabled-connector READ runs exactly ONCE for N senses (spying on
# ConnectorSenseFiller.enabled_connector_names).
# Updated: 2026-08-02 (Sense Phase 2, SP2-2) — coverage for the AGENT tier:
# an ``AgentSenseContext`` pref beats both the workspace and the pocket
# preference row; a pref for another sense doesn't leak; a pref naming a
# NON-candidate is skipped (one INFO log) and falls through to the stored
# preference (or to the ambiguous deterministic pick when there is none); a pref
# can't conjure a provider where none is enabled; ``agent=None`` and the omitted
# keyword resolve identically to the legacy path; ``resolve_many`` and
# ``execute_sense`` honour the pref (execute runs against the PREFERRED
# connector); and the carried ``senses`` mount list does NOT gate yet (SP2-3).

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import (
    EnableConnectorRequest,
    ExecuteActionResponse,
)
from pocketpaw_ee.cloud.senses import filler as filler_mod
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
# resolve_many — batch resolution with ONE enabled-connector read
# ---------------------------------------------------------------------------


async def test_resolve_many_all_resolve() -> None:
    await _enable("gmail")
    await _enable("github")
    out = await resolver.resolve_many(["paw.email.v1", "paw.code.v1"], WS)
    assert set(out) == {"paw.email.v1", "paw.code.v1"}
    assert out["paw.email.v1"].connector_name == "gmail"
    # github only (gitlab not enabled) -> single, unambiguous.
    assert out["paw.code.v1"].connector_name == "github"
    assert out["paw.code.v1"].ambiguous is False


async def test_resolve_many_partial_none() -> None:
    # Only email is wired; payments has no provider for this workspace.
    await _enable("gmail")
    out = await resolver.resolve_many(["paw.email.v1", "paw.payments.v1"], WS)
    assert out["paw.email.v1"] is not None
    assert out["paw.email.v1"].connector_name == "gmail"
    assert out["paw.payments.v1"] is None


async def test_resolve_many_preserves_ambiguity() -> None:
    await _enable("github")
    await _enable("gitlab")
    out = await resolver.resolve_many(["paw.code.v1"], WS)
    res = out["paw.code.v1"]
    assert res is not None
    assert res.candidates == ["github", "gitlab"]
    assert res.ambiguous is True
    assert res.connector_name == "github"  # sorted-first deterministic pick


async def test_resolve_many_matches_resolve_per_sense() -> None:
    """resolve_many produces the SAME result as calling resolve() per id."""
    await _enable("gmail")
    await _enable("github")
    ids = ["paw.email.v1", "paw.code.v1", "paw.payments.v1"]
    batch = await resolver.resolve_many(ids, WS)
    for sid in ids:
        single = await resolver.resolve(sid, WS)
        assert batch[sid] == single


async def test_resolve_many_reads_enabled_connectors_once(monkeypatch) -> None:
    """The enabled-connector query runs EXACTLY ONCE for N senses — the whole
    point of the batch path (was one query per sense)."""
    await _enable("gmail")
    await _enable("github")

    real = filler_mod.ConnectorSenseFiller.enabled_connector_names

    # Count calls to the enabled-connector read, delegating to the real impl.
    calls = {"n": 0}

    async def _counting(self, workspace_id, *, pocket_id=None):
        calls["n"] += 1
        return await real(self, workspace_id, pocket_id=pocket_id)

    monkeypatch.setattr(filler_mod.ConnectorSenseFiller, "enabled_connector_names", _counting)

    out = await resolver.resolve_many(["paw.email.v1", "paw.code.v1", "paw.payments.v1"], WS)
    assert set(out) == {"paw.email.v1", "paw.code.v1", "paw.payments.v1"}
    # ONE read for THREE senses.
    assert calls["n"] == 1


async def test_resolve_many_validates_ids() -> None:
    with pytest.raises(SenseValidationError):
        await resolver.resolve_many(["paw.email.v1", "paw.telepathy.v1"], WS)


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


# ---------------------------------------------------------------------------
# Agent tier (SP2-2) — the running agent's own pref outranks the stored rows
# ---------------------------------------------------------------------------


def _agent(prefs: dict[str, str], agent_id: str = "ag-1") -> resolver.AgentSenseContext:
    return resolver.AgentSenseContext(agent_id=agent_id, prefs=prefs)


async def test_agent_pref_beats_workspace_preference_row() -> None:
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "github")

    result = await resolver.resolve("paw.code.v1", WS, agent=_agent({"paw.code.v1": "gitlab"}))

    assert result is not None
    assert result.connector_name == "gitlab"
    assert result.ambiguous is False
    assert result.candidates == ["github", "gitlab"]


async def test_agent_pref_beats_pocket_preference_row() -> None:
    # The pocket row is the narrowest stored scope — the agent still outranks it.
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "github", pocket_id="pk1")

    result = await resolver.resolve(
        "paw.code.v1", WS, pocket_id="pk1", agent=_agent({"paw.code.v1": "gitlab"})
    )

    assert result is not None
    assert result.connector_name == "gitlab"
    assert result.ambiguous is False


async def test_agent_pref_for_other_sense_does_not_leak() -> None:
    # A pref keyed to a DIFFERENT sense must not steer this one.
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "github")

    result = await resolver.resolve("paw.code.v1", WS, agent=_agent({"paw.email.v1": "gitlab"}))

    assert result is not None
    assert result.connector_name == "github"  # the stored row still decides


async def test_agent_pref_not_a_candidate_falls_through_to_stored_preference(caplog) -> None:
    # A pref naming a provider that isn't a candidate (never enabled, disabled,
    # typo) is SKIPPED — one log line, then the stored preference decides.
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "gitlab")

    with caplog.at_level("INFO", logger="pocketpaw_ee.cloud.senses.resolver"):
        result = await resolver.resolve(
            "paw.code.v1", WS, agent=_agent({"paw.code.v1": "bitbucket"})
        )

    assert result is not None
    assert result.connector_name == "gitlab"  # stored preference, not the agent's
    assert result.ambiguous is False
    skips = [r for r in caplog.records if "agent sense pref skipped" in r.message]
    assert len(skips) == 1


async def test_agent_pref_not_a_candidate_and_no_stored_pref_is_ambiguous() -> None:
    await _enable("github")
    await _enable("gitlab")

    result = await resolver.resolve("paw.code.v1", WS, agent=_agent({"paw.code.v1": "bitbucket"}))

    assert result is not None
    assert result.connector_name == "github"  # deterministic sorted-first
    assert result.ambiguous is True


async def test_agent_pref_with_single_candidate_is_a_no_op() -> None:
    # One candidate: preferring it changes nothing, and preferring something
    # else must not strand the sense.
    await _enable("gmail")

    preferred = await resolver.resolve("paw.email.v1", WS, agent=_agent({"paw.email.v1": "gmail"}))
    assert preferred is not None
    assert preferred.connector_name == "gmail"
    assert preferred.ambiguous is False

    bogus = await resolver.resolve("paw.email.v1", WS, agent=_agent({"paw.email.v1": "outlook"}))
    assert bogus is not None
    assert bogus.connector_name == "gmail"


async def test_agent_pref_cannot_conjure_a_provider() -> None:
    # No enabled connector fills the sense — an agent pref does NOT create one.
    result = await resolver.resolve("paw.code.v1", WS, agent=_agent({"paw.code.v1": "gitlab"}))
    assert result is None


async def test_agent_none_matches_legacy_resolution() -> None:
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "github")

    explicit_none = await resolver.resolve("paw.code.v1", WS, agent=None)
    omitted = await resolver.resolve("paw.code.v1", WS)

    assert explicit_none == omitted
    assert omitted is not None
    assert omitted.connector_name == "github"


async def test_resolve_many_honours_agent_prefs() -> None:
    await _enable("gmail")
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "github")

    out = await resolver.resolve_many(
        ["paw.email.v1", "paw.code.v1"], WS, agent=_agent({"paw.code.v1": "gitlab"})
    )

    assert out["paw.code.v1"].connector_name == "gitlab"
    assert out["paw.code.v1"].ambiguous is False
    # The unpreferred sense resolves exactly as before.
    assert out["paw.email.v1"].connector_name == "gmail"


async def test_resolve_many_matches_resolve_per_sense_with_agent() -> None:
    await _enable("github")
    await _enable("gitlab")
    agent = _agent({"paw.code.v1": "gitlab"})

    batch = await resolver.resolve_many(["paw.code.v1"], WS, agent=agent)
    single = await resolver.resolve("paw.code.v1", WS, agent=agent)

    assert batch["paw.code.v1"] == single


async def test_execute_sense_runs_against_the_agent_preferred_connector(monkeypatch) -> None:
    """End-to-end: the agent's pref decides which connector actually executes."""
    await _enable("github")
    await _enable("gitlab")
    await preference.set_preference(WS, "paw.code.v1", "github")
    spy = AsyncMock(return_value=ExecuteActionResponse(success=True, data=[]))
    monkeypatch.setattr(connectors_service, "execute", spy)

    # list_issues is trust_level=auto on BOTH providers, so the assertion is
    # about WHICH connector ran, not about the action existing on one of them.
    result = await resolver.execute_sense(
        "paw.code.v1", "list_issues", {}, WS, agent=_agent({"paw.code.v1": "gitlab"})
    )

    assert result.ok is True
    assert result.connector_name == "gitlab"
    spy.assert_awaited_once()
    assert spy.await_args.args[1] == "gitlab"


async def test_agent_context_carries_but_does_not_gate_on_senses() -> None:
    """SP2-2 CARRIES the mount list; gating on it is SP2-3. A sense that is NOT
    in the agent's ``senses`` still resolves today."""
    await _enable("gmail")
    ctx = resolver.AgentSenseContext(agent_id="ag-1", senses=("paw.code.v1",), prefs={})

    result = await resolver.resolve("paw.email.v1", WS, agent=ctx)

    assert result is not None
    assert result.connector_name == "gmail"
