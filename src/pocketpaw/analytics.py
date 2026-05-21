"""Analytics aggregation helpers for cost, performance, usage, and health.

All blocking file I/O (usage_tracker reads, trace file reads) is delegated to
asyncio.to_thread() to comply with the project's async I/O guideline.
"""

from __future__ import annotations

import asyncio
import calendar
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw.config import get_config_dir
from pocketpaw.health import get_health_engine
from pocketpaw.traces import get_trace_store
from pocketpaw.usage_tracker import get_usage_tracker


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _period_window(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    if period == "day":
        start = datetime(current.year, current.month, current.day, tzinfo=UTC)
    elif period == "week":
        start = current - timedelta(days=7)
    elif period == "month":
        start = datetime(current.year, current.month, 1, tzinfo=UTC)
    else:
        raise ValueError("period must be one of: day, week, month")
    return start, current


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    index = (len(ordered) - 1) * max(0.0, min(1.0, p))
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    frac = index - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _trace_started_at(trace: dict[str, Any]) -> str:
    inbound = trace.get("inbound") if isinstance(trace.get("inbound"), dict) else {}
    return str(trace.get("started_at") or inbound.get("timestamp") or "")


def _trace_total(trace: dict[str, Any]) -> dict[str, Any]:
    value = trace.get("total")
    if isinstance(value, dict):
        return value
    return {}


def _trace_cost(trace: dict[str, Any]) -> float:
    try:
        return float(_trace_total(trace).get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _trace_duration_ms(trace: dict[str, Any]) -> float:
    try:
        return float(_trace_total(trace).get("duration_ms") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _trace_llm_calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    llm_calls = trace.get("llm_calls")
    return llm_calls if isinstance(llm_calls, list) else []


def _trace_tool_calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = trace.get("tool_calls")
    return tool_calls if isinstance(tool_calls, list) else []


def _as_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Helpers that wrap blocking calls in to_thread
# ---------------------------------------------------------------------------

# Maximum traces loaded per analytics pass.  Keep low to bound memory use;
# traces are ordered most-recent-first so recent data is always included.
_TRACE_SCAN_LIMIT = 5000


async def _get_usage_summary(since: str) -> dict[str, Any]:
    """Fetch usage summary without blocking the event loop."""
    tracker = get_usage_tracker()
    return await asyncio.to_thread(tracker.get_summary, since)


async def _get_usage_records(limit: int = 20000) -> list[Any]:
    """Fetch usage records without blocking the event loop."""
    tracker = get_usage_tracker()
    return await asyncio.to_thread(tracker.get_records, limit)


async def _get_traces(since: str, limit: int = _TRACE_SCAN_LIMIT) -> list[Any]:
    """Fetch full traces once; callers share the result to avoid duplicate scans."""
    return await get_trace_store().get_full_traces(since=since, limit=limit)


async def _measure_memory_dir() -> int:
    """Measure memory dir size without blocking the event loop."""

    def _measure() -> int:
        memory_dir = get_config_dir() / "memory"
        size = 0
        if memory_dir.exists():
            for path in memory_dir.rglob("*"):
                if path.is_file():
                    try:
                        size += path.stat().st_size
                    except OSError:
                        pass
        return size

    return await asyncio.to_thread(_measure)


# ---------------------------------------------------------------------------
# Cost analytics
# ---------------------------------------------------------------------------


async def get_cost_analytics(
    period: str,
    traces: list[Any] | None = None,
) -> dict[str, Any]:
    start, end = _period_window(period)
    since = _as_iso(start)

    if traces is not None:
        _traces = traces
        usage_summary = await _get_usage_summary(since)
    else:
        usage_summary, _traces = await asyncio.gather(
            _get_usage_summary(since),
            _get_traces(since),
        )

    channel_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"cost_usd": 0.0, "count": 0})
    daily_cost: Counter = Counter()
    daily_requests: Counter = Counter()
    # Per-tool cost attribution — estimated by splitting trace cost evenly
    # across tool calls, weighted by tool duration share.
    tool_cost_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost_usd": 0.0, "count": 0}
    )

    for trace in _traces:
        inbound = trace.get("inbound") if isinstance(trace.get("inbound"), dict) else {}
        channel = str(inbound.get("channel") or "unknown")
        cost = _trace_cost(trace)
        channel_totals[channel]["cost_usd"] += cost
        channel_totals[channel]["count"] += 1

        started_at = _trace_started_at(trace)
        day_key = started_at[:10] if started_at else ""
        if day_key:
            daily_cost[day_key] += cost
            daily_requests[day_key] += 1

        # Attribute cost by tool — split trace cost proportionally by duration
        tool_calls = _trace_tool_calls(trace)
        if tool_calls and cost > 0:
            total_tool_duration = sum(float(tc.get("duration_ms") or 0.0) for tc in tool_calls)
            for tc in tool_calls:
                name = str(tc.get("name") or "unknown")
                tc_duration = float(tc.get("duration_ms") or 0.0)
                if total_tool_duration > 0:
                    tc_cost = cost * (tc_duration / total_tool_duration)
                else:
                    tc_cost = cost / len(tool_calls)
                tool_cost_totals[name]["cost_usd"] += tc_cost
                tool_cost_totals[name]["count"] += 1
        elif tool_calls:
            for tc in tool_calls:
                name = str(tc.get("name") or "unknown")
                tool_cost_totals[name]["count"] += 1

    total_cost = float(usage_summary.get("total_cost_usd") or 0.0)
    elapsed_hours = max((end - start).total_seconds() / 3600.0, 1 / 3600.0)
    burn_rate = total_cost / elapsed_hours
    days_in_month = calendar.monthrange(end.year, end.month)[1]

    trend = [
        {
            "date": day,
            "cost_usd": round(float(daily_cost[day]), 6),
            "requests": int(daily_requests[day]),
        }
        for day in sorted(daily_cost.keys())
    ]

    return {
        "period": period,
        "window_start": since,
        "window_end": _as_iso(end),
        "totals": {
            "cost_usd": round(total_cost, 6),
            "request_count": int(usage_summary.get("request_count") or 0),
            "input_tokens": int(usage_summary.get("total_input_tokens") or 0),
            "output_tokens": int(usage_summary.get("total_output_tokens") or 0),
            "cached_input_tokens": int(usage_summary.get("total_cached_input_tokens") or 0),
            "total_tokens": int(usage_summary.get("total_tokens") or 0),
            "burn_rate_usd_per_hour": round(burn_rate, 6),
            "projected_monthly_usd": round(burn_rate * 24 * days_in_month, 6),
        },
        "by_model": [
            {
                "model": model,
                "cost_usd": round(float(values.get("cost_usd") or 0.0), 6),
                "count": int(values.get("count") or 0),
                "input_tokens": int(values.get("input_tokens") or 0),
                "output_tokens": int(values.get("output_tokens") or 0),
            }
            for model, values in sorted(
                (usage_summary.get("by_model") or {}).items(),
                key=lambda item: float(item[1].get("cost_usd") or 0.0),
                reverse=True,
            )
        ],
        "by_channel": [
            {
                "channel": channel,
                "cost_usd": round(values["cost_usd"], 6),
                "count": int(values["count"]),
            }
            for channel, values in sorted(
                channel_totals.items(),
                key=lambda item: item[1]["cost_usd"],
                reverse=True,
            )
        ],
        # Cost by tool — amounts are proportional estimates based on duration share
        "by_tool": [
            {
                "name": name,
                "cost_usd": round(float(values["cost_usd"]), 6),
                "count": int(values["count"]),
            }
            for name, values in sorted(
                tool_cost_totals.items(),
                key=lambda item: item[1]["cost_usd"],
                reverse=True,
            )
        ],
        "daily_trend": trend,
    }


