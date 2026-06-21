# tests/cloud/test_discovered_rule_enforcement.py
# Created: 2026-06-21 (feat/szd-finish-enforce, F6) — pins the LIVE enforcement
# of approved workspace-discovered Instinct rules at the gate, behind the
# default-OFF ``instinct_enforce_discovered_rules`` flag.
#
# What this pins (per the F6 design in docs/design/szd-finish-build-plan.md §2):
#   * Backward-compat (iron law): flag OFF → ``get_active_rules`` is NEVER
#     called and the verdict is byte-identical to the template-only path.
#   * Behavior change: a discovered ``block`` / ``require_approval`` / ``notify``
#     rule that matches the row changes the verdict exactly as a template rule
#     of the same action would.
#   * Inertness: a discovered rule whose ``when`` is false, or scoped to a
#     different pocket, does not fire.
#   * Precedence: a discovered ``block`` beats a template ``execute`` (discovered
#     rules merge FIRST so the step-1 first-match short-circuit picks them).
#   * Fail-safe (security-critical): a ``get_active_rules`` read failure falls
#     through to the template path (fail-OPEN, no 404); a discovered rule whose
#     CEL ``when`` errors is DROPPED (never blocks, never 404s); a discovered
#     rule that fails to parse is dropped while its siblings survive.
#
# Harness: targets ``instinct_dispatch.gate_action`` directly with a stubbed
# ``get_active_rules`` (monkeypatched on the dispatch module) returning canned
# ``RuleResponse``-shaped wire dicts, and the REAL ``resolve_instinct`` composer.
# The flag is flipped by monkeypatching ``instinct_dispatch.get_settings`` to
# return a settings object with ``instinct_enforce_discovered_rules`` set.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import instinct_dispatch

from pocketpaw.bundled_templates import PocketTemplate
from pocketpaw.config import get_settings

pytestmark = pytest.mark.usefixtures("mongo_db")

FROZEN_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures — minimal valid v2 PocketTemplate + canned discovered-rule wire dicts
# ---------------------------------------------------------------------------


def _template(
    *,
    instinct_policy: str = "auto",
    rules: list[dict] | None = None,
    action_name: str = "do_thing",
) -> PocketTemplate:
    """A data-grid template with one configurable action and a ``value`` column
    so the row's ``value`` identifier resolves through the default resolver."""
    raw: dict = {
        "schema_version": "2",
        "name": "test-template",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "test fixture",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [{"field": "value", "widget": "number"}],
        },
        "actions": [
            {
                "name": action_name,
                "label": "Do Thing",
                "kind": "single-row",
                "instinct_policy": instinct_policy,
            }
        ],
    }
    if rules is not None:
        raw["instinct_rules"] = {"rules": rules}
    return PocketTemplate.model_validate(raw)


def _discovered(
    *,
    when: str,
    action: str,
    pocket_id: str | None = None,
    rule_id: str = "rule-1",
    name: str = "discovered rule",
) -> dict:
    """A ``RuleResponse``-shaped wire dict, as ``get_active_rules`` returns.

    Only ``when`` / ``action`` / ``scope`` matter to the gate; the rest is
    populated so the shape is realistic.
    """
    return {
        "id": rule_id,
        "workspace_id": "w1",
        "owner_user_id": "u1",
        "name": name,
        "description": None,
        "when": when,
        "action": action,
        "status": "active",
        "scope": {"workspace_id": "w1", "pocket_id": pocket_id, "object_type": None},
        "confidence": 0.9,
        "provenance": ["discovery"],
        "created_at": None,
        "updated_at": None,
    }


def _enable_flag(monkeypatch, *, enabled: bool = True) -> None:
    """Flip ``instinct_enforce_discovered_rules`` for the duration of a test by
    monkeypatching the module-local ``get_settings`` the gate calls."""
    settings = get_settings()
    object.__setattr__(settings, "instinct_enforce_discovered_rules", enabled)
    monkeypatch.setattr(instinct_dispatch, "get_settings", lambda: settings)


