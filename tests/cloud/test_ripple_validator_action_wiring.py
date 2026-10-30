# tests/cloud/test_ripple_validator_action_wiring.py
# 2026-05-23 — EE-side wiring tests for the action-verb +
# unwired-live-button gate. The pure walkers are covered in
# tests/test_ripple_manifest_action_wiring.py; this file covers the
# strict/logged variants, the agent-readable formatter, and the
# ActionWiringViolationError shape so the chat agent's retry loop can
# read it.

from __future__ import annotations

import logging

import pytest
from pocketpaw_ee.cloud.ripple_validator import (
    ActionWiringViolationError,
    format_action_violations_for_agent,
    validate_action_wiring_logged,
    validate_action_wiring_strict,
)


def _spec_with_invented_verb() -> dict:
    """The Test-D failure shape — Refresh button bound to ``action: "fetch"``."""
    return {
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "button",
                    "props": {
                        "label": "Refresh",
                        "on_click": {
                            "action": "fetch",
                            "endpoint": "/pet/1",
                            "target": "pet_rows",
                        },
                    },
                }
            ],
        }
    }


def _clean_spec() -> dict:
    return {
        "sources": {"pets": {"method": "GET", "path": "/pets", "bind": "state.pets"}},
        "ui": {
            "type": "button",
            "props": {
                "label": "Refresh",
                "on_click": {"action": "run_source", "source": "pets"},
            },
        },
    }


# ---------------------------------------------------------------------------
# Strict mode raises ActionWiringViolationError
# ---------------------------------------------------------------------------


class TestStrict:
    def test_raises_on_unknown_verb(self):
        with pytest.raises(ActionWiringViolationError) as ei:
            validate_action_wiring_strict(_spec_with_invented_verb())
        # The exception carries the structured violations list AND
        # formats a human-readable message.
        assert len(ei.value.violations) >= 1
        assert "fetch" in str(ei.value)

    def test_clean_spec_does_not_raise(self):
        validate_action_wiring_strict(_clean_spec())  # no raise

    def test_non_dict_does_not_raise(self):
        validate_action_wiring_strict(None)
        validate_action_wiring_strict("not a spec")


# ---------------------------------------------------------------------------
# Logged mode returns the list and emits structured log warnings
# ---------------------------------------------------------------------------


class TestLogged:
    def test_returns_violations_and_logs(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.WARNING, logger="pocketpaw_ee.cloud.ripple_validator")
        out = validate_action_wiring_logged(
            _spec_with_invented_verb(), pocket_id="p1", workspace_id="w1"
        )
        assert len(out) >= 1
        # Structured warning is emitted at WARNING level.
        records = [r for r in caplog.records if r.message.startswith("ripple_spec.")]
        assert records, "no structured warning emitted"
        codes = {r.message for r in records}
        assert "ripple_spec.unknown_action_verb" in codes

    def test_logs_unwired_live_button_separately(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.WARNING, logger="pocketpaw_ee.cloud.ripple_validator")
        spec = {
            "ui": {
                "type": "button",
                "props": {"label": "Refresh"},  # no on_click
            }
        }
        out = validate_action_wiring_logged(spec, pocket_id="p1", workspace_id="w1")
        assert len(out) == 1
        codes = {r.message for r in caplog.records if r.message.startswith("ripple_spec.")}
        assert "ripple_spec.unwired_live_button" in codes

    def test_clean_spec_returns_empty(self):
        assert validate_action_wiring_logged(_clean_spec()) == []

    def test_dedups_overlap_with_verb_check(self):
        # A Refresh button with an unknown verb produces ONE violation,
        # not two — the verb check fires first, and the live-button
        # walk skips paths the verb check already covered.
        out = validate_action_wiring_logged(_spec_with_invented_verb())
        assert len(out) == 1
        assert out[0].get("action") == "fetch"


# ---------------------------------------------------------------------------
# Agent-readable formatter
# ---------------------------------------------------------------------------


class TestFormatForAgent:
    def test_empty_returns_empty_string(self):
        assert format_action_violations_for_agent([]) == ""

    def test_unknown_verb_includes_path_and_suggestion(self):
        violations = [
            {
                "path": "ui.children[0].props.on_click",
                "prop": "on_click",
                "action": "fetch",
                "suggestion": "run_source",
            }
        ]
        msg = format_action_violations_for_agent(violations)
        assert "ui.children[0].props.on_click" in msg
        assert "'fetch'" in msg
        assert "run_source" in msg

    def test_unwired_button_includes_label_and_reason(self):
        violations = [
            {
                "path": "ui.children[2].props.on_click",
                "label": "Refresh",
                "reason": "live-labelled button has no on_click handler",
            }
        ]
        msg = format_action_violations_for_agent(violations)
        assert "Refresh" in msg
        assert "no on_click handler" in msg

    def test_message_carries_remediation_guidance(self):
        """The message must teach the agent the right shape, not just
        list what's broken — the retry loop is what closes the loop."""
        msg = format_action_violations_for_agent(
            [{"path": "ui", "prop": "on_click", "action": "fetch", "suggestion": None}]
        )
        # Should mention run_source or api as the right verbs and that
        # invented verbs silently no-op.
        assert "run_source" in msg or "api" in msg
        assert "no-op" in msg.lower() or "silently" in msg.lower()


# ---------------------------------------------------------------------------
# Service integration — _gate_catalog hooks the new gate
# ---------------------------------------------------------------------------


class TestGateCatalogWiring:
    """End-to-end: ``_gate_catalog`` calls the new action-wiring gate
    alongside the existing catalog gate. Strict mode raises, logged
    mode warns. We patch ``_catalog_allowed_types`` so the catalog
    walk passes (a clean type list) and the action gate is the one
    flagging."""

    @pytest.mark.asyncio
    async def test_strict_mode_raises_on_invented_verb(self, monkeypatch):
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        async def _allowed_types():
            return ["flex", "button", "text"]

        monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _allowed_types)
        with pytest.raises(ActionWiringViolationError):
            await pockets_service._gate_catalog(
                _spec_with_invented_verb(),
                strict=True,
                actor="agent",
                workspace_id="w1",
                pocket_id=None,
            )

    @pytest.mark.asyncio
    async def test_logged_mode_does_not_raise(self, monkeypatch):
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        async def _allowed_types():
            return ["flex", "button", "text"]

        monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _allowed_types)
        # No raise — logged mode swallows.
        await pockets_service._gate_catalog(
            _spec_with_invented_verb(),
            strict=False,
            actor="user",
            workspace_id="w1",
            pocket_id="p1",
        )

    @pytest.mark.asyncio
    async def test_manifest_unavailable_skips_gates(self, monkeypatch):
        """If the manifest fetch fails, both gates are skipped — best-effort
        posture matches the existing catalog gate."""
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        async def _allowed_types():
            return None  # manifest unavailable

        monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _allowed_types)
        # Even strict mode does not raise when the manifest is gone.
        await pockets_service._gate_catalog(
            _spec_with_invented_verb(),
            strict=True,
            actor="agent",
            workspace_id="w1",
            pocket_id=None,
        )
