# tests/cloud/test_instinct_gate_config.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T5) — config-default
# tests for the layered/learning Instinct gate's four global settings
# (2026-06-18 gate-layered-learning design).
# Updated: 2026-06-19 (feat/instinct-gate-integration, T6) — added the
# per-workspace override resolution (T-25): a Workspace document's
# `instinct_approval_level` field overrides the global default, an unset
# field falls back to the global default, and a global env var changes the
# default for workspaces that have NOT opted in (MF-9 — never a silent
# upgrade of an existing tenant that set its own value).
# Here we pin:
#   * the four fields exist with the design's default values
#   * the default approval level is "ASK" (triager dormant — zero
#     behavioral change on ship)
#   * env vars override the defaults via the POCKETPAW_ prefix
#   * resolve_workspace_approval_level honors the workspace field, falls back
#     to the global default, and degrades safe on a read failure.

from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------------------
# T-25 / T6 — per-workspace override resolution.
# ---------------------------------------------------------------------------

pytest.importorskip("pocketpaw_ee")


async def _make_workspace(level: str | None) -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="W", slug=f"w-{level or 'none'}", owner="u1")
    if level is not None:
        ws.instinct_approval_level = level
    await ws.insert()
    return str(ws.id)


@pytest.mark.usefixtures("mongo_db")
async def test_workspace_field_overrides_global_default() -> None:
    """T-25: a workspace that set TRIAGE resolves to TRIAGE even though the
    global default is ASK — the per-workspace opt-in."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    ws_id = await _make_workspace("TRIAGE")
    level = await pockets_service.resolve_workspace_approval_level(ws_id)
    assert level == "TRIAGE"


@pytest.mark.usefixtures("mongo_db")
async def test_workspace_unset_falls_back_to_global_default() -> None:
    """A workspace with no field set uses the global default (ASK)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    ws_id = await _make_workspace(None)
    level = await pockets_service.resolve_workspace_approval_level(ws_id)
    assert level == "ASK"


@pytest.mark.usefixtures("mongo_db")
async def test_resolution_degrades_safe_on_bad_id() -> None:
    """A malformed / unknown workspace id resolves to the global default, never
    a non-ASK level (a read failure must never activate the triager)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    level = await pockets_service.resolve_workspace_approval_level("not-an-objectid")
    assert level == "ASK"
