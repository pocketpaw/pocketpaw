# tests/cloud/test_instinct_gate_config.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T5) — config-default
# tests for the layered/learning Instinct gate's four global settings
# (2026-06-18 gate-layered-learning design). These are GLOBAL DEFAULTS;
# the per-workspace override path (the full T-25 workspace-document field)
# lands with the integration layer (T6, separate gated PR). Here we pin:
#   * the four fields exist with the design's default values
#   * the default approval level is "ASK" (triager dormant — zero
#     behavioral change on ship)
#   * env vars override the defaults via the POCKETPAW_ prefix

from __future__ import annotations

from pocketpaw.config import Settings


def test_instinct_gate_defaults() -> None:
    """The four gate settings default to the design's safe values."""
    s = Settings()
    assert s.instinct_approval_level == "ASK"
    assert s.instinct_auto_approve_threshold == 0.9
    assert s.instinct_dry_run_mode is False
    assert s.instinct_optimistic_ttl_seconds == 300


def test_default_approval_level_is_dormant() -> None:
    """ASK is the dormant level — nothing auto-approves until an admin
    opts a workspace into TRIAGE. This is the zero-behavioral-change
    guarantee for shipping the foundation."""
    assert Settings().instinct_approval_level == "ASK"


def test_env_overrides_approval_level(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", "TRIAGE")
    assert Settings().instinct_approval_level == "TRIAGE"


def test_env_overrides_threshold(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_INSTINCT_AUTO_APPROVE_THRESHOLD", "0.75")
    assert Settings().instinct_auto_approve_threshold == 0.75


def test_env_overrides_dry_run_mode(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_INSTINCT_DRY_RUN_MODE", "true")
    assert Settings().instinct_dry_run_mode is True


def test_env_overrides_optimistic_ttl(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_INSTINCT_OPTIMISTIC_TTL_SECONDS", "600")
    assert Settings().instinct_optimistic_ttl_seconds == 600