# ---------------------------------------------------------------------------
# Performance analytics
# ---------------------------------------------------------------------------


async def get_performance_analytics(
    period: str,
    traces: list[Any] | None = None,
) -> dict[str, Any]:
    start, end = _period_window(period)
    _traces = traces if traces is not None else await _get_traces(_as_iso(start))

    durations = [_trace_duration_ms(trace) for trace in _traces if _trace_duration_ms(trace) > 0]
    llm_calls_per_response = [len(_trace_llm_calls(trace)) for trace in _traces]

    cached_input_tokens = 0
    total_input_tokens = 0
    backend_latencies: dict[str, list[float]] = defaultdict(list)
    tools: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "failures": 0, "durations": []}
    )

    for trace in _traces:
        agent_start = trace.get("agent_start") if isinstance(trace.get("agent_start"), dict) else {}
        backend = str((agent_start or {}).get("backend") or "unknown")
        duration = _trace_duration_ms(trace)
        if duration > 0:
            backend_latencies[backend].append(duration)

        for llm_call in _trace_llm_calls(trace):
            cached_input_tokens += int(llm_call.get("cached_input_tokens") or 0)
            total_input_tokens += int(llm_call.get("input_tokens") or 0)

        for tool in _trace_tool_calls(trace):
            name = str(tool.get("name") or "unknown")
            duration_ms = float(tool.get("duration_ms") or 0.0)
            success = bool(tool.get("success", True))
            tools[name]["count"] += 1
            if not success:
                tools[name]["failures"] += 1
            if duration_ms > 0:
                tools[name]["durations"].append(duration_ms)

    cache_hit_rate = 0.0
    if total_input_tokens > 0:
        cache_hit_rate = cached_input_tokens / total_input_tokens

    tool_rows = []
    for name, values in sorted(tools.items(), key=lambda item: item[1]["count"], reverse=True):
        durations_for_tool = values["durations"]
        avg_duration = (
            sum(durations_for_tool) / len(durations_for_tool) if durations_for_tool else 0.0
        )
        failure_rate = values["failures"] / max(values["count"], 1)
        tool_rows.append(
            {
                "name": name,
                "count": int(values["count"]),
                "avg_duration_ms": round(avg_duration, 2),
                "p95_duration_ms": round(_percentile(durations_for_tool, 0.95), 2),
                "failure_rate": round(failure_rate, 4),
                "success_rate": round(1.0 - failure_rate, 4),
            }
        )

    return {
        "period": period,
        "window_start": _as_iso(start),
        "window_end": _as_iso(end),
        "response_latency_ms": {
            "avg": round(sum(durations) / len(durations), 2) if durations else 0.0,
            "p50": round(_percentile(durations, 0.50), 2),
            "p95": round(_percentile(durations, 0.95), 2),
            "p99": round(_percentile(durations, 0.99), 2),
            "count": len(durations),
        },
        "llm_calls_per_response": {
            "avg": round(sum(llm_calls_per_response) / len(llm_calls_per_response), 3)
            if llm_calls_per_response
            else 0.0,
            "p50": round(_percentile([float(v) for v in llm_calls_per_response], 0.50), 2)
            if llm_calls_per_response
            else 0.0,
            "p95": round(_percentile([float(v) for v in llm_calls_per_response], 0.95), 2)
            if llm_calls_per_response
            else 0.0,
        },
        "cache_hit_rate": round(cache_hit_rate, 4),
        "by_backend": [
            {
                "backend": backend,
                "avg_duration_ms": round(sum(values) / len(values), 2),
                "p95_duration_ms": round(_percentile(values, 0.95), 2),
                "count": len(values),
            }
            for backend, values in sorted(
                backend_latencies.items(), key=lambda item: len(item[1]), reverse=True
            )
        ],
        "tool_performance": tool_rows,
    }


