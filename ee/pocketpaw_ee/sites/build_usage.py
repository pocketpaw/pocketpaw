# ee/pocketpaw_ee/sites/build_usage.py -- what a Daytona build sandbox really used.
#
# ``daytona_runner.run_build`` (site publish/preview builds and project builds) calls
# :func:`measure` once at the end of a build, inside the still-running sandbox, and
# :func:`log_usage` once per build. The point is right-sizing: the build box asks
# Daytona for ``PAW_SITES_BUILD_CPU`` / ``_MEMORY_GIB`` / ``_DISK_GIB`` (2 / 4 / 10 by
# default) against a small org quota, and these lines say how much of it a build used.
#
# One exec, one shell script, every step bounded by ``timeout``:
#   * peak memory: cgroup v2 ``memory.peak``, else cgroup v1
#     ``memory.max_usage_in_bytes``, else ``/proc/meminfo`` used-now (NOT a peak; the
#     line says ``memory_source=proc_meminfo_now`` so nobody sizes from it blindly);
#   * ``du -sb`` of the project dir (node_modules and the output included), of
#     ``node_modules``, of the build output dir, and ``df`` used bytes of ``/``.
#
# INVARIANT: measuring never changes a build. :func:`measure` never raises (a raise,
# a timeout, missing files or garbage output all become ``None`` fields) and is
# bounded by ``PROBE_TIMEOUT_SECONDS``. The probe command carries
# ``USAGE_PROBE_MARKER`` so test fakes can tell it from the build exec.
from __future__ import annotations

import asyncio
import logging
import math
import shlex
from typing import Any

logger = logging.getLogger(__name__)

#: A no-op token at the top of the probe script; see the header.
USAGE_PROBE_MARKER = "paw-usage-probe"

#: Our await bound on the whole probe. The steps inside are bounded tighter, so a slow
#: ``du`` loses its own number, not the rest.
PROBE_TIMEOUT_SECONDS: float = 30.0
_STEP_TIMEOUT_SECONDS = 15

_INT_FIELDS = (
    "mem_peak_bytes",
    "disk_workdir_bytes",
    "disk_node_modules_bytes",
    "disk_output_bytes",
    "disk_fs_used_bytes",
)

_MIB = 1024 * 1024


def usage_probe_command(project_dir: str, output_dir: str) -> str:
    """The one shell command that prints ``key=value`` usage lines and exits 0."""
    q = shlex.quote
    t = _STEP_TIMEOUT_SECONDS
    script = "\n".join(
        [
            f": {USAGE_PROBE_MARKER}",
            'p=""; src=none',
            "for f in /sys/fs/cgroup/memory.peak:cgroup_v2"
            " /sys/fs/cgroup/memory/memory.max_usage_in_bytes:cgroup_v1; do",
            '  path="${f%%:*}"',
            '  if [ -r "$path" ]; then',
            '    p="$(timeout 5 cat "$path" 2>/dev/null)"',
            '    if [ -n "$p" ]; then src="${f##*:}"; break; fi',
            "  fi",
            "done",
            'if [ -z "$p" ] && [ -r /proc/meminfo ]; then',
            '  mt="$(sed -n "s/^MemTotal: *\\([0-9]*\\) kB/\\1/p" /proc/meminfo)"',
            '  ma="$(sed -n "s/^MemAvailable: *\\([0-9]*\\) kB/\\1/p" /proc/meminfo)"',
            '  if [ -n "$mt" ] && [ -n "$ma" ]; then',
            '    p="$(( (mt - ma) * 1024 ))"; src=proc_meminfo_now',
            "  fi",
            "fi",
            'echo "mem_peak_bytes=$p"',
            'echo "mem_source=$src"',
            f'echo "disk_workdir_bytes=$(timeout {t} du -sb {q(project_dir)} 2>/dev/null'
            ' | cut -f1)"',
            f'echo "disk_node_modules_bytes=$(timeout {t} du -sb'
            f' {q(project_dir + "/node_modules")} 2>/dev/null | cut -f1)"',
            f'echo "disk_output_bytes=$(timeout {t} du -sb {q(output_dir)} 2>/dev/null | cut -f1)"',
            'echo "disk_fs_used_bytes=$(timeout 5 df -B1 --output=used / 2>/dev/null'
            ' | tail -n 1 | tr -d " ")"',
            "exit 0",
        ]
    )
    return f"sh -c {q(script)}"


