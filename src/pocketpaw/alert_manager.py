"""AlertManager — periodic threshold checks and structured alert pipeline.

Publishes SystemEvent(event_type="alert", …) on the message bus for:
    - error_spike      — error rate exceeds threshold
    - tool_degradation — tool failure rate exceeds threshold
    - channel_disconnect — an active channel adapter stopped
    - budget_warning   — budget crosses warning threshold (re-emitted every check)
    - budget_exhausted — budget is exhausted

Usage
-----
    manager = get_alert_manager()
    await manager.start()   # called by dashboard_lifecycle on startup
    await manager.stop()    # called on shutdown

API surface
-----------
    manager.list_alerts(limit, unread_only) -> list[dict]
    manager.unread_count -> int
    manager.mark_read()
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

from pocketpaw.bus.events import SystemEvent

logger = logging.getLogger(__name__)

# ── Tunable constants ──────────────────────────────────────────────────────────

_CHECK_INTERVAL_SECONDS = 60
_MAX_STORED_ALERTS = 200
_DEFAULT_ERROR_SPIKE_THRESHOLD = 0.20  # 20 % error rate in last 24 h
_DEFAULT_TOOL_DEGRADATION_THRESHOLD = 0.30  # 30 % failure rate for any tool


# ── Alert severity ─────────────────────────────────────────────────────────────


class AlertSeverity:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ── In-memory alert store ──────────────────────────────────────────────────────


class AlertStore:
    """Fixed-size ring buffer of alert dicts with unread tracking."""

    def __init__(self, maxlen: int = _MAX_STORED_ALERTS) -> None:
        self._alerts: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._unread = 0

    def append(self, alert: dict[str, Any]) -> None:
        self._alerts.append(alert)
        self._unread += 1

    @property
    def unread_count(self) -> int:
        return self._unread

    def mark_read(self) -> None:
        """Reset unread counter and clear per-alert _unread flags."""
        self._unread = 0
        for alert in self._alerts:
            alert["_unread"] = False

    def list_alerts(
        self,
        *,
        limit: int = 50,
        unread_only: bool = False,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        since_dt = _parse_iso(since)
        for alert in reversed(self._alerts):
            if unread_only and not alert.get("_unread"):
                continue
            if since_dt:
                ts = _parse_iso(str(alert.get("timestamp") or ""))
                if ts and ts < since_dt:
                    continue
            # Return a copy without the internal _unread flag so the API
            # serialization layer never exposes implementation details.
            public = {k: v for k, v in alert.items() if k != "_unread"}
            results.append(public)
            if len(results) >= limit:
                break
        return results


# ── Helpers ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ── AlertManager ───────────────────────────────────────────────────────────────


class AlertManager:
    """Periodic threshold checker that publishes structured alert events."""

    def __init__(
        self,
        *,
        check_interval: float = _CHECK_INTERVAL_SECONDS,
        error_spike_threshold: float = _DEFAULT_ERROR_SPIKE_THRESHOLD,
        tool_degradation_threshold: float = _DEFAULT_TOOL_DEGRADATION_THRESHOLD,
    ) -> None:
        self._check_interval = check_interval
        self._error_spike_threshold = error_spike_threshold
        self._tool_degradation_threshold = tool_degradation_threshold

        self._store = AlertStore()
        self._task: asyncio.Task | None = None
        self._running = False
        self._subscribed = False

        # Dedup: remember which alert types were already in "active" state
        self._active_alert_types: set[str] = set()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def unread_count(self) -> int:
        return self._store.unread_count

    def mark_read(self) -> None:
        self._store.mark_read()

    def list_alerts(
        self,
        *,
        limit: int = 50,
        unread_only: bool = False,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.list_alerts(limit=limit, unread_only=unread_only, since=since)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._subscribe()
        self._task = asyncio.create_task(self._check_loop(), name="alert-manager-check")
        logger.info("AlertManager started (interval=%ds)", self._check_interval)

    async def stop(self) -> None:
        self._running = False
        await self._unsubscribe()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("AlertManager stopped")

    # ── Bus subscription for channel_connected/disconnected events ────────────

    async def _subscribe(self) -> None:
        if self._subscribed:
            return
        try:
            from pocketpaw.bus import get_message_bus

            bus = get_message_bus()
            bus.subscribe_system(self._on_system_event)
            self._subscribed = True
        except Exception:
            logger.debug("AlertManager bus subscription failed", exc_info=True)

    async def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        try:
            from pocketpaw.bus import get_message_bus

            bus = get_message_bus()
            bus.unsubscribe_system(self._on_system_event)
        except Exception:
            pass
        self._subscribed = False

    async def _on_system_event(self, event: SystemEvent) -> None:
        """React to channel_disconnected events immediately (don't wait for next check)."""
        if event.event_type == "channel_disconnected":
            data = event.data or {}
            channel = str(data.get("channel") or "unknown")
            reason = str(data.get("reason") or "adapter stopped")
            await self._emit_alert(
                alert_type="channel_disconnect",
                severity=AlertSeverity.WARNING,
                message=f"Channel '{channel}' disconnected: {reason}",
                details=data,
            )

    # ── Check loop ────────────────────────────────────────────────────────────

    async def _check_loop(self) -> None:
        # Small initial delay so the server finishes startup before first check
        await asyncio.sleep(10)
        while self._running:
            try:
                await self._run_checks()
            except Exception:
                logger.debug("AlertManager check loop error", exc_info=True)
            await asyncio.sleep(self._check_interval)

    async def _run_checks(self) -> None:
        await asyncio.gather(
            self._check_error_spike(),
            self._check_tool_degradation(),
            self._check_budget(),
            return_exceptions=True,
        )

    # ── Individual checks ─────────────────────────────────────────────────────

    async def _check_error_spike(self) -> None:
        try:
            from pocketpaw.analytics import get_health_analytics

            health = await get_health_analytics()
            error_rate = float(health.get("error_rate_24h") or 0.0)
        except Exception:
            return

        alert_type = "error_spike"
        if error_rate >= self._error_spike_threshold:
            if alert_type not in self._active_alert_types:
                self._active_alert_types.add(alert_type)
                await self._emit_alert(
                    alert_type=alert_type,
                    severity=AlertSeverity.WARNING,
                    message=(
                        f"Error spike detected: {error_rate * 100:.1f}% of requests "
                        f"in the last 24 h failed (threshold: "
                        f"{self._error_spike_threshold * 100:.0f}%)"
                    ),
                    details={"error_rate_24h": error_rate},
                )
        else:
            self._active_alert_types.discard(alert_type)

    async def _check_tool_degradation(self) -> None:
        try:
            from pocketpaw.analytics import get_performance_analytics

            perf = await get_performance_analytics("day")
            tool_rows = perf.get("tool_performance") or []
        except Exception:
            return

        for tool in tool_rows:
            failure_rate = float(tool.get("failure_rate") or 0.0)
            name = str(tool.get("name") or "unknown")
            count = int(tool.get("count") or 0)
            if count < 5:
                # Not enough samples
                continue
            alert_type = f"tool_degradation:{name}"
            if failure_rate >= self._tool_degradation_threshold:
                if alert_type not in self._active_alert_types:
                    self._active_alert_types.add(alert_type)
                    await self._emit_alert(
                        alert_type="tool_degradation",
                        severity=AlertSeverity.WARNING,
                        message=(
                            f"Tool '{name}' is degraded: {failure_rate * 100:.1f}% failure rate "
                            f"({count} calls today)"
                        ),
                        details={"tool": name, "failure_rate": failure_rate, "count": count},
                    )
            else:
                self._active_alert_types.discard(alert_type)

    async def _check_budget(self) -> None:
        try:
            from pocketpaw.budget import sync_budget_state
            from pocketpaw.config import Settings

            settings = Settings.load()
            snapshot, _ = sync_budget_state(settings)
        except Exception:
            return

        if snapshot.level == "exhausted":
            alert_type = "budget_exhausted"
            if alert_type not in self._active_alert_types:
                self._active_alert_types.add(alert_type)
                self._active_alert_types.discard("budget_warning")
                await self._emit_alert(
                    alert_type=alert_type,
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Budget exhausted: ${snapshot.spent_usd:.4f} spent against "
                        f"${snapshot.effective_cap_usd or 0:.4f} cap"
                    ),
                    details=snapshot.to_dict(),
                )
        elif snapshot.level == "warning":
            alert_type = "budget_warning"
            self._active_alert_types.discard("budget_exhausted")
            if alert_type not in self._active_alert_types:
                self._active_alert_types.add(alert_type)
                await self._emit_alert(
                    alert_type=alert_type,
                    severity=AlertSeverity.WARNING,
                    message=(
                        f"Budget warning: {snapshot.percent_used:.1f}% used "
                        f"(${snapshot.spent_usd:.4f} / "
                        f"${snapshot.effective_cap_usd or 0:.4f})"
                    ),
                    details=snapshot.to_dict(),
                )
        else:
            self._active_alert_types.discard("budget_warning")
            self._active_alert_types.discard("budget_exhausted")

    # ── Emit helpers ──────────────────────────────────────────────────────────

    async def _emit_alert(
        self,
        *,
        alert_type: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        timestamp = _now_iso()
        alert_data: dict[str, Any] = {
            "alert_type": alert_type,
            "severity": severity,
            "message": message,
            "details": details or {},
            "timestamp": timestamp,
        }
        # Store locally
        self._store.append({**alert_data, "_unread": True})
        logger.info("Alert [%s/%s]: %s", severity, alert_type, message)

        # Publish to bus so WS clients / dashboard receive it live
        try:
            from pocketpaw.bus import get_message_bus

            bus = get_message_bus()
            await bus.publish_system(
                SystemEvent(
                    event_type="alert",
                    data=alert_data,
                )
            )
        except Exception:
            logger.debug("AlertManager bus publish failed", exc_info=True)

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "unread_count": self._store.unread_count,
            "active_alert_types": sorted(self._active_alert_types),
        }


# ── Singleton ──────────────────────────────────────────────────────────────────

_alert_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()

        from pocketpaw.lifecycle import register

        def _reset() -> None:
            global _alert_manager
            _alert_manager = None

        register("alert_manager", reset=_reset)
    return _alert_manager
