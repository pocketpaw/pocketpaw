# tests/cloud/test_paw_bar_decision_loop_tenancy.py — B0 H1 (store-split repro).
#
# Created: 2026-07-15 (fix/paw-bar-decision-loop-tenancy). Reproduces the H1
# tenancy defect: a paw-bar customer-decision proposal was written through the
# BARE ``get_instinct_store()`` (no workspace) with the in-row workspace resolved
# from the widget OWNER, while the cloud dashboard's pending feed reads the
# PER-WORKSPACE store keyed by the widget's REAL ``workspace_id``. In cloud /
# flag mode (``POCKETPAW_REQUIRE_WORKSPACE_SCOPE``) the public ingest path has NO
# ``current_workspace`` context, so the bare factory raised ``WorkspaceScopeRequired``
# — swallowed by the best-effort loop — and the proposal was DROPPED: the owner
# never saw it in The Tray.
#
# Unlike ``tests/cloud/test_paw_bar_decision_loop.py`` this test does NOT
# monkeypatch ``get_instinct_store`` (that stub — one store, arg-ignoring — is
# exactly what hides the split). It drives the REAL store factory against a tmp
# data dir with the cloud flag on, and asserts the proposal is visible in the
# tenant's OWN per-workspace pending read.

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.paw_bar.decision_loop import CUSTOMER_REPLY_KEY, propose_customer_decision

import pocketpaw.stores as stores
from pocketpaw.paw_bar.models import PawBarEvent, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

# A REAL, server-stamped workspace id (create_widget stamps this from the
# authenticated session): a store-path-safe token, NOT the colon-qualified owner.
WS_REAL = "wreal01"
OWNER = "user:maya"  # a within-tenant human label — colon-qualified, NOT a ws id


@pytest.fixture(autouse=True)
def _cloud_flag_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real store factory, tmp data dir, cloud flag ON, no workspace context.

    Mirrors ``tests/test_instinct_workspace_isolation.py::_isolate_data_dir`` but
    with the required-scope flag SET and the ContextVar cleared — the exact state
    of the PUBLIC ``POST /paw-bar/events/{id}`` ingest path in cloud mode.
    """
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    stores.reset_store_caches()
    token = stores.current_workspace.set(None)
    try:
        yield
    finally:
        try:
            stores.current_workspace.reset(token)
        except ValueError:
            stores.current_workspace.set(None)
        stores.reset_store_caches()


def _widget() -> PawBarWidget:
    spec = PawBarSpec(widget_id="pp_x", pocket_id="pocket-1")
    return PawBarWidget(
        id="pp_x",
        pocket_id="pocket-1",
        owner=OWNER,
        workspace_id=WS_REAL,
        name="Appointment Widget",
        spec=spec,
    )


def _event() -> PawBarEvent:
    return PawBarEvent(
        widget_id="pp_x",
        type="appointment_request",
        payload={"when": "tuesday 9am"},
        customer_ref="cust-1",
    )


async def test_cloud_proposal_lands_in_the_tenants_pending_feed(tmp_path: Path) -> None:
    """The proposal must be visible in the widget's REAL-workspace pending read.

    Pre-fix: the bare ``get_instinct_store()`` raises ``WorkspaceScopeRequired``
    (flag on + no context) → swallowed → ``action_id is None`` and the tenant's
    pending feed is empty. Post-fix: the write routes through
    ``get_instinct_store(workspace_id=widget.workspace_id)`` so it lands in the
    SAME per-workspace store the dashboard reads.
    """
    pp_store = PawBarStore(tmp_path / "paw_bar.db")
    action_id = await propose_customer_decision(
        widget=_widget(), event=_event(), paw_bar_store=pp_store
    )

    assert action_id is not None, (
        "the cloud proposal was dropped — the owner never sees it in The Tray"
    )

    # The dashboard's GET /instinct/actions/pending resolves the PER-WORKSPACE
    # store keyed by the caller's REAL active workspace and filters in-row by it.
    dashboard_store = stores.get_instinct_store(workspace_id=WS_REAL)
    pending = await dashboard_store.pending(workspace_id=WS_REAL)

    assert [a.id for a in pending] == [action_id], (
        "the proposal is not in the tenant's per-workspace pending feed"
    )
    # And its tenancy key is the REAL workspace, not the colon-qualified owner.
    blob = pending[0].parameters[CUSTOMER_REPLY_KEY]
    assert blob["workspace_id"] == WS_REAL, (
        f"proposal scoped to {blob['workspace_id']!r}, expected the real workspace"
    )
