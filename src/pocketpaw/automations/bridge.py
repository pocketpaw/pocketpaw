# bridge.py — Syncs enterprise automation rules to core daemon intentions.
# Created: 2026-03-30 — Schedule/threshold/data_change rules mapped to daemon
#   intentions with cron triggers. Supports create, update, and delete sync.
# Updated: 2026-04-27 — Added ``prune_orphan_auto_intentions()`` so the
#   startup path can drop bridged ``[auto] *`` intentions whose source
#   Rule no longer exists. Tests that don't isolate ``~/.pocketpaw/`` left
#   stale entries that re-saved on every cron fire and registered phantom
#   jobs with the trigger engine on every restart.
# Updated: 2026-04-27 — Escape the ``[auto]`` token in the prune-summary
#   log message. The dashboard's Rich console handler treats ``[auto]``
#   as a markup tag and swallows it, leaving readers with a confusing
#   ``Pruned N orphan  intentions`` (double space, no prefix).

"""
bridge.py — Syncs enterprise automation rules to core daemon intentions.

Schedule rules -> create a core Intention with cron trigger
Threshold rules -> create a core Intention with interval trigger (evaluator prompt)
Data change rules -> create a core Intention with event trigger (poll-based)

When a rule is created/updated/deleted, the bridge updates the corresponding intention.
"""

from __future__ import annotations

import logging

from pocketpaw.automations.models import Rule, RuleType

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


_AUTO_PREFIX = "[auto] "


def prune_orphan_auto_intentions() -> int:
    """Drop ``[auto] *`` daemon intentions whose source Rule no longer exists.

    The bridge writes ``[auto] <name>`` intentions when an EE automation rule
    is created. If the rule is deleted out-of-band — test fixtures that don't
    isolate ``~/.pocketpaw/``, manual edits to ``automations/rules.json``,
    a corrupted rules file — the linked intention stays in
    ``intentions.json`` and keeps firing its cron forever. Run this on
    startup so a clean slate stays clean.

    A bridged intention is "orphan" when no Rule has
    ``linked_intention_id`` pointing at it.

    Returns the number of intentions pruned.
    """
    from pocketpaw.automations.store import get_automation_store
    from pocketpaw.daemon.intentions import get_intention_store

    intention_store = get_intention_store()
    automation_store = get_automation_store()

    valid_ids: set[str] = {
        rule.linked_intention_id
        for rule in automation_store.list_rules()
        if rule.linked_intention_id
    }

    pruned = 0
    # ``get_all`` returns a copy, so deleting during iteration is safe, but
    # snapshot anyway to be explicit about intent.
    for intention in list(intention_store.get_all()):
        name = intention.get("name", "")
        if not name.startswith(_AUTO_PREFIX):
            continue
        if intention["id"] in valid_ids:
            continue
        # quiet=True suppresses the per-item ``Deleted intention: ...`` INFO
        # log. The summary line below is the only output we want at boot.
        intention_store.delete(intention["id"], quiet=True)
        pruned += 1

    if pruned:
        # Escape the brackets so Rich's console handler doesn't parse
        # ``[auto]`` as a markup tag and swallow the prefix in the output.
        logger.info("Pruned %d orphan \\[auto] intentions on startup", pruned)
    return pruned
