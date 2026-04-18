# test_ee_evaluator.py
# Tests for the automation evaluator, bridge, and related router/model behavior.
# Created: 2026-03-30 — Covers bridge spec conversion, evaluator lifecycle and cooldown logic,
#   _fire_rule dispatch by mode, router evaluator endpoints, router bridge integration,
#   and model defaults/enums. All I/O uses tmp_path; daemon and instinct store are mocked.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.ee.automations.bridge import (
    SCHEDULE_TO_CRON,
    rule_to_intention_spec,
    sync_rule_to_daemon,
    unsync_rule_from_daemon,
)
from pocketpaw.ee.automations.evaluator import AutomationEvaluator
from pocketpaw.ee.automations.models import (
    CreateRuleRequest,
    ExecutionMode,
    Rule,
    RuleType,
    UpdateRuleRequest,
)
from pocketpaw.ee.automations.router import router
from pocketpaw.ee.automations.store import AutomationStore

# ============================================================================
# Helpers / factories
# ============================================================================


def _schedule_rule(**kwargs) -> Rule:
    defaults = dict(
        name="Weekly digest",
        type=RuleType.SCHEDULE,
        schedule="Daily at 8am",
        action="send_digest",
        enabled=True,
    )
    defaults.update(kwargs)
    return Rule(**defaults)


def _threshold_rule(**kwargs) -> Rule:
    defaults = dict(
        name="Low stock alert",
        type=RuleType.THRESHOLD,
        object_type="Product",
        property="stock",
        operator="less_than",
        value="10",
        action="notify:owner",
        enabled=True,
    )
    defaults.update(kwargs)
    return Rule(**defaults)


def _data_change_rule(**kwargs) -> Rule:
    defaults = dict(
        name="Price change",
        type=RuleType.DATA_CHANGE,
        object_type="Product",
        property="price",
        operator="changed",
        action="notify:manager",
        enabled=True,
    )
    defaults.update(kwargs)
    return Rule(**defaults)


def _threshold_req(**kwargs) -> CreateRuleRequest:
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


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_path: Path) -> AutomationStore:
    """Fresh AutomationStore backed by a temp file — never touches ~/.pocketpaw."""
    return AutomationStore(path=tmp_path / "rules.json")


@pytest.fixture
def evaluator() -> AutomationEvaluator:
    """Fresh AutomationEvaluator instance (not the global singleton)."""
    return AutomationEvaluator(interval_seconds=30)


@pytest.fixture
def app() -> FastAPI:
    """Minimal FastAPI app that mounts the automations router."""
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    return application


@pytest.fixture
def client_with_mocks(app: FastAPI, tmp_path: Path):
    """
    TestClient with:
    - isolated store backed by tmp_path
    - bridge functions (sync/unsync) stubbed out — no daemon required
    - a fresh evaluator injected for evaluator endpoint tests
    """
    isolated_store = AutomationStore(path=tmp_path / "rules.json")
    fresh_evaluator = AutomationEvaluator(interval_seconds=30)

    with (
        patch(
            "pocketpaw.ee.automations.router.get_automation_store",
            return_value=isolated_store,
        ),
        patch(
            "pocketpaw.ee.automations.router.sync_rule_to_daemon",
            return_value=None,
        ),
        patch(
            "pocketpaw.ee.automations.router.unsync_rule_from_daemon",
            return_value=True,
        ),
        patch(
            "pocketpaw.ee.automations.router.get_evaluator",
            return_value=fresh_evaluator,
        ),
    ):
        yield TestClient(app), isolated_store, fresh_evaluator


# ============================================================================
# Bridge tests
# ============================================================================


