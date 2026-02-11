"""
ContextHub - Gathers system context for intention execution.

Provides context sources that can be included in intention prompts:
- system_status: CPU, RAM, disk, battery info
- datetime: Current date and time
- active_window: (Phase 2) Currently focused application

Template variables in prompts (e.g., {{system_status}}) are replaced
with gathered context before execution.
"""

import logging
import platform
import re
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class ContextHub:
    """
    Gathers context information for intention execution.

    Context sources can be enabled per-intention to customize
    what information is included in the prompt.
    """

    AVAILABLE_SOURCES = [
        "system_status",
        "datetime",
        # Phase 2:
        # "active_window",
        # "recent_files",
    ]

    def __init__(self):
        self._cache: dict[str, tuple[datetime, Any]] = {}
        self._cache_ttl = 5  # seconds

    async def gather(self, sources: list[str] | None = None) -> dict[str, Any]:
        """
        Gather context from specified sources.

        Args:
            sources: List of context source names.
                     If None, gathers from all available sources.

        Returns:
            Dict mapping source names to their context data
        """
        if sources is None:
            sources = self.AVAILABLE_SOURCES

        context = {}

        for source in sources:
            if source not in self.AVAILABLE_SOURCES:
                logger.warning(f"Unknown context source: {source}")
                continue

            try:
                context[source] = await self._gather_source(source)
            except Exception as e:
                logger.error(f"Error gathering context from {source}: {e}")
                context[source] = f"Error: {e}"

        return context

    async def _gather_source(self, source: str) -> Any:
        """Gather context from a single source."""
        # Check cache
        if source in self._cache:
            cached_time, cached_value = self._cache[source]
            age = (datetime.now(tz=UTC) - cached_time).total_seconds()
            if age < self._cache_ttl:
                return cached_value

        # Gather fresh data
        gatherers = {
            "system_status": self._gather_system_status,
            "datetime": self._gather_datetime,
            # Phase 2:
            # "active_window": self._gather_active_window,
        }

        gatherer = gatherers.get(source)
        if gatherer:
            value = await gatherer()
            self._cache[source] = (datetime.now(tz=UTC), value)
            return value

        return None

    async def _gather_system_status(self) -> dict:
        """Gather system status information."""
        try:
            import psutil
        except ImportError:
            return {
                "platform": platform.system(),
                "hostname": platform.node(),
                "error": "psutil not installed — pip install 'pocketpaw[desktop]'",
            }

        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            status = {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_gb": round(memory.available / (1024**3), 1),
                "memory_total_gb": round(memory.total / (1024**3), 1),
                "disk_percent": disk.percent,
                "disk_free_gb": round(disk.free / (1024**3), 1),
                "platform": platform.system(),
                "hostname": platform.node(),
            }

            # Battery info (if available)
            battery = psutil.sensors_battery()
            if battery:
                status["battery_percent"] = battery.percent
                status["battery_plugged"] = battery.power_plugged
            else:
                status["battery_percent"] = None
                status["battery_plugged"] = None

            return status

        except Exception as e:
            logger.error(f"Error getting system status: {e}")
            return {"error": str(e)}

    async def _gather_datetime(self) -> dict:
        """Gather current date and time information."""
        now = datetime.now(tz=UTC)

        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "day_of_week": now.strftime("%A"),
            "iso": now.isoformat(),
            "timestamp": int(now.timestamp()),
        }

    def format_context_string(self, context: dict[str, Any]) -> str:
        """
        Format context dict as a readable string for inclusion in prompts.

        Args:
            context: Dict of context data

        Returns:
            Formatted string representation
        """
        parts = []

        for source, data in context.items():
            if source == "system_status" and isinstance(data, dict):
                parts.append(self._format_system_status(data))
            elif source == "datetime" and isinstance(data, dict):
                parts.append(self._format_datetime(data))
            else:
                parts.append(f"[{source}]\n{data}")

        return "\n\n".join(parts)

    def _format_system_status(self, status: dict) -> str:
        """Format system status as readable string."""
        if "error" in status:
            return f"[System Status]\nError: {status['error']}"

        lines = [
            "[System Status]",
            f"CPU: {status.get('cpu_percent', '?')}%",
            f"Memory: {status.get('memory_percent', '?')}% "
            f"({status.get('memory_available_gb', '?')}GB free of {status.get('memory_total_gb', '?')}GB)",
            f"Disk: {status.get('disk_percent', '?')}% used "
            f"({status.get('disk_free_gb', '?')}GB free)",
        ]

        if status.get("battery_percent") is not None:
            charging = " (charging)" if status.get("battery_plugged") else ""
            lines.append(f"Battery: {status['battery_percent']}%{charging}")

        return "\n".join(lines)

    def _format_datetime(self, dt: dict) -> str:
        """Format datetime as readable string."""
        return (
            f"[Current Time]\n"
            f"{dt.get('day_of_week', '')}, {dt.get('date', '')} {dt.get('time', '')}"
        )

    def apply_template(self, prompt: str, context: dict[str, Any]) -> str:
        """
        Apply context to a prompt template.

        Replaces {{variable}} placeholders with context values.

        Args:
            prompt: Prompt template with optional {{variable}} placeholders
            context: Dict of context data

        Returns:
            Prompt with placeholders replaced
        """
        result = prompt

        # Replace {{context}} with full formatted context
        if "{{context}}" in result:
            formatted = self.format_context_string(context)
            result = result.replace("{{context}}", formatted)

        # Replace individual {{source}} placeholders
        for source, data in context.items():
            placeholder = "{{" + source + "}}"
            if placeholder in result:
                if source == "system_status":
                    formatted = self._format_system_status(data)
                elif source == "datetime":
                    formatted = self._format_datetime(data)
                else:
                    formatted = str(data)
                result = result.replace(placeholder, formatted)

        # Replace any {{key}} with nested values if data is dict
        pattern = r"\{\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\}\}"
        matches = re.findall(pattern, result)

        for match in matches:
            value = self._get_nested_value(context, match)
            if value is not None:
                result = result.replace("{{" + match + "}}", str(value))

        return result

    def _get_nested_value(self, data: dict, path: str) -> Any | None:
        """Get a nested value from dict using dot notation."""
        parts = path.split(".")
        current = data

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current


# Module-level instance for convenience
_context_hub: ContextHub | None = None


def get_context_hub() -> ContextHub:
    """Get singleton ContextHub instance."""
    global _context_hub
    if _context_hub is None:
        _context_hub = ContextHub()
    return _context_hub
