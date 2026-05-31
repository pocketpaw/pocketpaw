"""Channel health store — tracks adapter connect/disconnect timeline.

Subscribes to bus events:
    channel_connected    — emitted by dashboard_channels when adapter starts
    channel_disconnected — emitted by dashboard_channels when adapter stops

Provides:
    get_timeline(channel=None, limit=100) -> list[dict]
    get_uptime_stats()                    -> dict[channel, {uptime_pct, …}]
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw.bus.events import SystemEvent

logger = logging.getLogger(__name__)

_MAX_TIMELINE_EVENTS = 500
_UPTIME_WINDOW_DAYS = 7


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class ChannelHealthStore:
    """Ring-buffer timeline of channel connect/disconnect events."""

    def __init__(self, maxlen: int = _MAX_TIMELINE_EVENTS) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        # channel -> last_connected timestamp (datetime | None)
        self._connected_since: dict[str, datetime | None] = {}
        self._subscribed = False

    async def subscribe(self) -> None:
        if self._subscribed:
            return
        try:
            from pocketpaw.bus import get_message_bus

            bus = get_message_bus()
            bus.subscribe_system(self._on_event)
            self._subscribed = True
            logger.info("ChannelHealthStore subscribed")
        except Exception:
            logger.debug("ChannelHealthStore subscribe failed", exc_info=True)

    async def unsubscribe(self) -> None:
        if not self._subscribed:
            return
        try:
            from pocketpaw.bus import get_message_bus

            bus = get_message_bus()
            bus.unsubscribe_system(self._on_event)
        except Exception:
            pass
        self._subscribed = False

    async def _on_event(self, event: SystemEvent) -> None:
        data = event.data or {}
        channel = str(data.get("channel") or "unknown")
        timestamp = str(data.get("timestamp") or _now_iso())

        if event.event_type == "channel_connected":
            entry: dict[str, Any] = {
                "event": "connected",
                "channel": channel,
                "adapter": str(data.get("adapter") or ""),
                "timestamp": timestamp,
            }
            self._events.append(entry)
            self._connected_since[channel] = _parse_iso(timestamp) or datetime.now(tz=UTC)

        elif event.event_type == "channel_disconnected":
            entry = {
                "event": "disconnected",
                "channel": channel,
                "adapter": str(data.get("adapter") or ""),
                "timestamp": timestamp,
                "reason": str(data.get("reason") or ""),
            }
            self._events.append(entry)
            self._connected_since[channel] = None

    def get_timeline(
        self,
        channel: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return events newest-first, optionally filtered by channel."""
        results: list[dict[str, Any]] = []
        for evt in reversed(self._events):
            if channel and evt.get("channel") != channel:
                continue
            results.append(evt)
            if len(results) >= limit:
                break
        return results

    def get_uptime_stats(self) -> dict[str, dict[str, Any]]:
        """Per-channel uptime stats over the last 7 days."""
        now = datetime.now(tz=UTC)
        window_start = now - timedelta(days=_UPTIME_WINDOW_DAYS)
        window_seconds = _UPTIME_WINDOW_DAYS * 86400.0

        # Build channel -> sorted events list within window
        by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for evt in self._events:
            ts = _parse_iso(str(evt.get("timestamp") or ""))
            if ts and ts >= window_start:
                by_channel[evt["channel"]].append(evt)

        stats: dict[str, dict[str, Any]] = {}
        for ch, events in by_channel.items():
            sorted_evts = sorted(events, key=lambda e: str(e.get("timestamp") or ""))
            up_ms = 0.0
            last_conn: datetime | None = None

            for evt in sorted_evts:
                ts = _parse_iso(str(evt.get("timestamp") or "")) or now
                if evt["event"] == "connected":
                    last_conn = ts
                elif evt["event"] == "disconnected" and last_conn is not None:
                    up_ms += (ts - last_conn).total_seconds() * 1000
                    last_conn = None

            # If still connected, count up to now
            if last_conn is not None:
                up_ms += (now - last_conn).total_seconds() * 1000

            total_ms = window_seconds * 1000
            uptime_pct = round(min(up_ms / total_ms * 100, 100.0), 2) if total_ms > 0 else 0.0
            stats[ch] = {
                "channel": ch,
                "uptime_percent": uptime_pct,
                "up_ms": round(up_ms),
                "down_ms": round(max(total_ms - up_ms, 0)),
                "currently_connected": self._connected_since.get(ch) is not None,
                "connected_since": (
                    self._connected_since[ch].isoformat() if self._connected_since.get(ch) else None
                ),
            }

        return stats

    def snapshot(self) -> dict[str, Any]:
        return {
            "subscribed": self._subscribed,
            "event_count": len(self._events),
            "tracked_channels": list(self._connected_since.keys()),
        }


# ── Singleton ──────────────────────────────────────────────────────────────────

_channel_health_store: ChannelHealthStore | None = None


def get_channel_health_store() -> ChannelHealthStore:
    global _channel_health_store
    if _channel_health_store is None:
        _channel_health_store = ChannelHealthStore()

        from pocketpaw.lifecycle import register

        def _reset() -> None:
            global _channel_health_store
            _channel_health_store = None

        register("channel_health_store", reset=_reset)
    return _channel_health_store
