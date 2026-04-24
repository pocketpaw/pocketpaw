---
{
  "title": "System Status Tool: Real-Time Hardware Metrics with psutil Fallback",
  "summary": "The system status tool collects CPU, memory, disk, battery, and uptime metrics using psutil and formats them as a human-readable markdown string for display in the agent UI. When psutil is not installed, it falls back to a limited report using the standard library `platform` module, preserving basic utility without requiring the desktop extras.",
  "concepts": [
    "system status",
    "psutil",
    "CPU metrics",
    "memory metrics",
    "disk metrics",
    "battery status",
    "uptime",
    "platform module",
    "desktop extras",
    "interval=0"
  ],
  "categories": [
    "tools",
    "system monitoring",
    "desktop integration"
  ],
  "source_docs": [
    "e69fb473bdc1a75c"
  ],
  "backlinks": null,
  "word_count": 330,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw agents often run in resource-constrained or shared environments where users want visibility into the host system's health. The `status.py` module provides a lightweight system interrogation function that produces a formatted status report usable directly in agent conversations or dashboard panels.

## Two-Tier Availability

The function handles two availability levels:

**Without psutil** (minimal): Uses only `platform.system()` and `platform.machine()` from the Python standard library. Returns a reduced status string with a recommendation to install the `pocketpaw[desktop]` extra.

**With psutil** (full): Collects six categories of system metrics:

- **CPU**: Percent utilization and core count. Uses `interval=0` to avoid blocking.
- **Memory**: Used and total RAM in GiB, plus utilization percentage.
- **Disk**: Used and total storage on the root filesystem (`/`), plus utilization percentage.
- **Uptime**: Calculated from `psutil.boot_time()` and current UTC time, formatted as `HH:MM:SS`.
- **Battery**: Conditionally included if `psutil.sensors_battery()` returns data. The battery block is omitted on machines without batteries.
- **Platform**: System name and machine architecture.

## The interval=0 Choice

```python
cpu_percent = psutil.cpu_percent(interval=0)
```

Using `interval=0` returns the cached CPU percentage from the last poll rather than performing a new blocking sample. psutil's default behavior with `interval=None` blocks for 0.1 seconds to sample CPU, which is unacceptable in an async context. The tradeoff is a potentially slightly stale value, but the function returns immediately -- essential for an agent tool called on demand.

## Formatted Output

The return value is a markdown-formatted string with emoji indicators and bold headings, designed for rendering in PocketPaw's dashboard chat interface rather than for machine parsing. No structured data (dict, dataclass) is returned because the tool protocol expects string results.

## Known Gaps

Disk usage is always measured against `/` (root filesystem). On Windows or multi-partition Linux systems, the most relevant disk may not be the root partition. The battery section is wrapped in a broad `except Exception: pass`, which means battery read failures are silently hidden. Network I/O metrics (bytes sent/received) are not included despite psutil supporting them.