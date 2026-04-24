---
{
  "title": "System and Usage Metrics API",
  "summary": "The metrics router provides two endpoints: one that reports real-time system resource usage (CPU, RAM, disk, uptime, battery), and one that clears accumulated usage records. Both are protected by an admin scope to prevent unprivileged callers from inspecting host hardware details.",
  "concepts": [
    "system metrics",
    "psutil",
    "CPU usage",
    "memory usage",
    "disk usage",
    "battery",
    "uptime",
    "optional dependency",
    "admin scope",
    "usage records"
  ],
  "categories": [
    "monitoring",
    "API",
    "system"
  ],
  "source_docs": [
    "17290e5a3f2ec5d9"
  ],
  "backlinks": null,
  "word_count": 410,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Visibility into the host machine matters for self-hosted deployments where the companion agent runs 24/7 on a user's personal hardware. The `metrics.py` router surfaces this information through a standard REST interface so the dashboard can show live system health at a glance.

## `GET /metrics/system`

This endpoint is guarded by `require_scope("metrics", "admin")` — the narrowest possible scope that still allows a dedicated monitoring API key without granting full admin rights.

### psutil as an Optional Dependency

The endpoint imports `psutil` inside the function body rather than at module level:

```python
try:
    import psutil
except ImportError:
    return {
        "available": False,
        "os": platform.system(),
        "arch": platform.machine(),
        "error": "psutil not installed",
    }
```

This pattern has two benefits. First, PocketPaw can start without `psutil` installed — useful for minimal Docker images or restricted environments. Second, import errors surface as a structured API response rather than a 500, so the dashboard can render a "metrics unavailable" state instead of crashing.

### Resource Collection

When psutil is present, the endpoint collects:

- **CPU**: utilisation percentage (non-blocking, `interval=0`), core count, and frequency in MHz
- **RAM**: total, available, used, and percentage (from `virtual_memory`)
- **Disk**: usage at the root path, gracefully degraded to `null` if the filesystem query fails (network-mounted roots or restricted containers can raise exceptions)
- **Uptime**: derived from `psutil.boot_time()` and the current UTC clock
- **Battery**: optional field returned only when a battery is present (`psutil.sensors_battery()` returns `None` on desktops and servers)

All values are rounded or cast to avoid floating-point noise in the API response.

### Why Non-Blocking CPU

`cpu_percent(interval=0)` samples the last measurement rather than blocking for a full second. This keeps the HTTP response fast. The trade-off is slightly less accurate instantaneous readings on the first call, which is acceptable for a dashboard that refreshes periodically.

## `DELETE /usage`

Clears all accumulated usage records. The `require_scope("admin")` guard ensures only the owner can reset billing-relevant data. No request body is needed — this is an administrative hard reset, not a filtered delete.

## Known Gaps

- The disk path is hardcoded to `"/"`. On Windows this would need to query the system drive letter. The `platform.system()` check in the ImportError path suggests cross-platform awareness, but the disk query does not yet handle the Windows case.
- Battery percentage is reported but charge/discharge state is not surfaced, limiting actionability for laptop-based deployments.
- There is no historical time-series storage. Each call returns a point-in-time snapshot; trending requires an external metrics collector.