def _stub_active_rules(monkeypatch, rows: list[dict]) -> None:
    """Stub ``get_active_rules`` on the dispatch module to return canned rows."""

    async def _fake(workspace_id: str) -> list[dict]:  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(instinct_dispatch, "get_active_rules", _fake)


def _stub_active_rules_raises(monkeypatch, exc: Exception) -> None:
    """Stub ``get_active_rules`` to raise — the store-read-failure case."""

    async def _raise(workspace_id: str) -> list[dict]:  # noqa: ARG001
        raise exc

    monkeypatch.setattr(instinct_dispatch, "get_active_rules", _raise)


async def _gate(
    template: PocketTemplate,
    *,
    row_context: dict[str, Any],
    pocket_id: str = "p1",
    workspace_context: dict[str, Any] | None = None,
):
    return await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="do_thing",
        row_context=row_context,
        workspace_context=workspace_context,
        now=FROZEN_NOW,
    )


# ===========================================================================
# Backward-compat — the iron law: flag OFF is byte-identical to template-only.
# ===========================================================================


async def test_flag_off_does_not_call_get_active_rules(monkeypatch) -> None:
    """Default flag (OFF) → ``get_active_rules`` is NEVER called, and the
    verdict is byte-identical to the no-discovered-rules template path."""
    called = {"n": 0}

    async def _boom(workspace_id: str) -> list[dict]:  # noqa: ARG001
        called["n"] += 1
        raise AssertionError("get_active_rules must NOT be called when the flag is off")

    monkeypatch.setattr(instinct_dispatch, "get_active_rules", _boom)
    # Flag stays at its real default (False) — no _enable_flag call.

    template = _template(instinct_policy="auto")
    result = await _gate(template, row_context={"value": 1})

    assert called["n"] == 0
    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"


# ===========================================================================
# Behavior change — a matching discovered rule changes the verdict.
# ===========================================================================


async def test_discovered_block_rule_blocks_matching_action(monkeypatch) -> None:
    """A discovered ``block`` rule whose ``when`` matches → BLOCK / blocked."""
    _enable_flag(monkeypatch)
    _stub_active_rules(monkeypatch, [_discovered(when="value > 100", action="block")])

    template = _template(instinct_policy="auto")  # template alone would EXECUTE
    result = await _gate(template, row_context={"value": 999})

    assert result.next_step == "blocked"
    assert result.decision.verdict == "BLOCK"
    assert any(r.when == "value > 100" for r in result.decision.matched_rules)


async def test_discovered_require_approval_escalates(monkeypatch) -> None:
    """A discovered ``require_approval`` rule that matches → ESCALATE_APPROVAL /
    pending_approval under the dormant ASK default."""
    _enable_flag(monkeypatch)
    _stub_active_rules(monkeypatch, [_discovered(when="value > 100", action="require_approval")])

    template = _template(instinct_policy="auto")
    result = await _gate(template, row_context={"value": 200})

    assert result.next_step == "pending_approval"
    assert result.decision.verdict == "ESCALATE_APPROVAL"
    assert result.approval_id is not None


async def test_discovered_notify_rule_rides_proceed(monkeypatch) -> None:
    """A discovered ``notify`` rule that matches → verdict stays proceed, the
    rule rides ``notify_rules``."""
    _enable_flag(monkeypatch)
    _stub_active_rules(monkeypatch, [_discovered(when="value > 100", action="notify")])

    template = _template(instinct_policy="auto")
    result = await _gate(template, row_context={"value": 999})

    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"
    assert any(r.when == "value > 100" for r in result.notify_rules)


# ===========================================================================
# Inertness — a non-matching / out-of-scope discovered rule does not fire.
# ===========================================================================


async def test_discovered_rule_inert_when_when_false(monkeypatch) -> None:
    """A discovered ``block`` rule whose ``when`` evaluates False against the
    row is inert — the action proceeds."""
    _enable_flag(monkeypatch)
    _stub_active_rules(monkeypatch, [_discovered(when="value > 100", action="block")])

    template = _template(instinct_policy="auto")
    result = await _gate(template, row_context={"value": 5})  # 5 > 100 is False

    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"


