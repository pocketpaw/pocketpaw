# HealthEngine — orchestrates checks, stores state, provides prompt injection.
# Created: 2026-02-17
# LLM-independent: all checks are pure Python. Agent consumes results, not produces.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw.health.checks import (
    CONNECTIVITY_CHECKS,
    STARTUP_CHECKS,
    HealthCheckResult,
)
from pocketpaw.health.store import ErrorStore

logger = logging.getLogger(__name__)


class HealthEngine:
    """Central health engine — orchestrates checks and error storage.

    Pure Python, no LLM dependency. The agent is a *consumer* of health state.
    If the LLM is down, the health engine still works.
    """

    def __init__(self):
        self._results: list[HealthCheckResult] = []
        self._error_store = ErrorStore()
        self._last_check: str = ""

    # =========================================================================
    # Run checks
    # =========================================================================

    def run_startup_checks(self) -> list[HealthCheckResult]:
        """Run all sync config + storage checks. Fast, never blocks."""
        results: list[HealthCheckResult] = []
        for check_fn in STARTUP_CHECKS:
            try:
                result = check_fn()
                results.append(result)
            except Exception as e:
                logger.warning("Health check %s failed: %s", check_fn.__name__, e)
                results.append(
                    HealthCheckResult(
                        check_id=check_fn.__name__.replace("check_", ""),
                        name=check_fn.__name__.replace("check_", "").replace("_", " ").title(),
                        category="config",
                        status="warning",
                        message=f"Check itself failed: {e}",
                        fix_hint="",
                    )
                )
        self._results = results
        self._last_check = datetime.now(tz=UTC).isoformat()
        return results

    async def run_connectivity_checks(self) -> list[HealthCheckResult]:
        """Run async connectivity checks (background, non-blocking)."""
        results: list[HealthCheckResult] = []
        for check_fn in CONNECTIVITY_CHECKS:
            try:
                result = await check_fn()
                results.append(result)
            except Exception as e:
                logger.warning("Connectivity check %s failed: %s", check_fn.__name__, e)
                results.append(
                    HealthCheckResult(
                        check_id=check_fn.__name__.replace("check_", ""),
                        name=check_fn.__name__.replace("check_", "").replace("_", " ").title(),
                        category="connectivity",
                        status="warning",
                        message=f"Check itself failed: {e}",
                        fix_hint="",
                    )
                )

        # Merge with existing results (replace by check_id)
        existing_ids = {r.check_id for r in results}
        self._results = [r for r in self._results if r.check_id not in existing_ids] + results
        self._last_check = datetime.now(tz=UTC).isoformat()
        return results

    async def run_all_checks(self) -> list[HealthCheckResult]:
        """Run startup + connectivity checks."""
        self.run_startup_checks()
        await self.run_connectivity_checks()
        return list(self._results)

    # =========================================================================
    # Status properties
    # =========================================================================

    @property
    def results(self) -> list[HealthCheckResult]:
        return list(self._results)

    @property
    def overall_status(self) -> str:
        """Compute overall status: 'healthy' / 'degraded' / 'unhealthy'."""
        if not self._results:
            return "healthy"

        has_critical = any(r.status == "critical" for r in self._results)
        has_warning = any(r.status == "warning" for r in self._results)

        if has_critical:
            return "unhealthy"
        if has_warning:
            return "degraded"
        return "healthy"

    @property
    def summary(self) -> dict:
        """Compact dict for API/WebSocket."""
        issues = [r.to_dict() for r in self._results if r.status != "ok"]
        return {
            "status": self.overall_status,
            "check_count": len(self._results),
            "issues": issues,
            "last_check": self._last_check,
        }

    # =========================================================================
    # System prompt injection
    # =========================================================================

    def get_health_prompt_section(self) -> str:
        """Return system prompt block (only when degraded/unhealthy).

        Returns empty string when healthy (saves context window).
        """
        status = self.overall_status
        if status == "healthy":
            return ""

        lines = [
            "# System Health Status",
            f"System is currently: {status.upper()}",
            "",
            "Known issues:",
        ]

        for r in self._results:
            if r.status == "ok":
                continue
            severity = r.status.upper()
            lines.append(f"- [{severity}] {r.name}: {r.message}")
            if r.fix_hint:
                lines.append(f"  Fix: {r.fix_hint}")

        lines.append("")
        lines.append("If the user reports problems, check these issues first.")
        lines.append(
            "Use the `health_check` tool for diagnostics and `error_log` for recent errors."
        )
        return "\n".join(lines)

    # =========================================================================
    # Error recording (delegates to ErrorStore)
    # =========================================================================

    def record_error(
        self,
        message: str,
        source: str = "unknown",
        severity: str = "error",
        traceback: str = "",
        context: dict | None = None,
    ) -> str:
        """Record an error to the persistent store. Returns error ID.

        Safe to call from anywhere — never raises.
        """
        try:
            return self._error_store.record(
                message=message,
                source=source,
                severity=severity,
                traceback=traceback,
                context=context,
            )
        except Exception as e:
            logger.warning("Failed to record error: %s", e)
            return ""

    def get_recent_errors(self, limit: int = 20, search: str = "") -> list[dict]:
        """Get recent errors from persistent store."""
        try:
            return self._error_store.get_recent(limit=limit, search=search)
        except Exception:
            return []

    @property
    def error_store(self) -> ErrorStore:
        return self._error_store
