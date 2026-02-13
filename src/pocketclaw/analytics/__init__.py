"""Agent Usage Analytics & Insights.

Lightweight analytics system that hooks into the MessageBus to track
tool usage patterns, response times, error rates, and session statistics.

Created: 2026-02-13
"""

from pocketclaw.analytics.collector import AnalyticsCollector

__all__ = ["AnalyticsCollector", "get_analytics_collector"]

_collector: AnalyticsCollector | None = None


def get_analytics_collector() -> AnalyticsCollector:
    """Get the global analytics collector instance."""
    global _collector
    if _collector is None:
        _collector = AnalyticsCollector()

        from pocketclaw.lifecycle import register

        def _reset():
            global _collector
            _collector = None

        register("analytics_collector", reset=_reset)
    return _collector
