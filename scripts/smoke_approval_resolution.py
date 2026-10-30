# scripts/smoke_approval_resolution.py
# Created: 2026-07-04 (fix/approval-resolution) — live smoke proving the
# mission-control list-vs-approve "missing" bug is fixed against the running
# paw-atlas-smoke Mongo + the workspace's per-tenant instinct.db.
#
# What it proves (mirrors the WA-2 smoke pattern — init_cloud_db + init_realtime):
#   1. The real pending admin proposal (billing.manage → pro,
#      pocket_id == workspace_id == the workspace) LISTS in The Tray as a
#      ``nudge:<id>`` via ``agent_list_work_items``.
#   2. BEFORE the fix (simulated by calling the OLD tenancy split that only
#      admits ``pocket_id in visible_pockets``) bulk-approve reports the nudge
#      MISSING — the exact bug.
#   3. AFTER the fix ``agent_bulk_approve`` RESOLVES the same wire id (approved
#      non-empty, missing empty) AND fires the admin executor so the
#      billing.manage action reaches a terminal state (executed with a checkout
#      url when a provider is wired, else a clear failed-closed terminal).
#
# Runs against a CLONED pending action (fresh id) so the captain's original
# ``act-19f2abdf91b-3of8`` row stays untouched and the smoke is re-runnable.
# Read-only against Mongo except for the cloned instinct.db row it seeds/cleans.

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime

WORKSPACE_ID = "6a470535c9cfb091df549b7e"
SOURCE_ACTION_ID = "act-19f2abdf91b-3of8"
CLONE_ACTION_ID = "act-smoke-approval-fix-clone"
DB_PATH = os.path.expanduser(f"~/.pocketpaw/workspaces/{WORKSPACE_ID}/instinct.db")


