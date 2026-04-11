# evaluator.py — Background evaluation engine for automation rules.
# Created: 2026-03-30 — Periodic loop checks threshold/data_change conditions,
#   fires rules through the Instinct pipeline (propose) or directly via daemon.
#   Singleton via get_evaluator(). Start/stop endpoints wired in router.

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

from pocketpaw.ee.automations.models import ExecutionMode, Rule, RuleType, UpdateRuleRequest
from pocketpaw.ee.automations.store import get_automation_store

logger = logging.getLogger(__name__)


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
        """Check if a threshold condition is met by querying Fabric."""
        try:
            logger.debug(
                "Evaluating threshold: %s.%s %s %s",
                rule.object_type,
                rule.property,
                rule.operator,
                rule.value,
            )

            # Update last_evaluated timestamp
            store = get_automation_store()
            store.update_rule(
                rule.id,
                UpdateRuleRequest(last_evaluated=datetime.now(UTC)),
            )

            # TODO: Real Fabric query when ontology store is wired.
            # For now, return False (never fires without real data).
            return False
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
            from ee.api import get_instinct_store
            from ee.instinct.models import ActionTrigger

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
        logger.info("Notification for rule %s: %s fired", rule.name, rule.description)


# Singleton
_evaluator: AutomationEvaluator | None = None


def get_evaluator(interval: int = 30) -> AutomationEvaluator:
    """Return the module-level singleton evaluator."""
    global _evaluator
    if _evaluator is None:
        _evaluator = AutomationEvaluator(interval_seconds=interval)
    return _evaluator
