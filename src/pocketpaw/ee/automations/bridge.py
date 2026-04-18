# bridge.py — Syncs enterprise automation rules to core daemon intentions.
# Created: 2026-03-30 — Schedule/threshold/data_change rules mapped to daemon
#   intentions with cron triggers. Supports create, update, and delete sync.

"""
bridge.py — Syncs enterprise automation rules to core daemon intentions.

Schedule rules -> create a core Intention with cron trigger
Threshold rules -> create a core Intention with interval trigger (evaluator prompt)
Data change rules -> create a core Intention with event trigger (poll-based)

When a rule is created/updated/deleted, the bridge updates the corresponding intention.
"""

from __future__ import annotations

import logging

from pocketpaw.ee.automations.models import Rule, RuleType

logger = logging.getLogger(__name__)

# Map schedule presets to cron expressions
SCHEDULE_TO_CRON: dict[str, str] = {
    "Every Monday 9am": "0 9 * * 1",
    "Daily at 8am": "0 8 * * *",
    "Every hour": "0 * * * *",
    "Every 15 minutes": "*/15 * * * *",
    "First of month": "0 9 1 * *",
    "Every weekday 6pm": "0 18 * * 1-5",
}


def rule_to_intention_spec(rule: Rule) -> dict:
    """Convert an automation rule to a core daemon intention spec."""
    if rule.type == RuleType.SCHEDULE:
        cron = SCHEDULE_TO_CRON.get(rule.schedule or "", rule.schedule or "0 * * * *")
        return {
            "name": f"[auto] {rule.name}",
            "prompt": f"Automation rule fired: {rule.description}. Action: {rule.action}",
            "trigger": {"type": "cron", "schedule": cron},
            "context_sources": [],
            "enabled": rule.enabled,
        }
    elif rule.type == RuleType.THRESHOLD:
        # Threshold rules run on an interval to check conditions
        return {
            "name": f"[auto] {rule.name}",
            "prompt": (
                f"Check if {rule.object_type}.{rule.property} {rule.operator} {rule.value}. "
                f"If true, {rule.action}. "
                f"Use fabric_query to check current data."
            ),
            "trigger": {"type": "cron", "schedule": "*/5 * * * *"},  # every 5 min
            "context_sources": ["fabric"],
            "enabled": rule.enabled,
        }
    else:  # DATA_CHANGE
        return {
            "name": f"[auto] {rule.name}",
            "prompt": (
                f"A data change was detected. Check if it matches: "
                f"{rule.object_type}.{rule.property} {rule.operator}. "
                f"If yes, {rule.action}."
            ),
            "trigger": {"type": "cron", "schedule": "*/2 * * * *"},  # poll every 2 min
            "context_sources": ["fabric"],
            "enabled": rule.enabled,
        }


def sync_rule_to_daemon(rule: Rule) -> str | None:
    """Create or update a core daemon intention for this rule. Returns intention ID."""
    try:
        # Lazy import to avoid circular deps
        from pocketpaw.daemon.proactive import get_daemon

        daemon = get_daemon()
        spec = rule_to_intention_spec(rule)

        if rule.linked_intention_id:
            # Update existing intention
            result = daemon.update_intention(rule.linked_intention_id, spec)
            if result:
                return rule.linked_intention_id

        # Create new intention
        result = daemon.create_intention(**spec)
        return result.get("id") if result else None
    except Exception as e:
        logger.warning("Failed to sync rule %s to daemon: %s", rule.id, e)
        return None


def unsync_rule_from_daemon(rule: Rule) -> bool:
    """Remove the linked daemon intention when a rule is deleted."""
    if not rule.linked_intention_id:
        return True
    try:
        # Lazy import to avoid circular deps
        from pocketpaw.daemon.proactive import get_daemon

        daemon = get_daemon()
        return daemon.delete_intention(rule.linked_intention_id)
    except Exception as e:
        logger.warning("Failed to unsync rule %s: %s", rule.id, e)
        return False
