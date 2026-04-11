# test_ee_automations.py — Tests for the enterprise Automations module (rule CRUD).
# Created: 2026-03-30 — Unit tests for AutomationStore + integration tests for the
# FastAPI router. All file I/O uses tmp_path; no writes to ~/.pocketpaw.

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.ee.automations.models import (
    CreateRuleRequest,
    Rule,
    RuleType,
    UpdateRuleRequest,
)
from pocketpaw.ee.automations.router import router
from pocketpaw.ee.automations.store import AutomationStore

# ============================================================================
# Helpers / shared factories
# ============================================================================


def _threshold_req(**kwargs) -> CreateRuleRequest:
    """Return a minimal threshold CreateRuleRequest."""
    defaults = dict(
        name="Low stock alert",
        type=RuleType.THRESHOLD,
        pocket_id="pocket-1",
        object_type="Product",
        property="stock",
        operator="less_than",
        value="10",
        action="notify:owner",
    )
    defaults.update(kwargs)
    return CreateRuleRequest(**defaults)


def _schedule_req(**kwargs) -> CreateRuleRequest:
    """Return a minimal schedule CreateRuleRequest."""
    defaults = dict(
        name="Daily report",
        type=RuleType.SCHEDULE,
        pocket_id="pocket-1",
        schedule="0 9 * * *",
        action="send_report",
    )
    defaults.update(kwargs)
    return CreateRuleRequest(**defaults)


def _data_change_req(**kwargs) -> CreateRuleRequest:
    """Return a minimal data_change CreateRuleRequest."""
    defaults = dict(
        name="Price change alert",
        type=RuleType.DATA_CHANGE,
        pocket_id="pocket-2",
        object_type="Product",
        property="price",
        operator="changed",
        action="notify:manager",
    )
    defaults.update(kwargs)
    return CreateRuleRequest(**defaults)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_path: Path) -> AutomationStore:
    """Fresh AutomationStore backed by a temp file — never touches ~/.pocketpaw."""
    return AutomationStore(path=tmp_path / "rules.json")


@pytest.fixture
def app() -> FastAPI:
    """Minimal FastAPI app that mounts the automations router."""
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    return application


@pytest.fixture
def client(app: FastAPI, tmp_path: Path) -> TestClient:
    """TestClient with the singleton store replaced by a tmp_path-backed instance."""
    isolated_store = AutomationStore(path=tmp_path / "rules.json")
    with patch(
        "pocketpaw.ee.automations.router.get_automation_store",
        return_value=isolated_store,
    ):
        yield TestClient(app)


# ============================================================================
# Unit tests — AutomationStore
# ============================================================================


class TestCreateThresholdRule:
    def test_create_rule(self, store: AutomationStore) -> None:
        """create_rule returns a Rule with all fields populated correctly."""
        req = _threshold_req()
        rule = store.create_rule(req)

        assert isinstance(rule, Rule)
        assert rule.id  # 12-char uuid fragment
        assert rule.name == "Low stock alert"
        assert rule.type == RuleType.THRESHOLD
        assert rule.pocket_id == "pocket-1"
        assert rule.object_type == "Product"
        assert rule.property == "stock"
        assert rule.operator == "less_than"
        assert rule.value == "10"
        assert rule.action == "notify:owner"
        assert rule.enabled is True
        assert rule.fire_count == 0
        assert rule.last_fired is None
        assert rule.created_at is not None
        assert rule.updated_at is not None


class TestCreateScheduleRule:
    def test_create_schedule_rule(self, store: AutomationStore) -> None:
        """create_rule with schedule type stores the cron expression."""
        req = _schedule_req()
        rule = store.create_rule(req)

        assert rule.type == RuleType.SCHEDULE
        assert rule.schedule == "0 9 * * *"
        assert rule.action == "send_report"
        # threshold-specific fields should be None
        assert rule.object_type is None
        assert rule.operator is None
        assert rule.value is None


