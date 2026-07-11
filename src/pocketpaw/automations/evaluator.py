# evaluator.py — Background evaluation engine for automation rules.
# Created: 2026-03-30 — Periodic loop checks threshold/data_change conditions,
#   fires rules through the Instinct pipeline (propose) or directly via daemon.
#   Singleton via get_evaluator(). Start/stop endpoints wired in router.
# Updated: 2026-07-11 (feat/external-alerting-c2c3) — replaced the
#   ``_evaluate_threshold`` ``return False`` stub with a REAL Fabric query: the
#   rule's (object_type, property, operator, value) becomes a FabricQuery with a
#   comparison filter, run against the OSS FabricStore; the rule fires when at
#   least one object crosses the threshold. Operator strings ("less_than",
#   "greater_than", "equals", …) map to the store's whitelisted filter operators.
#   OSS-only — no EE import (the store is resolved via pocketpaw.stores).

"""
evaluator.py — Background evaluation engine for automation rules.

Runs periodically, checks threshold/data_change conditions against Fabric data,
and fires rules through the Instinct pipeline or directly via daemon.

The evaluator is started/stopped via the router and runs alongside the ProactiveDaemon.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from pocketpaw.automations.models import ExecutionMode, Rule, RuleType, UpdateRuleRequest
from pocketpaw.automations.store import get_automation_store

logger = logging.getLogger(__name__)

# Rule operator strings -> FabricStore whitelisted filter operators
# (see fabric.store._FILTER_OPERATORS). Anything not in this map is a
# non-comparable rule (e.g. "changed" is handled by the data_change path)
# and never fires from the threshold branch.
_OPERATOR_MAP: dict[str, str] = {
    "less_than": "lt",
    "lt": "lt",
    "<": "lt",
    "less_or_equal": "lte",
    "less_than_or_equal": "lte",
    "lte": "lte",
    "<=": "lte",
    "greater_than": "gt",
    "gt": "gt",
    ">": "gt",
    "greater_or_equal": "gte",
    "greater_than_or_equal": "gte",
    "gte": "gte",
    ">=": "gte",
    "equals": "eq",
    "equal": "eq",
    "eq": "eq",
    "==": "eq",
    "not_equals": "ne",
    "ne": "ne",
    "!=": "ne",
}


def _coerce_value(raw: str) -> object:
    """Coerce a rule's string threshold value to int/float when it looks numeric.

    Fabric comparison filters CAST the stored property to REAL, so a numeric
    threshold must be bound as a number, not the string ``"10"``. Non-numeric
    values (an equality against a status string) pass through unchanged.
    """
    try:
        if "." in raw or "e" in raw.lower():
            return float(raw)
        return int(raw)
    except (ValueError, AttributeError):
        return raw


class AutomationEvaluator:
    """Background loop that evaluates automation rules."""

    def __init__(self, interval_seconds: int = 30):
        self.interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("AutomationEvaluator started (interval=%ds)", self.interval)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("AutomationEvaluator stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._evaluate_all()
            except Exception as e:
                logger.error("Evaluation cycle failed: %s", e)
            await asyncio.sleep(self.interval)

    async def _evaluate_all(self) -> None:
        store = get_automation_store()
        rules = store.list_rules()

        for rule in rules:
            if not rule.enabled:
                continue

            # Check cooldown
            if rule.last_fired and rule.cooldown_minutes > 0:
                last = rule.last_fired
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                cooldown_until = last + timedelta(minutes=rule.cooldown_minutes)
                if datetime.now(UTC) < cooldown_until:
                    continue

            if rule.type == RuleType.THRESHOLD:
                fired = await self._evaluate_threshold(rule)
            elif rule.type == RuleType.DATA_CHANGE:
                fired = await self._evaluate_data_change(rule)
            else:
                # Schedule rules are handled by the daemon's TriggerEngine
                continue

            if fired:
                await self._fire_rule(rule)

    async def _evaluate_threshold(self, rule: Rule) -> bool:
        """Check if a threshold condition is met by querying Fabric.

        Builds a :class:`~pocketpaw.fabric.models.FabricQuery` from the rule's
        ``(object_type, property, operator, value)`` and runs it against the OSS
        FabricStore. The rule FIRES when at least one object matches — e.g.
        "Product.stock less_than 10" fires the moment any Product's stock drops
        under 10. A malformed / non-comparable rule (missing fields, an operator
        with no comparison mapping) never fires and is logged at debug.
        """
        try:
            logger.debug(
                "Evaluating threshold: %s.%s %s %s",
                rule.object_type,
                rule.property,
                rule.operator,
                rule.value,
            )

            # Update last_evaluated timestamp regardless of outcome so the UI can
            # show the rule is being actively checked.
            store = get_automation_store()
            store.update_rule(
                rule.id,
                UpdateRuleRequest(last_evaluated=datetime.now(UTC)),
            )

            if not (rule.object_type and rule.property and rule.operator and rule.value):
                logger.debug("Rule %s has an incomplete threshold condition — skipping", rule.id)
                return False

            fabric_op = _OPERATOR_MAP.get(rule.operator.strip().lower())
            if fabric_op is None:
                logger.debug(
                    "Rule %s operator %r has no comparison mapping — skipping",
                    rule.id,
                    rule.operator,
                )
                return False

            # Lazy import: keep the automations package importable without the
            # Fabric stack at module load, and avoid a heavy startup-time import.
            from pocketpaw.fabric.models import FabricQuery
            from pocketpaw.stores import get_fabric_store

            query = FabricQuery(
                type_name=rule.object_type,
                filters={rule.property: {fabric_op: _coerce_value(rule.value)}},
                limit=1,
            )
            # OSS is single-tenant: the store resolves the default workspace and
            # an unscoped read is correct here (the cloud sweep path applies
            # per-workspace scoping separately).
            result = await get_fabric_store().query(query)
            fired = result.total > 0
            if fired:
                logger.info(
                    "Threshold met for rule %s: %s.%s %s %s (%d matching object(s))",
                    rule.id,
                    rule.object_type,
                    rule.property,
                    rule.operator,
                    rule.value,
                    result.total,
                )
            return fired
        except Exception as e:
            logger.debug("Threshold evaluation failed for rule %s: %s", rule.id, e)
            return False

    async def _evaluate_data_change(self, rule: Rule) -> bool:
        """Check if a data change event matches the rule condition."""
        # TODO: Hook into event bus for real-time data change detection
        return False

    async def _fire_rule(self, rule: Rule) -> None:
        """Fire a rule -- propose Instinct action or execute directly."""
        store = get_automation_store()
        store.record_fire(rule.id)

        logger.info("Rule fired: %s (mode=%s)", rule.name, rule.mode)

        if rule.mode == ExecutionMode.REQUIRE_APPROVAL:
            await self._propose_action(rule)
        elif rule.mode == ExecutionMode.AUTO_EXECUTE:
            await self._execute_directly(rule)
        elif rule.mode == ExecutionMode.NOTIFY_ONLY:
            await self._notify(rule)

    async def _propose_action(self, rule: Rule) -> None:
        """Propose an Instinct action for human approval."""
        try:
            # Lazy import to avoid circular deps and heavy startup cost
            from pocketpaw.instinct.models import ActionTrigger
            from pocketpaw.stores import get_instinct_store

            instinct = get_instinct_store()
            trigger = ActionTrigger(
                type="automation",
                source=rule.id,
                reason=f"Rule condition met: {rule.description}",
            )
            await instinct.propose(
                pocket_id=rule.pocket_id,
                title=rule.name,
                description=f"Automation fired: {rule.description}",
                recommendation=rule.action,
                trigger=trigger,
                context=None,
            )
            logger.info("Proposed Instinct action for rule: %s", rule.name)
        except Exception as e:
            logger.error("Failed to propose action for rule %s: %s", rule.id, e)

    async def _execute_directly(self, rule: Rule) -> None:
        """Execute via the daemon (agent runs the prompt directly)."""
        try:
            if rule.linked_intention_id:
                from pocketpaw.daemon.proactive import get_daemon

                daemon = get_daemon()
                asyncio.create_task(daemon.run_intention_now(rule.linked_intention_id))
                logger.info("Triggered direct execution for rule: %s", rule.name)
            else:
                logger.warning("Rule %s has auto_execute mode but no linked intention", rule.id)
        except Exception as e:
            logger.error("Failed to execute rule %s: %s", rule.id, e)

    async def _notify(self, rule: Rule) -> None:
        """Send notification only (no action proposal or execution)."""
        # TODO: Integrate with notification system (WebSocket, email, etc.)
        # FOLLOW-UP (external alerting): the cloud external fan-out (Slack /
        # generic webhook) lives in ee.cloud.notifications.delivery, but OSS core
        # MUST NOT import EE (import-linter "OSS core may not import from EE").
        # Reaching external sinks from here needs a structurally-different path —
        # an event-bus indirection the EE layer subscribes, or an OSS-side sink
        # impl — not a direct import. Tracked as a separate slice.
        logger.info("Notification for rule %s: %s fired", rule.name, rule.description)


# Singleton
_evaluator: AutomationEvaluator | None = None


def get_evaluator(interval: int = 30) -> AutomationEvaluator:
    """Return the module-level singleton evaluator."""
    global _evaluator
    if _evaluator is None:
        _evaluator = AutomationEvaluator(interval_seconds=interval)
    return _evaluator
