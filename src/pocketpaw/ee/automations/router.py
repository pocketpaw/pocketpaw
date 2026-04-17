# Automations router — REST API for rule-based pocket automations.
# Created: 2026-03-30 — CRUD endpoints, toggle, mounted at /api/v1/automations.
# Updated: 2026-03-30 — Wired bridge sync on create/update/delete/toggle.
#   Added evaluator start/stop/status endpoints.

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from pocketpaw.ee.automations.bridge import sync_rule_to_daemon, unsync_rule_from_daemon
from pocketpaw.ee.automations.evaluator import get_evaluator
from pocketpaw.ee.automations.models import CreateRuleRequest, Rule, UpdateRuleRequest
from pocketpaw.ee.automations.store import get_automation_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automations", tags=["Automations"])


# ── Rule CRUD ──────────────────────────────────────────────────────────────────


@router.get("/rules", response_model=list[Rule])
async def list_rules(pocket_id: str | None = None):
    """List all automation rules, optionally filtered by pocket_id."""
    store = get_automation_store()
    return store.list_rules(pocket_id=pocket_id)


@router.post("/rules", response_model=Rule, status_code=201)
async def create_rule(body: CreateRuleRequest):
    """Create a new automation rule and sync to daemon."""
    store = get_automation_store()
    rule = store.create_rule(body)

    # Sync to core daemon — creates a linked intention
    intention_id = sync_rule_to_daemon(rule)
    if intention_id:
        store.update_rule(rule.id, UpdateRuleRequest(linked_intention_id=intention_id))
        rule.linked_intention_id = intention_id
        logger.info("Rule %s linked to intention %s", rule.id, intention_id)

    return rule


@router.get("/rules/{rule_id}", response_model=Rule)
async def get_rule(rule_id: str):
    """Get a single automation rule by ID."""
    store = get_automation_store()
    rule = store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return rule


@router.patch("/rules/{rule_id}", response_model=Rule)
async def update_rule(rule_id: str, body: UpdateRuleRequest):
    """Update an existing automation rule and re-sync to daemon."""
    store = get_automation_store()
    try:
        rule = store.update_rule(rule_id, body)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")

    # Re-sync to daemon with updated config
    intention_id = sync_rule_to_daemon(rule)
    if intention_id and intention_id != rule.linked_intention_id:
        store.update_rule(rule.id, UpdateRuleRequest(linked_intention_id=intention_id))
        rule.linked_intention_id = intention_id

    return rule


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str):
    """Delete an automation rule and remove its daemon intention."""
    store = get_automation_store()
    rule = store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")

    # Unsync from daemon first
    unsync_rule_from_daemon(rule)

    deleted = store.delete_rule(rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return {"ok": True, "id": rule_id}


@router.post("/rules/{rule_id}/toggle", response_model=Rule)
async def toggle_rule(rule_id: str):
    """Toggle the enabled state of an automation rule and update daemon."""
    store = get_automation_store()
    try:
        rule = store.toggle_rule(rule_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")

    # Re-sync to daemon (updates enabled state on the intention)
    sync_rule_to_daemon(rule)
    return rule


# ── Evaluator control ──────────────────────────────────────────────────────────


@router.post("/evaluator/start")
async def start_evaluator():
    """Start the background automation evaluator."""
    evaluator = get_evaluator()
    if evaluator.is_running:
        return {"ok": True, "status": "already_running"}
    evaluator.start()
    return {"ok": True, "status": "started"}


@router.post("/evaluator/stop")
async def stop_evaluator():
    """Stop the background automation evaluator."""
    evaluator = get_evaluator()
    if not evaluator.is_running:
        return {"ok": True, "status": "already_stopped"}
    evaluator.stop()
    return {"ok": True, "status": "stopped"}


@router.get("/evaluator/status")
async def evaluator_status():
    """Get the current evaluator status."""
    evaluator = get_evaluator()
    return {
        "running": evaluator.is_running,
        "interval_seconds": evaluator.interval,
    }