class TestCreateDataChangeRule:
    def test_create_data_change_rule(self, store: AutomationStore) -> None:
        """create_rule with data_change type stores operator 'changed'."""
        req = _data_change_req()
        rule = store.create_rule(req)

        assert rule.type == RuleType.DATA_CHANGE
        assert rule.object_type == "Product"
        assert rule.property == "price"
        assert rule.operator == "changed"
        assert rule.schedule is None


class TestListRules:
    def test_list_rules_returns_all(self, store: AutomationStore) -> None:
        """list_rules returns every rule that has been created."""
        store.create_rule(_threshold_req(name="R1"))
        store.create_rule(_threshold_req(name="R2"))
        store.create_rule(_schedule_req(name="R3"))

        rules = store.list_rules()
        assert len(rules) == 3

    def test_list_rules_by_pocket(self, store: AutomationStore) -> None:
        """list_rules filtered by pocket_id returns only matching rules."""
        store.create_rule(_threshold_req(name="pocket-1 rule A", pocket_id="pocket-1"))
        store.create_rule(_threshold_req(name="pocket-1 rule B", pocket_id="pocket-1"))
        store.create_rule(_data_change_req(name="pocket-2 rule", pocket_id="pocket-2"))

        p1_rules = store.list_rules(pocket_id="pocket-1")
        p2_rules = store.list_rules(pocket_id="pocket-2")

        assert len(p1_rules) == 2
        assert all(r.pocket_id == "pocket-1" for r in p1_rules)
        assert len(p2_rules) == 1
        assert p2_rules[0].pocket_id == "pocket-2"

    def test_list_rules_empty_store(self, store: AutomationStore) -> None:
        """list_rules returns an empty list when no rules exist."""
        assert store.list_rules() == []

    def test_list_rules_sorted_newest_first(self, store: AutomationStore) -> None:
        """list_rules returns rules sorted newest created_at first."""
        r1 = store.create_rule(_threshold_req(name="first"))
        r2 = store.create_rule(_threshold_req(name="second"))
        r3 = store.create_rule(_threshold_req(name="third"))

        rules = store.list_rules()
        # Most recently created is first; IDs uniquely identify each rule.
        ids = [r.id for r in rules]
        assert ids == [r3.id, r2.id, r1.id]