# ---------------------------------------------------------------------------
# Usage analytics
# ---------------------------------------------------------------------------


async def get_usage_analytics(
    period: str,
    traces: list[Any] | None = None,
) -> dict[str, Any]:
    start, end = _period_window(period)
    since = _as_iso(start)

    if traces is not None:
        _traces = traces
        usage_records = await _get_usage_records(limit=20000)
    else:
        _traces, usage_records = await asyncio.gather(
            _get_traces(since),
            _get_usage_records(limit=20000),
        )

    messages_per_day: Counter = Counter()
    messages_by_channel: Counter = Counter()
    active_sessions_by_day: dict[str, set[str]] = defaultdict(set)
    peak_hours: Counter = Counter()
    tool_counts: Counter = Counter()

    for trace in _traces:
        started_at = _trace_started_at(trace)
        day_key = started_at[:10] if started_at else ""
        hour_key = started_at[11:13] if len(started_at) >= 13 else ""

        inbound = trace.get("inbound") if isinstance(trace.get("inbound"), dict) else {}
        channel = str(inbound.get("channel") or "unknown")

        if day_key:
            messages_per_day[day_key] += 1
            active_sessions_by_day[day_key].add(str(trace.get("session_key") or ""))
        if hour_key:
            peak_hours[hour_key] += 1
        messages_by_channel[channel] += 1

        for tool in _trace_tool_calls(trace):
            tool_name = str(tool.get("name") or "unknown")
            tool_counts[tool_name] += 1

    token_trend: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "total_tokens": 0,
        }
    )
    for record in usage_records:
        ts = _parse_iso(record.timestamp)
        if ts is None or ts < start:
            continue
        day_key = ts.date().isoformat()
        token_trend[day_key]["input_tokens"] += int(record.input_tokens)
        token_trend[day_key]["output_tokens"] += int(record.output_tokens)
        token_trend[day_key]["cached_input_tokens"] += int(record.cached_input_tokens)
        token_trend[day_key]["total_tokens"] += int(record.total_tokens)

    return {
        "period": period,
        "window_start": since,
        "window_end": _as_iso(end),
        "totals": {
            "messages": int(sum(messages_per_day.values())),
            "active_sessions": len(
                {session for values in active_sessions_by_day.values() for session in values}
            ),
        },
        "messages_per_day": [
            {"date": day, "count": int(count)} for day, count in sorted(messages_per_day.items())
        ],
        "messages_by_channel": [
            {"channel": channel, "count": int(count)}
            for channel, count in messages_by_channel.most_common()
        ],
        "active_sessions_per_day": [
            {"date": day, "count": len(sessions)}
            for day, sessions in sorted(active_sessions_by_day.items())
        ],
        "peak_usage_hours": [
            {"hour": hour, "count": int(count)} for hour, count in sorted(peak_hours.items())
        ],
        "most_used_tools": [
            {"name": name, "count": int(count)} for name, count in tool_counts.most_common(20)
        ],
        "token_trend": [
            {
                "date": day,
                "input_tokens": values["input_tokens"],
                "output_tokens": values["output_tokens"],
                "cached_input_tokens": values["cached_input_tokens"],
                "total_tokens": values["total_tokens"],
            }
            for day, values in sorted(token_trend.items())
        ],
    }


