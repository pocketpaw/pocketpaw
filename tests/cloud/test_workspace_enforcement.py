# test_workspace_enforcement.py — per-workspace authored-rule enforcement toggle.
# Created: 2026-07-09 (feat/instinct-guardrail-rules) — pins the PER-WORKSPACE
# resolution of Instinct enforcement at the live gate:
#   * A workspace override set True enforces authored rules even when the GLOBAL
#     ``instinct_enforce_discovered_rules`` flag is OFF.
#   * With NO override, the workspace inherits the global flag (OFF → inert,
#     ON → enforced) — the pre-existing default is preserved.
#   * A workspace override set False turns enforcement OFF even when the global
#     flag is ON.
#   * CRITICAL fail-OPEN: an enforcement-config read error yields NO enforcement
#     (the gate proceeds on the template floor, a WARNING is logged, no raise) —
#     a DB hiccup never blocks the gate.
#
# Harness mirrors test_discovered_rule_enforcement.py: it targets
# ``instinct_dispatch.gate_action`` with a stubbed ``get_active_rules`` and the
# REAL ``resolve_instinct`` composer. The per-workspace override is written
# through the real ``rules.service.set_enforcement`` against the mongomock
# ``mongo_db`` fixture; the global flag is flipped by monkeypatching
# ``instinct_dispatch.get_settings``.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import instinct_dispatch
from pocketpaw_ee.cloud.rules import service as rules_service

from pocketpaw.bundled_templates import PocketTemplate
from pocketpaw.config import get_settings

pytestmark = pytest.mark.usefixtures("mongo_db")

FROZEN_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures (minimal, copied from test_discovered_rule_enforcement.py)
# ---------------------------------------------------------------------------


def _template(action_name: str = "do_thing") -> PocketTemplate:
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
                "instinct_policy": "auto",
            }
        ],
    }
    return PocketTemplate.model_validate(raw)


def _block_rule() -> dict:
    return {
        "id": "rule-1",
        "workspace_id": "w1",
        "owner_user_id": "u1",
        "name": "discovered block",
        "description": None,
        "when": "value > 100",
        "action": "block",
        "status": "active",
        "scope": {"workspace_id": "w1", "pocket_id": None, "object_type": None},
        "confidence": 0.9,
        "provenance": ["discovery"],
        "created_at": None,
        "updated_at": None,
    }


def _set_global(monkeypatch, *, enabled: bool) -> None:
    """Flip the GLOBAL ``instinct_enforce_discovered_rules`` flag the gate reads."""
    settings = get_settings()
    object.__setattr__(settings, "instinct_enforce_discovered_rules", enabled)
    monkeypatch.setattr(instinct_dispatch, "get_settings", lambda: settings)


def _stub_active_rules(monkeypatch, rows: list[dict]) -> None:
    async def _fake(workspace_id: str) -> list[dict]:  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(instinct_dispatch, "get_active_rules", _fake)


async def _gate(template: PocketTemplate, *, row_context: dict[str, Any], workspace_id: str = "w1"):
    return await instinct_dispatch.gate_action(
        workspace_id=workspace_id,
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context=row_context,
        workspace_context=None,
        now=FROZEN_NOW,
    )


# ===========================================================================
# Per-workspace override turns enforcement ON when the global flag is OFF.
# ===========================================================================


async def test_workspace_override_on_enforces_when_global_off(monkeypatch) -> None:
    _set_global(monkeypatch, enabled=False)  # global OFF
    await rules_service.set_enforcement("w1", "u1", True)  # per-workspace ON
    _stub_active_rules(monkeypatch, [_block_rule()])

    result = await _gate(_template(), row_context={"value": 999})

    assert result.next_step == "blocked"
    assert result.decision.verdict == "BLOCK"


