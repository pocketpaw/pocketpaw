# Health diagnostic tools — allow agent to check system health and read error logs.
# Created: 2026-02-17
# Part of Health Engine (Phase 1).

from typing import Any

from pocketpaw.tools.protocol import BaseTool


class HealthCheckTool(BaseTool):
    """Run system health diagnostics and return check results with fix hints."""

    @property
    def name(self) -> str:
        return "health_check"

    @property
    def description(self) -> str:
        return (
            "Run system health diagnostics. Returns check results for config, "
            "connectivity, and storage with status (ok/warning/critical) and fix hints. "
            "Use this when the user reports problems or asks about system status."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_connectivity": {
                    "type": "boolean",
                    "description": "Also run connectivity checks (slower, tests LLM API). Default: false.",
                    "default": False,
                },
            },
            "required": [],
        }

    async def execute(self, include_connectivity: bool = False) -> str:
        try:
            from pocketpaw.health import get_health_engine

            engine = get_health_engine()
            results = engine.run_startup_checks()

            if include_connectivity:
                conn_results = await engine.run_connectivity_checks()
                results = engine.results  # merged

            lines = [f"System Status: {engine.overall_status.upper()}\n"]
            for r in results:
                icon = {"ok": "[OK]", "warning": "[WARN]", "critical": "[FAIL]"}.get(
                    r.status, "[?]"
                )
                lines.append(f"{icon} {r.name}: {r.message}")
                if r.status != "ok" and r.fix_hint:
                    lines.append(f"    Fix: {r.fix_hint}")

            lines.append(f"\nTotal: {len(results)} checks")
            return "\n".join(lines)
        except Exception as e:
            return self._error(f"Health check failed: {e}")


class ErrorLogTool(BaseTool):
    """Read recent persistent errors from the health error log."""

    @property
    def name(self) -> str:
        return "error_log"

    @property
    def description(self) -> str:
        return (
            "Read recent errors from the persistent error log. Errors are stored across "
            "sessions and survive page refresh. Use this to find what went wrong when "
            "the user reports a failure."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max number of errors to return (default: 10)",
                    "default": 10,
                },
                "search": {
                    "type": "string",
                    "description": "Optional text to filter errors by (searches message, source, traceback)",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(self, limit: int = 10, search: str = "") -> str:
        try:
            from pocketpaw.health import get_health_engine

            engine = get_health_engine()
            errors = engine.get_recent_errors(limit=limit, search=search)

            if not errors:
                return "No errors found in the error log."

            lines = [f"Recent Errors ({len(errors)} found):\n"]
            for err in errors:
                ts = err.get("timestamp", "")[:19]  # trim microseconds
                source = err.get("source", "unknown")
                severity = err.get("severity", "error")
                msg = err.get("message", "")
                lines.append(f"[{ts}] [{severity.upper()}] {source}: {msg}")

                tb = err.get("traceback", "")
                if tb:
                    # Show last 3 lines of traceback
                    tb_lines = tb.strip().splitlines()[-3:]
                    for tl in tb_lines:
                        lines.append(f"    {tl}")
                lines.append("")

            return "\n".join(lines)
        except Exception as e:
            return self._error(f"Failed to read error log: {e}")


class ConfigDoctorTool(BaseTool):
    """Validate config and return playbook-backed diagnosis."""

    @property
    def name(self) -> str:
        return "config_doctor"

    @property
    def description(self) -> str:
        return (
            "Validate configuration and return a diagnostic report with playbook-backed "
            "fix suggestions. Optionally focus on a specific section: 'api_keys', 'backend', "
            "or 'storage'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Focus area: 'api_keys', 'backend', 'storage', or '' for all",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(self, section: str = "") -> str:
        try:
            from pocketpaw.health.playbooks import diagnose_config

            return diagnose_config(section=section)
        except Exception as e:
            return self._error(f"Config doctor failed: {e}")
