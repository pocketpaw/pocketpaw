"""Tests for the four behavioral gap fixes in the analytics/budget system.

Covers:
1. Budget enforcement fail-safe for unknown/unpriced models (usage_tracker)
2. AlertStore.mark_read() clearing per-alert _unread flags
3. guardian_block_rate read from audit log (analytics)
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── 1. Budget enforcement: unknown model fail-safe ────────────────────────────


class TestBudgetEnforcementUnknownModel:
    """usage_tracker.record() must not silently pass unknown-cost models
    when the cap is already exhausted."""

    def _make_tracker(self, tmp_path: Path):
        from pocketpaw.usage_tracker import UsageTracker

        return UsageTracker(tmp_path / "usage.jsonl")

    def test_known_model_zero_cost_does_not_block_in_record(self, tmp_path: Path) -> None:
        """record() no longer raises BudgetExhaustedError — enforcement lives
        in the async AgentLoop preflight.  A zero-cost known-model call must
        succeed and return a record with cost_usd == 0.0."""
        tracker = self._make_tracker(tmp_path)

        mock_settings = MagicMock()
        mock_settings.budget_auto_pause = True
        mock_settings.budget_monthly_usd = 0.01

        mock_snap = MagicMock()
        mock_snap.spent_usd = 0.01

        with (
            patch("pocketpaw.config.get_settings", return_value=mock_settings),
            patch("pocketpaw.budget.get_budget_snapshot", return_value=mock_snap),
        ):
            # Must NOT raise — enforcement is the loop's responsibility.
            record = tracker.record(
                backend="test",
                model="claude-3-5-haiku-20241022",
                input_tokens=0,
                output_tokens=0,
                total_cost_usd=0.0,
            )
        assert record.cost_usd == 0.0

    def test_unknown_model_does_not_block_in_record(self, tmp_path: Path) -> None:
        """An unknown model (None cost) must NOT be blocked by record() —
        enforcement is the async preflight's job. record() only logs a warning."""
        tracker = self._make_tracker(tmp_path)

        mock_settings = MagicMock()
        mock_settings.budget_auto_pause = True
        mock_settings.budget_monthly_usd = 0.01

        mock_snap = MagicMock()
        mock_snap.spent_usd = 0.01

        with (
            patch("pocketpaw.config.get_settings", return_value=mock_settings),
            patch("pocketpaw.budget.get_budget_snapshot", return_value=mock_snap),
        ):
            record = tracker.record(
                backend="test",
                model="some-new-unknown-model-xyz",
                input_tokens=1000,
                output_tokens=500,
            )
        assert record.cost_usd is None

    def test_unknown_model_passes_when_under_cap(self, tmp_path: Path) -> None:
        """An unknown model must NOT be blocked when the window is under cap."""
        tracker = self._make_tracker(tmp_path)

        mock_settings = MagicMock()
        mock_settings.budget_auto_pause = True
        mock_settings.budget_monthly_usd = 10.0

        mock_snap = MagicMock()
        mock_snap.spent_usd = 0.001  # well under cap

        with (
            patch("pocketpaw.config.get_settings", return_value=mock_settings),
            patch("pocketpaw.budget.get_budget_snapshot", return_value=mock_snap),
        ):
            record = tracker.record(
                backend="test",
                model="some-new-unknown-model-xyz",
                input_tokens=100,
                output_tokens=50,
            )
        assert record.cost_usd is None
        assert record.model == "some-new-unknown-model-xyz"

    def test_unknown_model_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown model must log a warning about missing pricing."""
        import logging

        tracker = self._make_tracker(tmp_path)

        mock_settings = MagicMock()
        mock_settings.budget_auto_pause = True
        mock_settings.budget_monthly_usd = 10.0

        mock_snap = MagicMock()
        mock_snap.spent_usd = 0.0

        with (
            patch("pocketpaw.config.get_settings", return_value=mock_settings),
            patch("pocketpaw.budget.get_budget_snapshot", return_value=mock_snap),
            caplog.at_level(logging.WARNING, logger="pocketpaw.usage_tracker"),
        ):
            tracker.record(
                backend="test",
                model="totally-unknown-model",
                input_tokens=10,
                output_tokens=5,
            )

        assert any("totally-unknown-model" in r.message for r in caplog.records)


# ── 2. AlertStore.mark_read() flag clearing ───────────────────────────────────


class TestAlertStoreMarkRead:
    def _store(self):
        from pocketpaw.alert_manager import AlertStore

        return AlertStore()

    def test_mark_read_resets_counter(self) -> None:
        store = self._store()
        store.append({"alert_type": "test", "severity": "warning", "_unread": True})
        store.append({"alert_type": "test2", "severity": "info", "_unread": True})
        assert store.unread_count == 2
        store.mark_read()
        assert store.unread_count == 0

    def test_mark_read_clears_per_alert_flags(self) -> None:
        """After mark_read(), unread_only queries must return empty."""
        store = self._store()
        store.append({"alert_type": "a", "severity": "warning", "_unread": True})
        store.append({"alert_type": "b", "severity": "info", "_unread": True})

        assert len(store.list_alerts(unread_only=True)) == 2
        store.mark_read()
        assert store.list_alerts(unread_only=True) == []

    def test_mark_read_leaves_all_alerts_for_regular_query(self) -> None:
        """mark_read() must not delete alerts, only clear their unread flag."""
        store = self._store()
        store.append({"alert_type": "a", "severity": "warning", "_unread": True})
        store.mark_read()
        assert len(store.list_alerts(unread_only=False)) == 1

    def test_new_alerts_after_mark_read_are_unread(self) -> None:
        """Alerts appended after mark_read() appear in unread_only."""
        store = self._store()
        store.append({"alert_type": "old", "_unread": True})
        store.mark_read()
        store.append({"alert_type": "new", "_unread": True})
        assert store.unread_count == 1
        unread = store.list_alerts(unread_only=True)
        assert len(unread) == 1
        assert unread[0]["alert_type"] == "new"


# ── 3. Guardian block rate from audit log ─────────────────────────────────────


class TestGuardianBlockRate:
    def _write_audit(self, path: Path, entries: list[dict]) -> None:
        with path.open("w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_no_audit_file_returns_zero(self) -> None:
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                rate = _read_guardian_block_rate_sync(datetime.now(UTC) - timedelta(days=1))
        assert rate == 0.0

    def test_all_allowed_returns_zero(self, tmp_path: Path) -> None:
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        pocketpaw_dir = tmp_path / ".pocketpaw"
        pocketpaw_dir.mkdir()
        audit = pocketpaw_dir / "audit.jsonl"
        ts = datetime.now(UTC).isoformat()
        self._write_audit(
            audit,
            [
                {"actor": "guardian", "action": "scan_result", "status": "allow", "timestamp": ts},
                {"actor": "guardian", "action": "scan_result", "status": "allow", "timestamp": ts},
            ],
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            rate = _read_guardian_block_rate_sync(datetime.now(UTC) - timedelta(days=1))
        assert rate == 0.0

    def test_half_blocked_returns_half(self, tmp_path: Path) -> None:
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        pocketpaw_dir = tmp_path / ".pocketpaw"
        pocketpaw_dir.mkdir()
        audit = pocketpaw_dir / "audit.jsonl"
        ts = datetime.now(UTC).isoformat()
        self._write_audit(
            audit,
            [
                {"actor": "guardian", "action": "scan_result", "status": "block", "timestamp": ts},
                {"actor": "guardian", "action": "scan_result", "status": "allow", "timestamp": ts},
            ],
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            rate = _read_guardian_block_rate_sync(datetime.now(UTC) - timedelta(days=1))
        assert rate == pytest.approx(0.5)

    def test_non_guardian_entries_ignored(self, tmp_path: Path) -> None:
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        pocketpaw_dir = tmp_path / ".pocketpaw"
        pocketpaw_dir.mkdir()
        audit = pocketpaw_dir / "audit.jsonl"
        ts = datetime.now(UTC).isoformat()
        self._write_audit(
            audit,
            [
                {"actor": "agent", "action": "tool_use", "status": "block", "timestamp": ts},
                {"actor": "guardian", "action": "scan_result", "status": "block", "timestamp": ts},
            ],
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            rate = _read_guardian_block_rate_sync(datetime.now(UTC) - timedelta(days=1))
        assert rate == pytest.approx(1.0)

    def test_entries_outside_window_ignored(self, tmp_path: Path) -> None:
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        pocketpaw_dir = tmp_path / ".pocketpaw"
        pocketpaw_dir.mkdir()
        audit = pocketpaw_dir / "audit.jsonl"
        old_ts = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        recent_ts = datetime.now(UTC).isoformat()
        self._write_audit(
            audit,
            [
                {
                    "actor": "guardian",
                    "action": "scan_result",
                    "status": "block",
                    "timestamp": old_ts,
                },
                {
                    "actor": "guardian",
                    "action": "scan_result",
                    "status": "allow",
                    "timestamp": recent_ts,
                },
            ],
        )
        since = datetime.now(UTC) - timedelta(days=1)
        with patch("pathlib.Path.home", return_value=tmp_path):
            rate = _read_guardian_block_rate_sync(since)
        assert rate == 0.0

    def test_pending_scan_command_entries_ignored(self, tmp_path: Path) -> None:
        """scan_command entries (pending, not a decision) must not count."""
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        pocketpaw_dir = tmp_path / ".pocketpaw"
        pocketpaw_dir.mkdir()
        audit = pocketpaw_dir / "audit.jsonl"
        ts = datetime.now(UTC).isoformat()
        self._write_audit(
            audit,
            [
                {
                    "actor": "guardian",
                    "action": "scan_command",
                    "status": "pending",
                    "timestamp": ts,
                },
                {"actor": "guardian", "action": "scan_result", "status": "allow", "timestamp": ts},
            ],
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            rate = _read_guardian_block_rate_sync(datetime.now(UTC) - timedelta(days=1))
        assert rate == 0.0

    def test_local_safety_check_entries_count(self, tmp_path: Path) -> None:
        """local_safety_check action (offline guardian) must also count."""
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        pocketpaw_dir = tmp_path / ".pocketpaw"
        pocketpaw_dir.mkdir()
        audit = pocketpaw_dir / "audit.jsonl"
        ts = datetime.now(UTC).isoformat()
        self._write_audit(
            audit,
            [
                {
                    "actor": "guardian",
                    "action": "local_safety_check",
                    "status": "block",
                    "timestamp": ts,
                },
                {
                    "actor": "guardian",
                    "action": "local_safety_check",
                    "status": "allow",
                    "timestamp": ts,
                },
            ],
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            rate = _read_guardian_block_rate_sync(datetime.now(UTC) - timedelta(days=1))
        assert rate == pytest.approx(0.5)

    def test_corrupted_lines_skipped(self, tmp_path: Path) -> None:
        from pocketpaw.analytics import _read_guardian_block_rate_sync

        pocketpaw_dir = tmp_path / ".pocketpaw"
        pocketpaw_dir.mkdir()
        audit = pocketpaw_dir / "audit.jsonl"
        ts = datetime.now(UTC).isoformat()
        with audit.open("w") as f:
            f.write("not-json\n")
            f.write(
                json.dumps(
                    {
                        "actor": "guardian",
                        "action": "scan_result",
                        "status": "block",
                        "timestamp": ts,
                    }
                )
                + "\n"
            )
        with patch("pathlib.Path.home", return_value=tmp_path):
            rate = _read_guardian_block_rate_sync(datetime.now(UTC) - timedelta(days=1))
        assert rate == pytest.approx(1.0)
