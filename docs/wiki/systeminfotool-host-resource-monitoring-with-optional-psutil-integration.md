---
{
  "title": "SystemInfoTool: Host Resource Monitoring with Optional psutil Integration",
  "summary": "`SystemInfoTool` reports CPU, RAM, disk, network I/O, and optionally top processes by delegating to `get_system_status()` for the base snapshot, then augmenting with `psutil` data when available. The psutil dependency is treated as optional — the tool degrades gracefully to the base snapshot if psutil is not installed.",
  "concepts": [
    "SystemInfoTool",
    "get_system_status",
    "psutil",
    "optional_dependency",
    "network_io",
    "process_enumeration",
    "resource_monitoring",
    "graceful_degradation",
    "BaseTool",
    "cpu_percent"
  ],
  "categories": [
    "tools",
    "system-monitoring",
    "observability",
    "infrastructure"
  ],
  "source_docs": [
    "3c6e6e0fa5fd80aa"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`sysinfo.py` implements the `system_info` tool, which gives agents visibility into the host machine's resource usage. This is useful for monitoring agent infrastructure, debugging performance issues, or answering user questions like "is the server overloaded?" The tool is split across two layers: a base implementation in `get_system_status()` (from `pocketpaw.tools.status`) and an augmentation layer using `psutil` when available.

## Two-Layer Architecture

```python
async def execute(self, include_processes: bool = False) -> str:
    result = get_system_status()  # Base snapshot (no external deps)
    try:
        import psutil
    except ImportError:
        return result  # Graceful degradation
    # Augment with network I/O and process list
```

The `try/except ImportError` pattern is a deliberate optional-dependency strategy. `psutil` is a C extension that requires compilation and isn't always available in constrained environments (Alpine Linux containers, read-only filesystems). By checking at runtime rather than declaring it as a required import, the tool remains functional even without psutil — it just returns the base system status.

## Network I/O Metrics

```python
net = psutil.net_io_counters()
sent_mb = net.bytes_sent / (1024**2)
recv_mb = net.bytes_recv / (1024**2)
sections.append(f"Network: ↑ {sent_mb:.1f} MB sent, ↓ {recv_mb:.1f} MB received")
```

Network I/O is reported as cumulative bytes since boot (this is what `psutil.net_io_counters()` returns), not a per-second rate. The tool doesn't document this distinction, which could mislead users into thinking 500 MB sent means current throughput.

Each augmentation section is wrapped in its own `try/except` so a failure in network counter collection doesn't prevent the process list from being shown, and vice versa. This per-section error isolation means partial data is always better than no data.

## Top Processes

```python
if include_processes:
    procs = [(cpu, name, pid) for proc in psutil.process_iter(["pid", "name", "cpu_percent"])]
    procs.sort(reverse=True)
    top = procs[:5]
```

The top-5 filter prevents the output from becoming unwieldy on busy servers. Processes with 0% CPU are filtered out before sorting — on a typical machine, hundreds of idle processes would otherwise dominate the list. The `include_processes` flag defaults to `False` because process enumeration has measurable overhead (`psutil.process_iter` with `cpu_percent` triggers a stat syscall per process).

## Trust Level: Not Set

Unlike most tools in the codebase, `SystemInfoTool` does not define a `trust_level` property. This means it inherits `BaseTool`'s default, which is typically `standard`. Host system information is not highly sensitive — it doesn't expose credentials or file contents — so standard trust is appropriate.

## Known Gaps

- Network metrics are cumulative since boot, not a per-second rate. A note in the description would prevent misinterpretation.
- `get_system_status()` internals are in a separate module (`tools/status.py`) not shown here — the full picture of what the base snapshot includes is opaque.
- No disk I/O metrics (read/write bytes per second) — only disk usage percentage is available through the base snapshot.
- Process list is capped at 5 with no parameter to adjust the limit.
