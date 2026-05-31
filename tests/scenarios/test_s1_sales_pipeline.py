# tests/scenarios/test_s1_sales_pipeline.py — Phase 4 / Scenario 1.
# Created: 2026-05-10 — "Org-admin installs the Sales Fleet, sees Arrow
# soul + Pipeline pocket live."
#
# Outcome under test (per docs/playbooks/grand-smoke.md §S1):
#
#   Admin installs the bundled sales-fleet template. The installer
#   births the Arrow soul, leaves the Pipeline pocket step in
#   "skipped" state (the runtime router doesn't yet wire the cloud
#   pocket creator into the installer — tracked as #1089), and leaves
#   the HubSpot + Gong connectors skipped (no credentials supplied).
#   After the install report returns, the soul is available via
#   /soul/dashboard.
#
#   The Layer 1 Playwright spec pairs with this harness and drives
#   the paw-enterprise UI to create the Pipeline pocket as a
#   follow-on step (independent of the fleet installer's pocket
#   path), so the full demo asset still shows Arrow soul +
#   Pipeline pocket end-state.
#
# This is the smallest end-to-end proof that the fleet install path
# (ee/fleet/installer.py + ee/fleet/router.py + the bundled YAML at
# src/pocketpaw/fleet_templates/sales-fleet.yaml) actually wires the
# soul + pocket primitives together correctly. It also doubles as the
# captain-facing demo scenario for the GTM script — see
# scenario-s1-sales-pipeline.spec.ts (Layer 1) for the Playwright UI
# driver that pairs with this harness.
#
# Idempotency: the install endpoint emits "skipped" steps when an
# artifact already exists, so reruns against the same admin workspace
# pass without polluting state. The first run is the cold-create path;
# subsequent runs are the steady-state confirmation path.
#
# What we DON'T cover here (out of scope, separate scenarios or
# follow-ups):
#   - Connector secrets / OAuth dance (HubSpot, Gong) — installer
#     leaves them in 'skipped' state and that's deliberate.
#   - Codex agent chat turn with Arrow — needs an LLM provider live
#     and adds 30 s+ per run; the Layer 1 spec captures the demo
#     value of that interaction visually.
#   - Pocket widget data hydration — widgets resolve their sources
#     against Fabric/journal projections; covered by widget-level
#     tests in tests/ee/.

from __future__ import annotations

from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers — bearer login + workspace lookup, shaped to mirror S2.
# ---------------------------------------------------------------------------