# ---------------------------------------------------------------------------
# Health analytics
# ---------------------------------------------------------------------------


def _read_guardian_block_rate_sync(since: datetime) -> float:
    """Read guardian block rate from the audit JSONL log.

    Scans from the *tail* of the file (up to _AUDIT_TAIL_BYTES) so the read
    is bounded regardless of how large audit.jsonl grows.  Counts
    actor=``guardian`` decision events within the time window.
    Returns 0.0 on any error.
    """
    _AUDIT_TAIL_BYTES = 512 * 1024  # 512 KB — enough for ~24 h at typical rates
    try:
        from pathlib import Path

        audit_path = Path.home() / ".pocketpaw" / "audit.jsonl"
        if not audit_path.exists():
            return 0.0

        file_size = audit_path.stat().st_size
        seek_pos = max(0, file_size - _AUDIT_TAIL_BYTES)

        blocks = 0
        total_checks = 0
        with audit_path.open("rb") as fh:
            fh.seek(seek_pos)
            if seek_pos > 0:
                fh.readline()  # skip possible partial first line
            for raw in fh:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("actor") or "") != "guardian":
                    continue
                action = str(entry.get("action") or "")
                # Only count decision events (not pending scan_command entries)
                if action not in ("scan_result", "local_safety_check", "scan_error"):
                    continue
                ts = _parse_iso(str(entry.get("timestamp") or ""))
                if ts is None or ts < since:
                    continue
                total_checks += 1
                if str(entry.get("status") or "") == "block":
                    blocks += 1

        return round(blocks / max(total_checks, 1), 4)
    except Exception:
        return 0.0


