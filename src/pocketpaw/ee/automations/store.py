# Automations store — JSON file-based persistence for automation rules.
# Created: 2026-03-30 — CRUD ops, toggle, fire recording. Persists to ~/.pocketpaw/automations/.
# Updated: 2026-03-30 — create_rule now passes mode/cooldown_minutes from request.

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from pocketpaw.ee.automations.models import CreateRuleRequest, Rule, UpdateRuleRequest

logger = logging.getLogger(__name__)

_STORE_DIR = Path.home() / ".pocketpaw" / "automations"
_STORE_FILE = _STORE_DIR / "rules.json"

# Module-level singleton so all callers share the same instance.
_instance: AutomationStore | None = None


class AutomationStore:
    """JSON file-backed store for automation rules."""

    def __init__(self, path: Path = _STORE_FILE) -> None:
        self._path = path
        self._rules: dict[str, Rule] = {}
        self._load()

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create_rule(self, req: CreateRuleRequest) -> Rule:
        rule = Rule(
            pocket_id=req.pocket_id,
            name=req.name,
            description=req.description,
            type=req.type,
            object_type=req.object_type,
            property=req.property,
            operator=req.operator,
            value=req.value,
            schedule=req.schedule,
            action=req.action,
            **({"mode": req.mode} if req.mode is not None else {}),
            **(
                {"cooldown_minutes": req.cooldown_minutes}
                if req.cooldown_minutes is not None
                else {}
            ),
        )
        self._rules[rule.id] = rule
        self._save()
        logger.info("Created automation rule %s: %s", rule.id, rule.name)
        return rule

    def update_rule(self, rule_id: str, updates: UpdateRuleRequest) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise KeyError(f"Rule {rule_id} not found")

        patch = updates.model_dump(exclude_unset=True)
        for field, val in patch.items():
            setattr(rule, field, val)
        rule.updated_at = datetime.utcnow()

        self._rules[rule_id] = rule
        self._save()
        logger.info("Updated automation rule %s", rule_id)
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        if rule_id not in self._rules:
            return False
        del self._rules[rule_id]
        self._save()
        logger.info("Deleted automation rule %s", rule_id)
        return True

    def toggle_rule(self, rule_id: str) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise KeyError(f"Rule {rule_id} not found")
        rule.enabled = not rule.enabled
        rule.updated_at = datetime.utcnow()
        self._rules[rule_id] = rule
        self._save()
        logger.info("Toggled rule %s -> enabled=%s", rule_id, rule.enabled)
        return rule

    def list_rules(self, pocket_id: str | None = None) -> list[Rule]:
        rules = list(self._rules.values())
        if pocket_id is not None:
            rules = [r for r in rules if r.pocket_id == pocket_id]
        return sorted(rules, key=lambda r: r.created_at, reverse=True)

    def get_rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def record_fire(self, rule_id: str) -> None:
        rule = self._rules.get(rule_id)
        if rule is None:
            return
        rule.fire_count += 1
        rule.last_fired = datetime.utcnow()
        self._rules[rule_id] = rule
        self._save()
        logger.debug("Rule %s fired (count=%d)", rule_id, rule.fire_count)

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            logger.debug("No automation rules file at %s — starting fresh", self._path)
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw:
                try:
                    rule = Rule.model_validate(item)
                    self._rules[rule.id] = rule
                except Exception:
                    logger.warning("Skipping malformed rule entry: %s", item)
            logger.info("Loaded %d automation rules from %s", len(self._rules), self._path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load automation rules: %s", exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [r.model_dump(mode="json") for r in self._rules.values()]
        self._path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def get_automation_store() -> AutomationStore:
    """Return the module-level singleton store."""
    global _instance
    if _instance is None:
        _instance = AutomationStore()
    return _instance