class TestRuleToIntentionSpec:
    def test_rule_to_intention_spec_schedule(self) -> None:
        """Schedule rule maps to cron intention with correct resolved schedule."""
        rule = _schedule_rule(schedule="Daily at 8am")
        spec = rule_to_intention_spec(rule)

        assert spec["trigger"]["type"] == "cron"
        assert spec["trigger"]["schedule"] == "0 8 * * *"
        assert rule.name in spec["name"]

    def test_rule_to_intention_spec_threshold(self) -> None:
        """Threshold rule maps to polling intention that includes fabric context source."""
        rule = _threshold_rule()
        spec = rule_to_intention_spec(rule)

        assert spec["trigger"]["type"] == "cron"
        assert "fabric" in spec["context_sources"]
        assert rule.object_type in spec["prompt"]
        assert rule.property in spec["prompt"]

    def test_rule_to_intention_spec_data_change(self) -> None:
        """Data_change rule maps to polling intention with fabric context."""
        rule = _data_change_rule()
        spec = rule_to_intention_spec(rule)

        assert spec["trigger"]["type"] == "cron"
        assert "fabric" in spec["context_sources"]
        assert rule.object_type in spec["prompt"]

    def test_schedule_to_cron_mapping(self) -> None:
        """All preset schedule strings in SCHEDULE_TO_CRON map to non-empty cron expressions."""
        for preset, cron in SCHEDULE_TO_CRON.items():
            # Minimal sanity: cron expression has 5 space-separated parts
            parts = cron.split()
            assert len(parts) == 5, f"Bad cron for '{preset}': {cron!r}"

    def test_unknown_schedule_passthrough(self) -> None:
        """An unknown schedule string is passed through as-is to the cron trigger."""
        custom_cron = "*/10 6 * * 2"
        rule = _schedule_rule(schedule=custom_cron)
        spec = rule_to_intention_spec(rule)

        assert spec["trigger"]["schedule"] == custom_cron

    def test_rule_to_intention_includes_name_prefix(self) -> None:
        """Intention name is prefixed with '[auto]' regardless of rule type."""
        for rule in [_schedule_rule(), _threshold_rule(), _data_change_rule()]:
            spec = rule_to_intention_spec(rule)
            assert spec["name"].startswith("[auto] "), (
                f"Missing [auto] prefix for rule type {rule.type}: {spec['name']!r}"
            )

    def test_disabled_rule_intention(self) -> None:
        """A disabled rule produces an intention spec with enabled=False."""
        rule = _schedule_rule(enabled=False)
        spec = rule_to_intention_spec(rule)

        assert spec["enabled"] is False


class TestSyncRuleToDaemon:
    def test_sync_rule_returns_intention_id(self) -> None:
        """sync_rule_to_daemon returns the intention id from the daemon."""
        mock_daemon = MagicMock()
        mock_daemon.create_intention.return_value = {"id": "intention-abc"}
        rule = _schedule_rule()

        with patch("pocketpaw.daemon.proactive.get_daemon", return_value=mock_daemon):
            result = sync_rule_to_daemon(rule)

        assert result == "intention-abc"

    def test_sync_rule_returns_none_on_exception(self) -> None:
        """sync_rule_to_daemon returns None (and does not raise) when daemon is unavailable."""
        with patch(
            "pocketpaw.daemon.proactive.get_daemon",
            side_effect=RuntimeError("daemon not running"),
        ):
            result = sync_rule_to_daemon(_schedule_rule())

        assert result is None

    def test_unsync_rule_calls_delete_intention(self) -> None:
        """unsync_rule_from_daemon delegates to daemon.delete_intention for linked rules."""
        mock_daemon = MagicMock()
        mock_daemon.delete_intention.return_value = True
        rule = _schedule_rule(linked_intention_id="int-xyz")

        with patch("pocketpaw.daemon.proactive.get_daemon", return_value=mock_daemon):
            result = unsync_rule_from_daemon(rule)

        mock_daemon.delete_intention.assert_called_once_with("int-xyz")
        assert result is True

    def test_unsync_rule_no_linked_intention_is_noop(self) -> None:
        """unsync_rule_from_daemon returns True immediately when no linked_intention_id."""
        rule = _schedule_rule()  # no linked_intention_id
        assert rule.linked_intention_id is None

        result = unsync_rule_from_daemon(rule)
        assert result is True


# ============================================================================
# Evaluator lifecycle tests
# ============================================================================