class TestGetRule:
    def test_get_rule(self, store: AutomationStore) -> None:
        """get_rule returns the rule for a known id."""
        created = store.create_rule(_threshold_req())
        fetched = store.get_rule(created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == created.name

    def test_get_rule_nonexistent(self, store: AutomationStore) -> None:
        """get_rule returns None for an unknown id."""
        result = store.get_rule("does-not-exist")
        assert result is None


class TestUpdateRule:
    def test_update_rule(self, store: AutomationStore) -> None:
        """update_rule applies name and description changes."""
        rule = store.create_rule(_threshold_req(description="original"))
        updated = store.update_rule(
            rule.id,
            UpdateRuleRequest(name="New name", description="Updated description"),
        )

        assert updated.name == "New name"
        assert updated.description == "Updated description"
        # Other fields should be untouched
        assert updated.type == RuleType.THRESHOLD
        assert updated.id == rule.id

    def test_update_rule_partial(self, store: AutomationStore) -> None:
        """update_rule with a single field only changes that field."""
        rule = store.create_rule(_threshold_req(name="Original name"))
        updated = store.update_rule(rule.id, UpdateRuleRequest(description="Just this"))

        # Name unchanged
        assert updated.name == "Original name"
        assert updated.description == "Just this"

    def test_update_rule_updates_timestamp(self, store: AutomationStore) -> None:
        """update_rule sets a new updated_at timestamp."""
        rule = store.create_rule(_threshold_req())
        original_ts = rule.updated_at

        updated = store.update_rule(rule.id, UpdateRuleRequest(name="Changed"))
        assert updated.updated_at >= original_ts

    def test_update_rule_nonexistent_raises(self, store: AutomationStore) -> None:
        """update_rule raises KeyError for an unknown id."""
        with pytest.raises(KeyError, match="not found"):
            store.update_rule("ghost-id", UpdateRuleRequest(name="x"))


class TestDeleteRule:
    def test_delete_rule(self, store: AutomationStore) -> None:
        """delete_rule removes the rule and returns True."""
        rule = store.create_rule(_threshold_req())
        result = store.delete_rule(rule.id)

        assert result is True
        assert store.get_rule(rule.id) is None

    def test_delete_nonexistent(self, store: AutomationStore) -> None:
        """delete_rule returns False for an id that does not exist."""
        result = store.delete_rule("no-such-rule")
        assert result is False

    def test_delete_removes_from_list(self, store: AutomationStore) -> None:
        """Deleted rule no longer appears in list_rules."""
        r1 = store.create_rule(_threshold_req(name="keep"))
        r2 = store.create_rule(_threshold_req(name="delete me"))
        store.delete_rule(r2.id)

        remaining = store.list_rules()
        assert len(remaining) == 1
        assert remaining[0].id == r1.id


class TestToggleRule:
    def test_toggle_rule(self, store: AutomationStore) -> None:
        """toggle_rule flips enabled from True to False."""
        rule = store.create_rule(_threshold_req())
        assert rule.enabled is True

        toggled = store.toggle_rule(rule.id)
        assert toggled.enabled is False

    def test_toggle_twice(self, store: AutomationStore) -> None:
        """Toggling twice returns the rule to its original state."""
        rule = store.create_rule(_threshold_req())
        store.toggle_rule(rule.id)
        twice = store.toggle_rule(rule.id)

        assert twice.enabled is True

    def test_toggle_nonexistent_raises(self, store: AutomationStore) -> None:
        """toggle_rule raises KeyError for an unknown id."""
        with pytest.raises(KeyError, match="not found"):
            store.toggle_rule("nope")

    def test_toggle_updates_timestamp(self, store: AutomationStore) -> None:
        """toggle_rule updates the updated_at timestamp."""
        rule = store.create_rule(_threshold_req())
        original_ts = rule.updated_at

        toggled = store.toggle_rule(rule.id)
        assert toggled.updated_at >= original_ts


class TestRecordFire:
    def test_record_fire(self, store: AutomationStore) -> None:
        """record_fire increments fire_count and sets last_fired."""
        rule = store.create_rule(_threshold_req())
        assert rule.fire_count == 0
        assert rule.last_fired is None

        store.record_fire(rule.id)
        updated = store.get_rule(rule.id)

        assert updated.fire_count == 1
        assert updated.last_fired is not None

    def test_record_fire_multiple(self, store: AutomationStore) -> None:
        """record_fire accumulates fire_count across multiple calls."""
        rule = store.create_rule(_threshold_req())
        store.record_fire(rule.id)
        store.record_fire(rule.id)
        store.record_fire(rule.id)

        updated = store.get_rule(rule.id)
        assert updated.fire_count == 3

    def test_record_fire_nonexistent_is_noop(self, store: AutomationStore) -> None:
        """record_fire for an unknown id does not raise — it is a no-op."""
        # Should not raise
        store.record_fire("phantom-id")


class TestPersistence:
    def test_persistence(self, tmp_path: Path) -> None:
        """Rules survive a new AutomationStore instance reading the same file."""
        path = tmp_path / "rules.json"

        store1 = AutomationStore(path=path)
        rule = store1.create_rule(
            _threshold_req(name="Persisted rule", description="should survive reload")
        )
        rule_id = rule.id

        # Simulate a new process / server restart by creating a fresh store.
        store2 = AutomationStore(path=path)
        recovered = store2.get_rule(rule_id)

        assert recovered is not None
        assert recovered.name == "Persisted rule"
        assert recovered.description == "should survive reload"
        assert recovered.type == RuleType.THRESHOLD
        assert recovered.pocket_id == "pocket-1"

    def test_persistence_multiple_rules(self, tmp_path: Path) -> None:
        """All rules in the store persist across reloads."""
        path = tmp_path / "rules.json"
        store1 = AutomationStore(path=path)
        ids = []
        for i in range(5):
            r = store1.create_rule(_threshold_req(name=f"Rule {i}"))
            ids.append(r.id)

        store2 = AutomationStore(path=path)
        assert len(store2.list_rules()) == 5
        for rid in ids:
            assert store2.get_rule(rid) is not None

    def test_empty_store_starts_fresh(self, tmp_path: Path) -> None:
        """A store pointed at a non-existent file starts with zero rules."""
        store = AutomationStore(path=tmp_path / "nonexistent.json")
        assert store.list_rules() == []


# ============================================================================
# Integration tests — FastAPI router via TestClient
# ============================================================================


class TestCreateRuleEndpoint:
    def test_create_rule_endpoint(self, client: TestClient) -> None:
        """POST /api/v1/automations/rules creates a rule and returns 201."""
        payload = {
            "name": "API threshold rule",
            "type": "threshold",
            "pocket_id": "p-99",
            "object_type": "Order",
            "property": "revenue",
            "operator": "greater_than",
            "value": "1000",
            "action": "notify:sales",
        }
        resp = client.post("/api/v1/automations/rules", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "API threshold rule"
        assert data["type"] == "threshold"
        assert data["pocket_id"] == "p-99"
        assert data["enabled"] is True
        assert "id" in data

    def test_create_rule_endpoint_schedule(self, client: TestClient) -> None:
        """POST with schedule type persists the cron field."""
        payload = {
            "name": "Weekly digest",
            "type": "schedule",
            "schedule": "0 8 * * 1",
            "action": "send_digest",
        }
        resp = client.post("/api/v1/automations/rules", json=payload)
        assert resp.status_code == 201
        assert resp.json()["schedule"] == "0 8 * * 1"

    def test_create_rule_endpoint_missing_required_field(self, client: TestClient) -> None:
        """POST without a required field (name) returns 422."""
        payload = {"type": "threshold"}
        resp = client.post("/api/v1/automations/rules", json=payload)
        assert resp.status_code == 422


class TestListRulesEndpoint:
    def test_list_rules_endpoint_empty(self, client: TestClient) -> None:
        """GET /api/v1/automations/rules returns an empty list when no rules exist."""
        resp = client.get("/api/v1/automations/rules")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_rules_endpoint(self, client: TestClient) -> None:
        """GET /api/v1/automations/rules returns all created rules."""
        for i in range(3):
            client.post(
                "/api/v1/automations/rules",
                json={"name": f"Rule {i}", "type": "threshold"},
            )
        resp = client.get("/api/v1/automations/rules")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_list_rules_endpoint_filter_by_pocket(self, client: TestClient) -> None:
        """GET with ?pocket_id= returns only rules for that pocket."""
        client.post(
            "/api/v1/automations/rules",
            json={"name": "Pocket A rule", "type": "threshold", "pocket_id": "a"},
        )
        client.post(
            "/api/v1/automations/rules",
            json={"name": "Pocket B rule", "type": "threshold", "pocket_id": "b"},
        )
        resp = client.get("/api/v1/automations/rules?pocket_id=a")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["pocket_id"] == "a"


class TestGetRuleEndpoint:
    def test_get_rule_endpoint(self, client: TestClient) -> None:
        """GET /api/v1/automations/rules/{id} returns the rule."""
        create_resp = client.post(
            "/api/v1/automations/rules",
            json={"name": "Fetchable rule", "type": "data_change"},
        )
        rule_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/automations/rules/{rule_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == rule_id
        assert resp.json()["name"] == "Fetchable rule"

    def test_get_rule_endpoint_not_found(self, client: TestClient) -> None:
        """GET for a non-existent id returns 404."""
        resp = client.get("/api/v1/automations/rules/does-not-exist")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestUpdateRuleEndpoint:
    def test_update_rule_endpoint(self, client: TestClient) -> None:
        """PATCH /api/v1/automations/rules/{id} applies partial updates."""
        create_resp = client.post(
            "/api/v1/automations/rules",
            json={"name": "Before update", "type": "threshold"},
        )
        rule_id = create_resp.json()["id"]

        resp = client.patch(
            f"/api/v1/automations/rules/{rule_id}",
            json={"name": "After update", "description": "Now has a description"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "After update"
        assert data["description"] == "Now has a description"
        assert data["id"] == rule_id

    def test_update_rule_endpoint_not_found(self, client: TestClient) -> None:
        """PATCH on a non-existent rule returns 404."""
        resp = client.patch(
            "/api/v1/automations/rules/no-such-rule",
            json={"name": "Ghost"},
        )
        assert resp.status_code == 404


class TestDeleteRuleEndpoint:
    def test_delete_rule_endpoint(self, client: TestClient) -> None:
        """DELETE /api/v1/automations/rules/{id} removes the rule and returns ok."""
        create_resp = client.post(
            "/api/v1/automations/rules",
            json={"name": "To delete", "type": "schedule"},
        )
        rule_id = create_resp.json()["id"]

        resp = client.delete(f"/api/v1/automations/rules/{rule_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["id"] == rule_id

        # Confirm it is gone
        get_resp = client.get(f"/api/v1/automations/rules/{rule_id}")
        assert get_resp.status_code == 404

    def test_delete_nonexistent_endpoint(self, client: TestClient) -> None:
        """DELETE on a non-existent rule returns 404."""
        resp = client.delete("/api/v1/automations/rules/ghost-rule")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestToggleEndpoint:
    def test_toggle_endpoint(self, client: TestClient) -> None:
        """POST /api/v1/automations/rules/{id}/toggle flips enabled flag."""
        create_resp = client.post(
            "/api/v1/automations/rules",
            json={"name": "Toggleable", "type": "threshold"},
        )
        rule_id = create_resp.json()["id"]
        assert create_resp.json()["enabled"] is True

        resp = client.post(f"/api/v1/automations/rules/{rule_id}/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_toggle_endpoint_not_found(self, client: TestClient) -> None:
        """Toggle on a non-existent rule returns 404."""
        resp = client.post("/api/v1/automations/rules/ghost/toggle")
        assert resp.status_code == 404

    def test_toggle_endpoint_idempotent_pairs(self, client: TestClient) -> None:
        """Two consecutive toggles return to the original enabled state."""
        create_resp = client.post(
            "/api/v1/automations/rules",
            json={"name": "Double toggle", "type": "schedule"},
        )
        rule_id = create_resp.json()["id"]

        client.post(f"/api/v1/automations/rules/{rule_id}/toggle")
        resp = client.post(f"/api/v1/automations/rules/{rule_id}/toggle")

        assert resp.json()["enabled"] is True


class TestFullCrudLifecycle:
    def test_full_crud_lifecycle(self, client: TestClient) -> None:
        """Create -> Read -> Update -> Toggle -> Delete end-to-end."""
        # Create
        create_resp = client.post(
            "/api/v1/automations/rules",
            json={
                "name": "Lifecycle rule",
                "type": "threshold",
                "pocket_id": "lifecycle-pocket",
                "object_type": "Inventory",
                "property": "units",
                "operator": "less_than",
                "value": "5",
                "action": "reorder",
                "description": "Auto-reorder when stock dips",
            },
        )
        assert create_resp.status_code == 201
        rule_id = create_resp.json()["id"]
        assert create_resp.json()["name"] == "Lifecycle rule"

        # Read
        get_resp = client.get(f"/api/v1/automations/rules/{rule_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["pocket_id"] == "lifecycle-pocket"

        # Update
        patch_resp = client.patch(
            f"/api/v1/automations/rules/{rule_id}",
            json={"value": "3", "description": "Updated threshold"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["value"] == "3"
        assert patch_resp.json()["description"] == "Updated threshold"

        # Toggle (disable)
        toggle_resp = client.post(f"/api/v1/automations/rules/{rule_id}/toggle")
        assert toggle_resp.status_code == 200
        assert toggle_resp.json()["enabled"] is False

        # Confirm state via list
        list_resp = client.get("/api/v1/automations/rules?pocket_id=lifecycle-pocket")
        assert list_resp.status_code == 200
        listed = list_resp.json()
        assert len(listed) == 1
        assert listed[0]["enabled"] is False

        # Delete
        del_resp = client.delete(f"/api/v1/automations/rules/{rule_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["ok"] is True

        # Verify gone
        final_resp = client.get(f"/api/v1/automations/rules/{rule_id}")
        assert final_resp.status_code == 404
