"""Agent-jail lifecycle GC (cloud only) — ART-3.

Created 2026-06-26 (ART-3). Bounds the per-tenant agent scratch jails that
``agent_jail`` hands out so one build-heavy run can't fill the shared box and
break every tenant. The jail is pure scratch (durability lives in blob storage,
a later task), so an idle jail is always safe to evict.

``sweep_agent_jails`` is the periodic half of the lifecycle (the per-workspace
quota is enforced inline at run-start in ``run_core``). One pass does two things,
in order:

  1. **TTL garbage-collection** — evict any jail whose run has ended and that has
     sat idle past the grace (``POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS``).
  2. **Disk-watermark eviction** — while the jail-root volume is over the
     high-water mark (``POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT``), evict the
     least-recently-used IDLE jails first until back under the mark.

The one invariant that makes this safe: a jail backing a still-active run is
NEVER evicted. "Active" = a non-terminal ``ChatRunDoc`` (status ``queued`` or
``running`` — the only non-terminal states) whose scope names that jail dir —
resolved once per pass via ``run_service.find_active_run_scopes`` and matched
against each dir's ``(workspace, session_segment)``. An ``interrupted`` run that
the user retries re-protects its jail by spawning a fresh ``queued`` run, so
evicting an idle jail between runs never races a resume. Idleness is read from
the jail's newest mtime (``agent_jail.scan_jail_dir``), which the DB active-set
check backstops: even a freshly-created queued run whose dir hasn't been written
yet (old mtime, empty dir) is protected, because its scope is in the active set.

Registered on cloud startup and the 5-minute heartbeat in
``extensions._sweeper_loop`` / ``start_run_sweeper``, in its own try, exactly
like the stale-run sweeper it mirrors — a jail-GC failure can never suppress the
other sweeps (or vice versa).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from pocketpaw_ee.cloud import agent_jail

logger = logging.getLogger(__name__)

# Cap dirs scanned per tick so a backlog can't wedge the heartbeat (mirrors the
# stale-run sweeper's batch cap). TTL eviction reaps idle dirs every pass, so the
# steady-state count stays bounded by active concurrency and this cap is slack.
_SWEEP_BATCH_LIMIT = 500

# One snapshot row: workspace, session segment, path, size, last-activity epoch.
_JailRow = tuple[str, str, Path, int, float]


async def sweep_agent_jails() -> int:
    """Run one TTL + watermark GC pass over the agent jails. Returns the number
    of jail dirs evicted. Never raises for an expected condition; a jail backing
    an active run is never evicted."""
    if not agent_jail.jail_gc_enabled():
        return 0

    from pocketpaw_ee.cloud.chat.runs import service as run_service

    active = await run_service.find_active_run_scopes()
    # A jail dir is named after its run's scope: SESSION runs → ``<scope_id>``;
    # the sessionless DM/group/pocket bridge → the per-workspace ``_shared`` dir.
    active_sessions = {(ws, scope) for (ws, ctype, scope) in active if ctype == "session"}
    active_shared_workspaces = {ws for (ws, ctype, _scope) in active if ctype != "session"}

    def _is_active(workspace_id: str, segment: str) -> bool:
        if segment == agent_jail._SESSIONLESS_DIRNAME:
            return workspace_id in active_shared_workspaces
        return (workspace_id, segment) in active_sessions

    # Snapshot every jail once (size + last activity), bounded per tick.
    rows: list[_JailRow] = []
    for workspace_id, segment, path in agent_jail.iter_workspace_jail_dirs():
        size, last_activity = agent_jail.scan_jail_dir(path)
        rows.append((workspace_id, segment, path, size, last_activity))
        if len(rows) >= _SWEEP_BATCH_LIMIT:
            break

    now = time.time()
    grace = agent_jail.jail_ttl_grace_seconds()
    evicted = 0
    survivors: list[_JailRow] = []  # idle dirs that passed TTL → watermark pool

    for row in rows:
        workspace_id, segment, path, _size, last_activity = row
        if _is_active(workspace_id, segment):
            continue  # never evict a jail whose run is still queued or running
        idle_for = now - last_activity
        if idle_for > grace:
            if agent_jail.evict_jail_dir(path):
                evicted += 1
                logger.info(
                    "jail GC: TTL-evicted idle jail %s/%s (idle %.0fs)",
                    workspace_id,
                    segment,
                    idle_for,
                )
        else:
            survivors.append(row)

    evicted += _evict_to_watermark(survivors)
    if evicted:
        logger.info("jail GC: evicted %d idle jail(s)", evicted)
    return evicted


def _evict_to_watermark(survivors: list[_JailRow]) -> int:
    """Evict idle survivors LRU-first while the jail-root volume is over the
    high-water mark. Only IDLE dirs reach here (active ones were skipped, TTL
    ones already reaped), so this never touches a live run's jail. Returns the
    count evicted."""
    watermark = agent_jail.jail_disk_watermark_pct()
    if watermark <= 0:
        return 0
    if agent_jail.disk_usage_pct() <= watermark:
        return 0

    evicted = 0
    # LRU first: oldest last-activity (row[4]) at the front of the queue.
    for workspace_id, segment, path, _size, _last in sorted(survivors, key=lambda r: r[4]):
        if agent_jail.disk_usage_pct() <= watermark:
            break
        if agent_jail.evict_jail_dir(path):
            evicted += 1
            logger.warning(
                "jail GC: watermark-evicted LRU idle jail %s/%s (disk over %.0f%%)",
                workspace_id,
                segment,
                watermark,
            )
    return evicted