async def test_discovered_rule_scoped_to_other_pocket_is_inert(monkeypatch) -> None:
    """A discovered rule scoped to a DIFFERENT pocket is filtered out before
    conversion — it never fires here."""
    _enable_flag(monkeypatch)
    _stub_active_rules(
        monkeypatch,
        [_discovered(when="value > 100", action="block", pocket_id="other-pocket")],
    )

    template = _template(instinct_policy="auto")
    # Current pocket is p1; the rule is scoped to "other-pocket".
    result = await _gate(template, row_context={"value": 999}, pocket_id="p1")

    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"


# ===========================================================================
# Precedence — discovered block wins the first-match short-circuit.
# ===========================================================================


async def test_discovered_block_beats_template_execute(monkeypatch) -> None:
    """Discovered rules merge FIRST, so a discovered ``block`` short-circuits
    even when the template alone would EXECUTE."""
    _enable_flag(monkeypatch)
    _stub_active_rules(monkeypatch, [_discovered(when="value > 0", action="block")])

    template = _template(instinct_policy="auto")  # no template rules → EXECUTE
    result = await _gate(template, row_context={"value": 1})

    assert result.next_step == "blocked"
    assert result.decision.verdict == "BLOCK"


# ===========================================================================
# Fail-safe (security-critical).
# ===========================================================================


async def test_get_active_rules_read_failure_falls_through_to_template(monkeypatch, caplog) -> None:
    """A ``get_active_rules`` read failure must fail OPEN: the action proceeds on
    the template-only verdict, a WARNING is logged, and there is NO 404."""
    _enable_flag(monkeypatch)
    _stub_active_rules_raises(monkeypatch, RuntimeError("mongo is down"))

    template = _template(instinct_policy="auto")
    with caplog.at_level("WARNING"):
        result = await _gate(template, row_context={"value": 1})

    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"
    assert any("discovered" in r.message.lower() for r in caplog.records)


async def test_discovered_rule_cel_eval_error_is_dropped_not_blocking(monkeypatch, caplog) -> None:
    """A discovered ``block`` rule whose ``when`` references an identifier that
    is absent from the row context errors on eval. It must be DROPPED — never a
    block, never a 404 — and the action proceeds on the template verdict."""
    _enable_flag(monkeypatch)
    # ``missing_field`` is not a declared column and not in row_context → the
    # resolver raises → CelEvaluationError on the guarded probe.
    _stub_active_rules(monkeypatch, [_discovered(when="missing_field > 100", action="block")])

    template = _template(instinct_policy="auto")
    with caplog.at_level("WARNING"):
        result = await _gate(template, row_context={"value": 999})

    # NOT blocked, NOT 404 — the broken discovered rule is inert.
    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"
    assert any("discovered" in r.message.lower() for r in caplog.records)


async def test_discovered_rule_parse_failure_dropped_others_survive(monkeypatch, caplog) -> None:
    """A batch with one malformed discovered rule (bad CEL syntax that fails to
    parse at conversion) drops the bad one and keeps the good one firing."""
    _enable_flag(monkeypatch)
    _stub_active_rules(
        monkeypatch,
        [
            # Unparseable CEL — fails InstinctRule.model_validate / probe.
            _discovered(when="value >>> 100", action="block", rule_id="bad"),
            # Valid block rule that matches.
            _discovered(when="value > 0", action="block", rule_id="good"),
        ],
    )

    template = _template(instinct_policy="auto")
    with caplog.at_level("WARNING"):
        result = await _gate(template, row_context={"value": 1})

    # The good rule still fires → blocked.
    assert result.next_step == "blocked"
    assert result.decision.verdict == "BLOCK"
    # The bad one was dropped with a warning.
    assert any("discovered" in r.message.lower() for r in caplog.records)