def _bearer_login(api_url: str, email: str, password: str) -> str:
    """Exchange admin credentials for a fastapi-users bearer JWT."""
    resp = httpx.post(
        f"{api_url}/api/v1/auth/bearer/login",
        data={"username": email, "password": password},
        headers={"content-type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _auth_headers(token: str, workspace_id: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if workspace_id:
        h["X-Workspace-Id"] = workspace_id
    return h


def _find_owner_workspace(api_url: str, token: str) -> str:
    """Return the admin's active or first owned workspace id.

    Mirrors the S2 helper. The fleet installer enforces that the caller
    is owner OR admin of the target workspace, so an owner workspace is
    always sufficient.
    """
    resp = httpx.get(
        f"{api_url}/api/v1/auth/me",
        headers=_auth_headers(token),
        timeout=10.0,
    )
    resp.raise_for_status()
    me = resp.json()
    memberships = me.get("workspaces") or []
    if me.get("activeWorkspace"):
        for w in memberships:
            if w.get("workspace") == me["activeWorkspace"] and w.get("role") == "owner":
                return w["workspace"]
    for w in memberships:
        if w.get("role") == "owner":
            return w["workspace"]
    raise AssertionError(f"admin must own at least one workspace; got memberships={memberships!r}")


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_admin_installs_sales_fleet_end_to_end(
    api_url: str,
    admin_credentials: tuple[str, str],
) -> None:
    """End-to-end install of the bundled sales-fleet template.

    Phases:
        A — Login + locate owner workspace.
        B — GET /fleet/templates includes sales-fleet (catches packaging
            regressions where the bundled YAML doesn't reach the router).
        C — POST /fleet/install returns a FleetInstallReport whose steps
            include create_soul:Arrow as 'succeeded' (or 'skipped' on
            idempotent rerun). create_pocket and connector steps are
            allowed to be 'skipped' (cloud-side wiring is a separate
            gap — see follow-up issue noted in the module docstring).
        D — GET /soul/dashboard confirms the soul subsystem is live.
    """
    email, password = admin_credentials

    # ----- Phase A: login + workspace ------------------------------------
    token = _bearer_login(api_url, email, password)
    workspace_id = _find_owner_workspace(api_url, token)

    # ----- Phase B: confirm the template is bundled ---------------------
    resp = httpx.get(
        f"{api_url}/api/v1/fleet/templates",
        headers=_auth_headers(token),
        timeout=10.0,
    )
    assert resp.status_code == 200, f"/fleet/templates returned {resp.status_code}: {resp.text}"
    payload = resp.json()
    names = [t.get("name") for t in payload.get("templates", [])]
    assert "sales-fleet" in names, f"sales-fleet missing from bundled templates; got {names!r}"

    # ----- Phase C: install --------------------------------------------
    resp = httpx.post(
        f"{api_url}/api/v1/fleet/install",
        headers={
            **_auth_headers(token, workspace_id),
            "content-type": "application/json",
        },
        json={
            "template_name": "sales-fleet",
            "workspace_id": workspace_id,
            "journal": True,
        },
        timeout=30.0,
    )
    assert resp.status_code == 200, f"/fleet/install returned {resp.status_code}: {resp.text}"
    report: dict[str, Any] = resp.json()
    assert report.get("fleet") == "sales-fleet"

    # Step inspection: every step must be in a terminal-success state.
    # 'succeeded' = first-run cold create. 'skipped' = idempotent rerun
    # against an already-installed fleet, OR a connector with no creds
    # (HubSpot/Gong are intentionally optional in the bundle).
    steps = report.get("steps") or []
    assert steps, f"install report had no steps: {report!r}"
    terminal_ok = {"succeeded", "skipped"}
    bad_steps = [s for s in steps if s.get("status") not in terminal_ok]
    assert not bad_steps, (
        f"install steps not in succeeded/skipped: {bad_steps!r}; full report: {report!r}"
    )

    # The Arrow soul ALWAYS gets created (or is already present on
    # rerun). pocket_id is None today because the runtime router
    # doesn't wire a pocket_creator into install_fleet — separate
    # gap, see the module docstring.
    soul_id = report.get("soul_id")
    assert soul_id, f"install report missing soul_id: {report!r}"
    assert soul_id.startswith("did:soul:"), f"soul_id should be a did:soul URI; got {soul_id!r}"

    # Soul step must be in a terminal-success state. The other steps
    # (pocket, connectors) may all be 'skipped' for now — see the
    # follow-up issue noted in the module docstring.
    soul_step = next(
        (s for s in steps if s.get("name", "").startswith("create_soul:")),
        None,
    )
    assert soul_step, f"no create_soul step in report: {report!r}"
    assert soul_step.get("status") in {"succeeded", "skipped"}, (
        f"create_soul step not succeeded/skipped: {soul_step!r}"
    )

    # ----- Phase D: soul subsystem is live -----------------------------
    resp = httpx.get(
        f"{api_url}/api/v1/soul/dashboard",
        headers=_auth_headers(token),
        timeout=10.0,
    )
    assert resp.status_code == 200, f"/soul/dashboard returned {resp.status_code}: {resp.text}"
    soul = resp.json()
    # The endpoint returns {enabled: False} when soul-protocol isn't
    # wired at runtime — a degenerate environment for this scenario.
    # Surface clearly so the operator knows to enable soul.
    assert soul.get("enabled") is not False, f"soul subsystem not enabled at runtime; got {soul!r}"