def _clone_pending_action() -> dict:
    """Clone the real pending admin proposal under a fresh id so the original
    stays pending for the captain and the smoke can re-run. Returns the cloned
    blob for reporting."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    src = db.execute("SELECT * FROM instinct_actions WHERE id = ?", (SOURCE_ACTION_ID,)).fetchone()
    if src is None:
        raise SystemExit(f"source action {SOURCE_ACTION_ID} not found in {DB_PATH}")
    cols = src.keys()
    row = dict(src)
    row["id"] = CLONE_ACTION_ID
    row["status"] = "pending"
    row["executed_at"] = None
    # Reset the idempotency signal so the clone actually fires (drop any outcome).
    params = json.loads(row["parameters"]) if row["parameters"] else {}
    blob = params.get("_admin_action", {})
    blob.pop("outcome", None)
    # Fresh idempotency key so it doesn't collide with the original.
    blob["idempotency_key"] = f"{blob.get('idempotency_key', '')}:smoke-clone"
    params["_admin_action"] = blob
    row["parameters"] = json.dumps(params)
    db.execute("DELETE FROM instinct_actions WHERE id = ?", (CLONE_ACTION_ID,))
    placeholders = ",".join("?" for _ in cols)
    db.execute(
        f"INSERT INTO instinct_actions ({','.join(cols)}) VALUES ({placeholders})",
        tuple(row[c] for c in cols),
    )
    db.commit()
    db.close()
    return params["_admin_action"]


def _cleanup_clone() -> None:
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM instinct_actions WHERE id = ?", (CLONE_ACTION_ID,))
    db.commit()
    db.close()


def _action_status() -> str | None:
    db = sqlite3.connect(DB_PATH)
    row = db.execute(
        "SELECT status, parameters FROM instinct_actions WHERE id = ?", (CLONE_ACTION_ID,)
    ).fetchone()
    db.close()
    if row is None:
        return None
    return row[0]


def _action_outcome() -> dict | None:
    db = sqlite3.connect(DB_PATH)
    row = db.execute(
        "SELECT parameters FROM instinct_actions WHERE id = ?", (CLONE_ACTION_ID,)
    ).fetchone()
    db.close()
    if row is None or not row[0]:
        return None
    blob = json.loads(row[0]).get("_admin_action", {})
    return blob.get("outcome")


async def main() -> int:
    from pocketpaw_ee.cloud import init_realtime
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
    from pocketpaw_ee.cloud.mission_control import service as mc_service
    from pocketpaw_ee.cloud.mission_control.dto import BulkActionRequest, ListWorkItemsRequest
    from pocketpaw_ee.cloud.shared.db import init_cloud_db

    mongo_uri = os.environ.get("PAW_MONGO_URI") or "mongodb://localhost:27017/paw-atlas-smoke"
    await init_cloud_db(mongo_uri)
    init_realtime()

    blob = _clone_pending_action()
    print("=" * 72)
    print("SMOKE: mission-control approval resolution (fix/approval-resolution)")
    print("=" * 72)
    print(f"workspace_id        : {WORKSPACE_ID}")
    print(f"cloned action id    : {CLONE_ACTION_ID}")
    print(f"blob.action         : {blob.get('action')}  args={blob.get('args')}")
    print("pocket_id == ws?    : pocket_id carries the workspace (workspace-scoped nudge)")

    ctx = RequestContext(
        user_id="6a470488a97d9360c578f3c9",  # the proposer
        workspace_id=WORKSPACE_ID,
        request_id="smoke-approval",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )

    # --- 1. LIST: the nudge surfaces in The Tray -----------------------------
    items = await mc_service.agent_list_work_items(
        ctx, ListWorkItemsRequest(section="tray", limit=200)
    )
    wire_ids = [it.id for it in items]
    listed = f"nudge:{CLONE_ACTION_ID}" in wire_ids
    print("\n[1] LIST  (GET /mission-control/items?section=tray)")
    print(f"    nudge listed? {listed}  (wire id nudge:{CLONE_ACTION_ID})")
    if not listed:
        print("    FAIL — the nudge did not surface in the list path.")
        _cleanup_clone()
        return 1

    # --- 2. BEFORE: the OLD tenancy split reports it MISSING ------------------
    # Reproduce the pre-fix behavior directly: the old split only admitted
    # ``pocket_id in visible_pockets`` (no workspace-id clause).
    store = mc_service.get_instinct_store(workspace_id=WORKSPACE_ID)
    visible = await mc_service._visible_pocket_ids(ctx)
    old_eligible: list[str] = []
    old_blocked: list[str] = []
    for aid in [CLONE_ACTION_ID]:
        a = await store.get_action(aid)
        if a is None:
            old_eligible.append(aid)
        elif a.pocket_id in visible:  # OLD clause — no ``== workspace_id``
            old_eligible.append(aid)
        else:
            old_blocked.append(aid)
    print("\n[2] BEFORE (old tenancy split: pocket_id in visible_pockets only)")
    print(f"    eligible={old_eligible}  blocked(→missing)={old_blocked}")
    before_missing = CLONE_ACTION_ID in old_blocked
    print(f"    reproduced 'missing'? {before_missing}")
    if not before_missing:
        print("    NOTE — could not reproduce the pre-fix miss (visible set unexpected).")

    # --- 3. AFTER: the fixed bulk-approve RESOLVES + fires the executor -------
    # Optionally inject a stub payments provider so the billing executor returns
    # a real ``{checkout_url}`` and the action reaches ``executed`` (the smoke
    # env has no Dodo product config, so without this the terminal is a clean
    # failed-closed — still a real executor call, not a stranded write).
    if os.environ.get("SMOKE_STUB_DODO") == "1":
        from dataclasses import replace

        from pocketpaw_ee.cloud.billing import plans as plan_catalog
        from pocketpaw_ee.cloud.billing import service as billing_service

        class _StubCheckout:
            checkout_url = "https://checkout.dodopayments.test/sub/smoke-approval-fix"

        class _StubProvider:
            async def create_subscription(self, **_kwargs):
                return _StubCheckout()

        billing_service._default_provider = lambda: _StubProvider()  # type: ignore[assignment]
        # Give the 'pro' tier a product id so subscribe() doesn't fail-closed on
        # unconfigured Dodo products (the smoke env has no product mapping).
        _real_get_plan = plan_catalog.get_plan

        def _stub_get_plan(key):
            tier = _real_get_plan(key)
            if tier is not None and getattr(tier, "dodo_product_id", None) is None:
                tier = replace(tier, dodo_product_id="prod_smoke_pro")
            return tier

        billing_service.plan_catalog.get_plan = _stub_get_plan  # type: ignore[attr-defined]

    result = await mc_service.agent_bulk_approve(
        ctx, BulkActionRequest(ids=[f"nudge:{CLONE_ACTION_ID}"])
    )
    approved_ids = {row["id"] for row in result.get("approved", [])}
    missing = result.get("missing", [])
    executed = result.get("executed", [])
    print("\n[3] AFTER  (POST /mission-control/items/bulk-approve — fixed)")
    print(f"    approved={sorted(approved_ids)}")
    print(f"    missing ={missing}")
    print(f"    executed={json.dumps(executed)}")

    status = _action_status()
    outcome = _action_outcome()
    print(f"    action terminal status: {status}")
    if outcome:
        print(f"    executor outcome       : {json.dumps(outcome)[:400]}")

    resolved = CLONE_ACTION_ID in approved_ids and not missing
    terminal = status in ("executed", "failed")
    print("\nRESULT")
    print(f"    before=missing : {before_missing}")
    print(f"    after resolved : {resolved}  (approved non-empty, missing empty)")
    print(f"    executor fired : {terminal}  (action reached terminal '{status}')")
    checkout = None
    if outcome and isinstance(outcome, dict):
        rs = outcome.get("response_summary", "")
        if "checkout_url" in str(rs):
            checkout = rs
    if checkout:
        print(f"    checkout url   : {checkout[:200]}")

    _cleanup_clone()

    ok = resolved and terminal
    print("\n" + ("SMOKE PASSED ✅" if ok else "SMOKE FAILED ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