async def get_health_analytics(
    traces: list[Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    day_start = now - timedelta(days=1)

    if traces is not None:
        _traces = traces
        memory_size_bytes = await _measure_memory_dir()
    else:
        _traces, memory_size_bytes = await asyncio.gather(
            _get_traces(_as_iso(day_start)),
            _measure_memory_dir(),
        )

    engine = get_health_engine()
    summary = engine.summary
    errors = engine.get_recent_errors(limit=500)

    recent_5m = 0
    recent_1h = 0
    for item in errors:
        ts = _parse_iso(str(item.get("timestamp") or ""))
        if ts is None:
            continue
        if ts >= now - timedelta(minutes=5):
            recent_5m += 1
        if ts >= now - timedelta(hours=1):
            recent_1h += 1

    error_traces = 0
    total_tool_calls = 0
    for trace in _traces:
        total = _trace_total(trace)
        status = str(total.get("status") or "ok")
        trace_errors = trace.get("errors") if isinstance(trace.get("errors"), list) else []
        if status != "ok" or trace_errors:
            error_traces += 1
        total_tool_calls += len(_trace_tool_calls(trace))

    error_rate_24h = error_traces / max(len(_traces), 1)

    # Guardian block rate — read directly from the audit log
    # (actor="guardian", status="block") over the last 24 h window.
    # This is the only reliable source; inferring from trace error strings
    # misses blocks logged via the local safety check or error paths.
    guardian_block_rate = await asyncio.to_thread(_read_guardian_block_rate_sync, day_start)

    sessions_by_channel: Counter = Counter()
    active_sessions = 0
    try:
        from pocketpaw.dashboard_state import status_tracker

        snap = status_tracker.snapshot()
        active_sessions = int((snap.get("global") or {}).get("active_sessions") or 0)
        for session in snap.get("sessions") or []:
            channel = str((session or {}).get("channel") or "unknown")
            sessions_by_channel[channel] += 1
    except Exception:
        pass

    # Channel uptime timeline from ChannelHealthStore
    channel_uptime: dict[str, Any] = {}
    channel_timeline: list[dict[str, Any]] = []
    try:
        from pocketpaw.channel_health_store import get_channel_health_store

        chs = get_channel_health_store()
        channel_uptime = chs.get_uptime_stats()
        channel_timeline = chs.get_timeline(limit=50)
    except Exception:
        pass

    return {
        "status": summary.get("status", "unknown"),
        "last_check": summary.get("last_check", ""),
        "issues": summary.get("issues", []),
        "check_count": summary.get("check_count", 0),
        "error_rate_24h": round(error_rate_24h, 4),
        "recent_errors": {
            "last_5m": recent_5m,
            "last_1h": recent_1h,
            "total": len(errors),
        },
        "guardian_block_rate": round(guardian_block_rate, 4),
        "channel_health": {
            "active_sessions": active_sessions,
            "by_channel": [
                {"channel": channel, "count": int(count)}
                for channel, count in sessions_by_channel.most_common()
            ],
            # Per-channel uptime stats (7-day window)
            "uptime": list(channel_uptime.values()),
            # Recent connect/disconnect timeline (newest first)
            "timeline": channel_timeline,
        },
        "memory_store_size_bytes": memory_size_bytes,
    }


# ---------------------------------------------------------------------------
# Combined aggregator — single trace scan for dashboard full-refresh
# ---------------------------------------------------------------------------


async def get_all_analytics(period: str = "day") -> dict[str, Any]:
    """Fetch all four analytics views in one pass.

    Loads the trace file exactly once and passes the result to each
    sub-function, eliminating the 4x concurrent unbounded trace scans
    that happen when the frontend fires Promise.all every 30 s.
    """
    start, _ = _period_window(period)
    since = _as_iso(start)
    # Health always uses a 24-hour window regardless of period.
    health_start = _as_iso(datetime.now(tz=UTC) - timedelta(days=1))
    # Use the wider window for the shared trace fetch.
    fetch_since = since if since < health_start else health_start

    shared_traces, usage_summary, usage_records, memory_size_bytes = await asyncio.gather(
        _get_traces(fetch_since),
        _get_usage_summary(since),
        _get_usage_records(limit=20000),
        _measure_memory_dir(),
    )

    # Partition traces into period-window and 24-h-health-window slices.
    health_traces = [t for t in shared_traces if (_trace_started_at(t) or "") >= health_start]
    period_traces = [t for t in shared_traces if (_trace_started_at(t) or "") >= since]

    cost, performance, usage, health = await asyncio.gather(
        get_cost_analytics(period, traces=period_traces),
        get_performance_analytics(period, traces=period_traces),
        get_usage_analytics(period, traces=period_traces),
        get_health_analytics(traces=health_traces),
    )
    # Inject the already-fetched usage_summary / usage_records so sub-functions
    # don't re-fetch.  They already used them when traces were pre-supplied.
    return {
        "cost": cost,
        "performance": performance,
        "usage": usage,
        "health": health,
    }