class TestEvaluatorLifecycle:
    def test_evaluator_start_sets_running_true(self, evaluator: AutomationEvaluator) -> None:
        """start() sets is_running to True."""
        assert evaluator.is_running is False
        # Patch asyncio.create_task so the loop doesn't actually run
        with patch("asyncio.create_task"):
            evaluator.start()
        assert evaluator.is_running is True
        evaluator.stop()

    def test_evaluator_stop_sets_running_false(self, evaluator: AutomationEvaluator) -> None:
        """stop() sets is_running to False."""
        with patch("asyncio.create_task"):
            evaluator.start()
        evaluator.stop()
        assert evaluator.is_running is False

    def test_evaluator_is_running_property(self, evaluator: AutomationEvaluator) -> None:
        """is_running property reflects internal _running state."""
        evaluator._running = True
        assert evaluator.is_running is True
        evaluator._running = False
        assert evaluator.is_running is False

    def test_evaluator_start_idempotent(self, evaluator: AutomationEvaluator) -> None:
        """Calling start() a second time when already running does not create a second task."""
        with patch("asyncio.create_task") as mock_create_task:
            evaluator.start()
            evaluator.start()
        # create_task should only be called once
        assert mock_create_task.call_count == 1
        evaluator.stop()


# ============================================================================
# Evaluator _evaluate_all logic tests
# ============================================================================