async def test_other_workspace_not_affected_by_w1_override(monkeypatch) -> None:
    """Turning enforcement on for w1 leaves w2 (no override, global OFF) inert."""
    _set_global(monkeypatch, enabled=False)
    await rules_service.set_enforcement("w1", "u1", True)

    called = {"n": 0}

    async def _boom(workspace_id: str) -> list[dict]:  # noqa: ARG001
        called["n"] += 1
        raise AssertionError("get_active_rules must not be called for an un-enforced workspace")

    monkeypatch.setattr(instinct_dispatch, "get_active_rules", _boom)

    # w2 has no override and the global flag is OFF → no enforcement.
    result = await _gate(_template(), row_context={"value": 999}, workspace_id="w2")

    assert called["n"] == 0
    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"


# ===========================================================================
# No override → inherit the global flag (default preserved).
# ===========================================================================


async def test_no_override_global_off_is_inert(monkeypatch) -> None:
    _set_global(monkeypatch, enabled=False)

    called = {"n": 0}

    async def _boom(workspace_id: str) -> list[dict]:  # noqa: ARG001
        called["n"] += 1
        raise AssertionError("get_active_rules must not be called when enforcement is off")

    monkeypatch.setattr(instinct_dispatch, "get_active_rules", _boom)

    result = await _gate(_template(), row_context={"value": 999})

    assert called["n"] == 0
    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"


async def test_no_override_global_on_enforces(monkeypatch) -> None:
    _set_global(monkeypatch, enabled=True)  # global ON, no per-workspace override
    _stub_active_rules(monkeypatch, [_block_rule()])

    result = await _gate(_template(), row_context={"value": 999})

    assert result.next_step == "blocked"
    assert result.decision.verdict == "BLOCK"


async def test_workspace_override_off_beats_global_on(monkeypatch) -> None:
    """A workspace can opt OUT even when the global flag is ON."""
    _set_global(monkeypatch, enabled=True)
    await rules_service.set_enforcement("w1", "u1", False)  # force OFF for w1
    _stub_active_rules(monkeypatch, [_block_rule()])

    result = await _gate(_template(), row_context={"value": 999})

    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"


# ===========================================================================
# CRITICAL — fail-OPEN: a config read error never blocks the gate.
# ===========================================================================


async def test_enforcement_config_read_error_fails_open(monkeypatch, caplog) -> None:
    """If the per-workspace override read raises, enforcement resolves to OFF
    (no enforcement), the gate proceeds on the template floor, a WARNING is
    logged, and NOTHING is raised — even with the global flag ON."""
    _set_global(monkeypatch, enabled=True)

    async def _raise(workspace_id: str) -> Any:  # noqa: ARG001
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(instinct_dispatch, "get_enforcement_override", _raise)

    async def _boom(workspace_id: str) -> list[dict]:  # noqa: ARG001
        raise AssertionError("get_active_rules must not be called when the config read fails open")

    monkeypatch.setattr(instinct_dispatch, "get_active_rules", _boom)

    with caplog.at_level("WARNING"):
        result = await _gate(_template(), row_context={"value": 999})

    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"
    assert any("enforcement" in r.message.lower() for r in caplog.records)


# ===========================================================================
# Unit — the _enforcement_enabled resolver in isolation.
# ===========================================================================


async def test_enforcement_enabled_resolution_matrix(monkeypatch) -> None:
    # override True → True regardless of global
    _set_global(monkeypatch, enabled=False)
    await rules_service.set_enforcement("w1", "u1", True)
    assert await instinct_dispatch._enforcement_enabled("w1") is True

    # override False → False regardless of global
    _set_global(monkeypatch, enabled=True)
    await rules_service.set_enforcement("w1", "u1", False)
    assert await instinct_dispatch._enforcement_enabled("w1") is False

    # no override → inherit global (True here, distinct workspace)
    _set_global(monkeypatch, enabled=True)
    assert await instinct_dispatch._enforcement_enabled("w-none") is True

    _set_global(monkeypatch, enabled=False)
    assert await instinct_dispatch._enforcement_enabled("w-none") is False
