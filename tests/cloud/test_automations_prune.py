# test_automations_prune.py — Tests for prune_orphan_auto_intentions().
# Created: 2026-04-27 — Cover the startup-time orphan sweep so bridged
#   ``[auto] *`` intentions whose Rule no longer exists don't keep firing
#   crons forever. All file I/O uses tmp_path; no writes to ~/.pocketpaw.

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.daemon.intentions import IntentionStore
from pocketpaw.ee.automations.bridge import prune_orphan_auto_intentions
from pocketpaw.ee.automations.models import CreateRuleRequest, RuleType
from pocketpaw.ee.automations.store import AutomationStore


@pytest.fixture
def isolated_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch both singleton getters with tmp_path-backed instances."""
    monkeypatch.setattr(
        "pocketpaw.daemon.intentions.get_intentions_path",
        lambda: tmp_path / "intentions.json",
    )
    intention_store = IntentionStore()
    automation_store = AutomationStore(path=tmp_path / "rules.json")

    # ``prune_orphan_auto_intentions`` lazy-imports both getters inside the
    # function body to avoid circular deps, so patch their canonical
    # locations rather than re-binding on the bridge module.
    with (
        patch(
            "pocketpaw.daemon.intentions.get_intention_store",
            return_value=intention_store,
        ),
        patch(
            "pocketpaw.ee.automations.store.get_automation_store",
            return_value=automation_store,
        ),
    ):
        yield intention_store, automation_store


def _add_auto_intention(store: IntentionStore, name: str) -> dict:
    return store.create(
        name=f"[auto] {name}",
        prompt="seeded for test",
        trigger={"type": "cron", "schedule": "*/5 * * * *"},
        enabled=True,
    )


class TestPruneOrphanAutoIntentions:
    def test_drops_orphan_auto_intention(self, isolated_stores) -> None:
        intention_store, _ = isolated_stores
        _add_auto_intention(intention_store, "Stale rule")

        pruned = prune_orphan_auto_intentions()

        assert pruned == 1
        assert intention_store.get_all() == []

    def test_keeps_auto_intention_with_live_rule(self, isolated_stores) -> None:
        intention_store, automation_store = isolated_stores
        intention = _add_auto_intention(intention_store, "Live rule")
        rule = automation_store.create_rule(
            CreateRuleRequest(
                name="Live rule",
                description="kept",
                type=RuleType.SCHEDULE,
                schedule="0 9 * * 1",
                action="ping",
            )
        )
        rule.linked_intention_id = intention["id"]

        pruned = prune_orphan_auto_intentions()

        assert pruned == 0
        assert len(intention_store.get_all()) == 1

    def test_keeps_user_created_intention(self, isolated_stores) -> None:
        """Non-``[auto]`` entries are user-managed — never touch them."""
        intention_store, _ = isolated_stores
        intention_store.create(
            name="Morning standup",
            prompt="What are your top 3?",
            trigger={"type": "cron", "schedule": "0 8 * * 1-5"},
        )

        pruned = prune_orphan_auto_intentions()

        assert pruned == 0
        assert len(intention_store.get_all()) == 1

    def test_mixed_set(self, isolated_stores) -> None:
        intention_store, automation_store = isolated_stores
        live = _add_auto_intention(intention_store, "Live")
        _add_auto_intention(intention_store, "Orphan A")
        _add_auto_intention(intention_store, "Orphan B")
        intention_store.create(
            name="User intention",
            prompt="hi",
            trigger={"type": "cron", "schedule": "0 * * * *"},
        )

        rule = automation_store.create_rule(
            CreateRuleRequest(
                name="Live",
                description="kept",
                type=RuleType.SCHEDULE,
                schedule="0 9 * * 1",
                action="ping",
            )
        )
        rule.linked_intention_id = live["id"]

        pruned = prune_orphan_auto_intentions()

        assert pruned == 2
        names = {i["name"] for i in intention_store.get_all()}
        assert names == {"[auto] Live", "User intention"}

    def test_no_intentions_is_a_noop(self, isolated_stores) -> None:
        assert prune_orphan_auto_intentions() == 0

    def test_prune_emits_only_summary_log(self, isolated_stores, caplog) -> None:
        """Prune must not log every deletion individually — only the summary."""
        import logging

        intention_store, _ = isolated_stores
        for i in range(5):
            _add_auto_intention(intention_store, f"Orphan {i}")

        with caplog.at_level(logging.INFO):
            pruned = prune_orphan_auto_intentions()

        assert pruned == 5
        delete_lines = [r for r in caplog.records if "Deleted intention" in r.message]
        summary_lines = [
            r
            for r in caplog.records
            if "Pruned" in r.message and "orphan" in r.message
        ]
        assert delete_lines == [], "per-item delete log must be suppressed during prune"
        assert len(summary_lines) == 1, "expected exactly one summary line"