class TestEvaluatorEvaluateAll:
    @pytest.mark.asyncio
    async def test_evaluator_skips_disabled_rules(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """Disabled rules are never evaluated (no _evaluate_threshold call)."""
        store.create_rule(_threshold_req(name="disabled", **{"enabled": True}))
        # Retrieve and disable it
        rule = store.list_rules()[0]
        store.toggle_rule(rule.id)  # now disabled

        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(evaluator, "_evaluate_threshold", new_callable=AsyncMock) as mock_eval,
        ):
            await evaluator._evaluate_all()

        mock_eval.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluator_skips_schedule_rules(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """Schedule rules are skipped by the evaluator (handled by daemon TriggerEngine)."""
        store.create_rule(
            CreateRuleRequest(name="cron rule", type=RuleType.SCHEDULE, schedule="0 9 * * *")
        )

        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(evaluator, "_evaluate_threshold", new_callable=AsyncMock) as mock_thresh,
            patch.object(evaluator, "_evaluate_data_change", new_callable=AsyncMock) as mock_dc,
        ):
            await evaluator._evaluate_all()

        mock_thresh.assert_not_called()
        mock_dc.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluator_respects_cooldown(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """Rule that fired recently (within cooldown window) is skipped."""
        req = _threshold_req(name="hot rule")
        rule = store.create_rule(req)
        # Set last_fired to 5 minutes ago; cooldown default is 60 minutes
        recent = datetime.now(UTC) - timedelta(minutes=5)
        store.update_rule(rule.id, UpdateRuleRequest(last_evaluated=None))
        rule = store.get_rule(rule.id)
        rule.last_fired = recent
        store._rules[rule.id] = rule

        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(evaluator, "_evaluate_threshold", new_callable=AsyncMock) as mock_eval,
        ):
            await evaluator._evaluate_all()

        mock_eval.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluator_fires_after_cooldown(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """Rule whose last_fired is past the cooldown window IS evaluated."""
        req = _threshold_req(name="cool rule")
        rule = store.create_rule(req)
        # Set last_fired to 90 minutes ago; cooldown default is 60 minutes
        old_fire = datetime.now(UTC) - timedelta(minutes=90)
        rule = store.get_rule(rule.id)
        rule.last_fired = old_fire
        store._rules[rule.id] = rule

        # _evaluate_threshold returns False so no _fire_rule occurs
        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(
                evaluator, "_evaluate_threshold", new_callable=AsyncMock, return_value=False
            ) as mock_eval,
        ):
            await evaluator._evaluate_all()

        mock_eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_evaluate_threshold_returns_false_by_design(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """_evaluate_threshold currently returns False (Fabric not wired yet)."""
        rule = store.create_rule(_threshold_req())
        fetched = store.get_rule(rule.id)

        with patch(
            "pocketpaw.ee.automations.evaluator.get_automation_store",
            return_value=store,
        ):
            result = await evaluator._evaluate_threshold(fetched)

        assert result is False

    @pytest.mark.asyncio
    async def test_evaluate_data_change_returns_false_by_design(
        self, evaluator: AutomationEvaluator
    ) -> None:
        """_evaluate_data_change currently returns False (event bus not wired yet)."""
        rule = _data_change_rule()
        result = await evaluator._evaluate_data_change(rule)
        assert result is False


# ============================================================================
# Evaluator _fire_rule dispatch tests
# ============================================================================


class TestFireRule:
    @pytest.mark.asyncio
    async def test_fire_rule_require_approval(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """REQUIRE_APPROVAL mode calls _propose_action."""
        rule = store.create_rule(_threshold_req(name="approve me"))
        fetched = store.get_rule(rule.id)
        fetched.mode = ExecutionMode.REQUIRE_APPROVAL

        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(evaluator, "_propose_action", new_callable=AsyncMock) as mock_propose,
        ):
            await evaluator._fire_rule(fetched)

        mock_propose.assert_called_once_with(fetched)

    @pytest.mark.asyncio
    async def test_fire_rule_auto_execute(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """AUTO_EXECUTE mode calls _execute_directly."""
        rule = store.create_rule(_threshold_req(name="auto run"))
        fetched = store.get_rule(rule.id)
        fetched.mode = ExecutionMode.AUTO_EXECUTE

        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(evaluator, "_execute_directly", new_callable=AsyncMock) as mock_execute,
        ):
            await evaluator._fire_rule(fetched)

        mock_execute.assert_called_once_with(fetched)

    @pytest.mark.asyncio
    async def test_fire_rule_notify_only(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """NOTIFY_ONLY mode calls _notify and does not call propose or execute."""
        rule = store.create_rule(_threshold_req(name="notify only"))
        fetched = store.get_rule(rule.id)
        fetched.mode = ExecutionMode.NOTIFY_ONLY

        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(evaluator, "_notify", new_callable=AsyncMock) as mock_notify,
            patch.object(evaluator, "_propose_action", new_callable=AsyncMock) as mock_propose,
            patch.object(evaluator, "_execute_directly", new_callable=AsyncMock) as mock_execute,
        ):
            await evaluator._fire_rule(fetched)

        mock_notify.assert_called_once_with(fetched)
        mock_propose.assert_not_called()
        mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_rule_increments_fire_count(
        self, evaluator: AutomationEvaluator, store: AutomationStore
    ) -> None:
        """After _fire_rule, store.record_fire is called, incrementing fire_count."""
        rule = store.create_rule(_threshold_req(name="count fires"))
        fetched = store.get_rule(rule.id)
        assert fetched.fire_count == 0

        with (
            patch(
                "pocketpaw.ee.automations.evaluator.get_automation_store",
                return_value=store,
            ),
            patch.object(evaluator, "_propose_action", new_callable=AsyncMock),
        ):
            await evaluator._fire_rule(fetched)

        updated = store.get_rule(rule.id)
        assert updated.fire_count == 1
        assert updated.last_fired is not None

    @pytest.mark.asyncio
    async def test_execute_directly_triggers_daemon(self, evaluator: AutomationEvaluator) -> None:
        """_execute_directly calls daemon.run_intention_now for rules with a linked intention."""
        rule = _threshold_rule(linked_intention_id="int-123", mode=ExecutionMode.AUTO_EXECUTE)

        mock_daemon = MagicMock()
        mock_daemon.run_intention_now = AsyncMock()

        with (
            patch("pocketpaw.daemon.proactive.get_daemon", return_value=mock_daemon),
            patch("asyncio.create_task") as mock_create_task,
        ):
            await evaluator._execute_directly(rule)

        # create_task is called with the coroutine from run_intention_now
        mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_directly_no_linked_intention_is_warning(
        self, evaluator: AutomationEvaluator
    ) -> None:
        """_execute_directly with no linked_intention_id logs a warning without raising."""
        rule = _threshold_rule(mode=ExecutionMode.AUTO_EXECUTE)
        assert rule.linked_intention_id is None

        # Should not raise even without a daemon available
        await evaluator._execute_directly(rule)


# ============================================================================
# Router — evaluator endpoint tests
# ============================================================================


class TestEvaluatorEndpoints:
    def test_evaluator_status_initially_stopped(self, client_with_mocks) -> None:
        """GET /evaluator/status returns running=false before start."""
        client, _, evaluator = client_with_mocks
        resp = client.get("/api/v1/automations/evaluator/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False

    def test_evaluator_start_endpoint(self, client_with_mocks) -> None:
        """POST /evaluator/start returns ok and starts the evaluator."""
        client, _, fresh_evaluator = client_with_mocks
        with patch("asyncio.create_task"):
            resp = client.post("/api/v1/automations/evaluator/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        fresh_evaluator.stop()

    def test_evaluator_stop_endpoint(self, client_with_mocks) -> None:
        """POST /evaluator/stop returns ok when evaluator is running."""
        client, _, fresh_evaluator = client_with_mocks
        # Manually start the evaluator
        with patch("asyncio.create_task"):
            fresh_evaluator.start()
        resp = client.post("/api/v1/automations/evaluator/stop")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_evaluator_status_after_start(self, client_with_mocks) -> None:
        """GET /evaluator/status returns running=true after start is called."""
        client, _, fresh_evaluator = client_with_mocks
        with patch("asyncio.create_task"):
            client.post("/api/v1/automations/evaluator/start")
        resp = client.get("/api/v1/automations/evaluator/status")
        assert resp.status_code == 200
        assert resp.json()["running"] is True
        fresh_evaluator.stop()

    def test_evaluator_start_when_already_running(self, client_with_mocks) -> None:
        """POST /evaluator/start when already running returns already_running status."""
        client, _, fresh_evaluator = client_with_mocks
        with patch("asyncio.create_task"):
            fresh_evaluator.start()
            resp = client.post("/api/v1/automations/evaluator/start")
        assert resp.json()["status"] == "already_running"
        fresh_evaluator.stop()

    def test_evaluator_stop_when_already_stopped(self, client_with_mocks) -> None:
        """POST /evaluator/stop when not running returns already_stopped status."""
        client, _, _ = client_with_mocks
        resp = client.post("/api/v1/automations/evaluator/stop")
        assert resp.json()["status"] == "already_stopped"


# ============================================================================
# Router — bridge integration tests
# ============================================================================


class TestRouterBridgeIntegration:
    def test_create_rule_syncs_to_daemon(self, app: FastAPI, tmp_path: Path) -> None:
        """POST /rules creates rule AND calls sync_rule_to_daemon."""
        isolated_store = AutomationStore(path=tmp_path / "rules.json")
        mock_sync = MagicMock(return_value="int-from-daemon")

        with (
            patch(
                "pocketpaw.ee.automations.router.get_automation_store",
                return_value=isolated_store,
            ),
            patch("pocketpaw.ee.automations.router.sync_rule_to_daemon", mock_sync),
            patch("pocketpaw.ee.automations.router.get_evaluator", return_value=MagicMock()),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/automations/rules",
                json={"name": "sync test", "type": "schedule", "schedule": "0 9 * * 1"},
            )

        assert resp.status_code == 201
        mock_sync.assert_called_once()
        # The created rule should have the linked intention id set
        data = resp.json()
        assert data["linked_intention_id"] == "int-from-daemon"

    def test_delete_rule_unsyncs_from_daemon(self, app: FastAPI, tmp_path: Path) -> None:
        """DELETE /rules/{id} removes rule AND calls unsync_rule_from_daemon."""
        isolated_store = AutomationStore(path=tmp_path / "rules.json")
        rule = isolated_store.create_rule(
            CreateRuleRequest(name="to unsync", type=RuleType.SCHEDULE)
        )
        mock_unsync = MagicMock(return_value=True)

        with (
            patch(
                "pocketpaw.ee.automations.router.get_automation_store",
                return_value=isolated_store,
            ),
            patch("pocketpaw.ee.automations.router.unsync_rule_from_daemon", mock_unsync),
            patch("pocketpaw.ee.automations.router.sync_rule_to_daemon", return_value=None),
            patch("pocketpaw.ee.automations.router.get_evaluator", return_value=MagicMock()),
        ):
            client = TestClient(app)
            resp = client.delete(f"/api/v1/automations/rules/{rule.id}")

        assert resp.status_code == 200
        mock_unsync.assert_called_once()

    def test_toggle_rule_syncs_to_daemon(self, app: FastAPI, tmp_path: Path) -> None:
        """POST /rules/{id}/toggle updates the daemon intention via sync_rule_to_daemon."""
        isolated_store = AutomationStore(path=tmp_path / "rules.json")
        rule = isolated_store.create_rule(
            CreateRuleRequest(name="to toggle sync", type=RuleType.THRESHOLD)
        )
        mock_sync = MagicMock(return_value=None)

        with (
            patch(
                "pocketpaw.ee.automations.router.get_automation_store",
                return_value=isolated_store,
            ),
            patch("pocketpaw.ee.automations.router.sync_rule_to_daemon", mock_sync),
            patch("pocketpaw.ee.automations.router.get_evaluator", return_value=MagicMock()),
        ):
            client = TestClient(app)
            resp = client.post(f"/api/v1/automations/rules/{rule.id}/toggle")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        mock_sync.assert_called_once()


# ============================================================================
# Model tests
# ============================================================================


class TestExecutionModeEnum:
    def test_execution_mode_enum_values(self) -> None:
        """ExecutionMode has exactly the three expected members."""
        modes = {m.value for m in ExecutionMode}
        assert modes == {"require_approval", "auto_execute", "notify_only"}

    def test_rule_default_mode(self) -> None:
        """New Rule defaults to REQUIRE_APPROVAL mode."""
        rule = Rule(name="test", type=RuleType.THRESHOLD)
        assert rule.mode == ExecutionMode.REQUIRE_APPROVAL

    def test_rule_default_cooldown(self) -> None:
        """New Rule defaults to 60-minute cooldown."""
        rule = Rule(name="test", type=RuleType.THRESHOLD)
        assert rule.cooldown_minutes == 60

    def test_create_rule_request_with_mode(self) -> None:
        """CreateRuleRequest accepts and stores the mode field."""
        req = CreateRuleRequest(
            name="auto rule",
            type=RuleType.THRESHOLD,
            mode=ExecutionMode.AUTO_EXECUTE,
        )
        assert req.mode == ExecutionMode.AUTO_EXECUTE

    def test_create_rule_request_mode_defaults_to_none(self) -> None:
        """CreateRuleRequest mode field defaults to None (store fills in default)."""
        req = CreateRuleRequest(name="plain rule", type=RuleType.THRESHOLD)
        assert req.mode is None

    def test_store_create_rule_applies_mode(self, store: AutomationStore) -> None:
        """AutomationStore.create_rule sets mode from request when provided."""
        req = CreateRuleRequest(
            name="notify rule",
            type=RuleType.THRESHOLD,
            mode=ExecutionMode.NOTIFY_ONLY,
        )
        rule = store.create_rule(req)
        assert rule.mode == ExecutionMode.NOTIFY_ONLY

    def test_store_create_rule_default_mode_when_omitted(self, store: AutomationStore) -> None:
        """AutomationStore.create_rule falls back to model default
        (REQUIRE_APPROVAL) when mode not in request."""
        req = CreateRuleRequest(name="default rule", type=RuleType.THRESHOLD)
        rule = store.create_rule(req)
        assert rule.mode == ExecutionMode.REQUIRE_APPROVAL