def parse_usage(raw: str | None) -> dict[str, Any]:
    """``key=value`` lines to a dict with every known field; unreadable values -> None."""
    values: dict[str, str] = {}
    for line in (raw or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    out: dict[str, Any] = {}
    for key in _INT_FIELDS:
        value = values.get(key, "")
        out[key] = int(value) if value.isdigit() else None
    source = values.get("mem_source", "")
    out["mem_source"] = source if source and source != "none" else None
    # Keep the dict's key order stable for logs: memory first, then disk.
    return {
        "mem_peak_bytes": out["mem_peak_bytes"],
        "mem_source": out["mem_source"],
        **{k: out[k] for k in _INT_FIELDS[1:]},
    }


def response_stdout(resp: Any) -> str:
    """stdout of an exec: the SDK's ``ExecuteResponse.result``, or a bare string."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    result = getattr(resp, "result", None)
    return result if isinstance(result, str) else ""


async def measure(
    client: Any, sandbox_id: str, *, project_dir: str, output_dir: str
) -> dict[str, Any]:
    """Run the probe in ``sandbox_id``. Never raises; unknowns come back as None."""
    command = usage_probe_command(project_dir, output_dir)
    try:
        resp = await asyncio.wait_for(
            client.execute_command(
                sandbox_id, command, timeout=max(1, math.ceil(PROBE_TIMEOUT_SECONDS))
            ),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 -- measuring must never fail a build
        logger.info("build_usage: probe skipped in %s (%s)", sandbox_id, type(exc).__name__)
        return parse_usage(None)
    return parse_usage(response_stdout(resp))


def _mib(value: int | None) -> str:
    return "unknown" if value is None else str(round(value / _MIB))


def _pct(used: int | None, requested_gib: int) -> str:
    if used is None or requested_gib <= 0:
        return "unknown"
    return str(round(100 * used / (requested_gib * 1024 * _MIB)))


def log_usage(
    usage: dict[str, Any] | None,
    *,
    engine: str,
    labels: dict[str, str] | None,
    outcome: str,
    cpu: int,
    memory_gib: int,
    disk_gib: int,
    duration_seconds: float,
) -> None:
    """One INFO line per build: what was asked for, what was used, how long it took.
    The same values ride on the record as ``build_usage`` for structured handlers."""
    usage = usage or parse_usage(None)
    labels = labels or {}
    record = {
        "engine": engine,
        "lane": labels.get("paw_lane"),
        "pocket": labels.get("paw_pocket"),
        "job": labels.get("paw_job"),
        "outcome": outcome,
        "cpu": cpu,
        "memory_gib": memory_gib,
        "disk_gib": disk_gib,
        "duration_seconds": round(duration_seconds, 1),
        **usage,
    }
    logger.info(
        "sites.build_usage: engine=%s lane=%s pocket=%s job=%s outcome=%s "
        "cpu=%d memory_gib=%d disk_gib=%d "
        "peak_memory_mib=%s peak_memory_pct=%s memory_source=%s "
        "workdir_mib=%s node_modules_mib=%s output_mib=%s fs_used_mib=%s fs_used_pct=%s "
        "duration_s=%.1f",
        engine,
        record["lane"] or "-",
        record["pocket"] or "-",
        record["job"] or "-",
        outcome,
        cpu,
        memory_gib,
        disk_gib,
        _mib(usage["mem_peak_bytes"]),
        _pct(usage["mem_peak_bytes"], memory_gib),
        usage["mem_source"] or "unknown",
        _mib(usage["disk_workdir_bytes"]),
        _mib(usage["disk_node_modules_bytes"]),
        _mib(usage["disk_output_bytes"]),
        _mib(usage["disk_fs_used_bytes"]),
        _pct(usage["disk_fs_used_bytes"], disk_gib),
        duration_seconds,
        extra={"build_usage": record},
    )


__all__ = [
    "PROBE_TIMEOUT_SECONDS",
    "USAGE_PROBE_MARKER",
    "log_usage",
    "measure",
    "parse_usage",
    "response_stdout",
    "usage_probe_command",
]
