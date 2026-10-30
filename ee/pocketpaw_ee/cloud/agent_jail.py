"""Per-tenant agent working-directory jail (cloud only).

Created: 2026-06-26 (ART-2) — gives every multi-tenant cloud workspace its own
agent working directory so a tenant's file operations never co-mingle in the
shared home dir. Today the chat agent (``ClaudeSDKBackend``) runs with
``cwd = settings.file_jail_path``, which defaults to ``Path.home()`` — in cloud
every tenant's agent shares ``~`` and writes land on top of each other.

``resolve_agent_cwd()`` reads the run's identity from the
``attach_agent_identity`` ContextVars (``current_workspace_id`` /
``current_session_mongo_id``) and returns a per-workspace, per-session jail dir.
It FAILS CLOSED, but ONLY for a cloud CHAT run: a run that reaches the backend on
the cloud chat dispatch path (marked by ``current_cloud_chat_run()`` — set by
``run_core.execute_run`` around the run lifecycle) with no resolvable workspace
RAISES rather than silently falling back to ``~`` — that silent fallback is the
exact co-mingling bug this closes. Every OTHER workspace-less run returns
``None`` so the core agent keeps using ``settings.file_jail_path`` unchanged: an
OSS / dedicated run (the cloud DB was never initialized), AND a non-chat run (the
CLI, a background job, a direct backend test) that merely shares a
cloud-connected process. ``is_multi_tenant_cloud()`` alone is a process-global,
so without the per-run marker the gate would wrongly hard-fail all of those.

The multi-tenant-cloud signal is ``is_multi_tenant_cloud()`` (ART-4) — the cloud
DB client is set exactly when ``init_cloud_db`` ran (``CloudLifecycleHook`` on
``CLOUD_MONGODB_URI``), so it is the authoritative "this process is serving
tenants" flag without inventing a new one. The signal now lives in one place
(``pocketpaw_ee.cloud.shared.db``) so the jail and the cloud-storage boot guard
read the same name.

When the fail-closed fires (a cloud run that lost its workspace = a
mis-tenanting signal), a high-severity cloud AuditEvent is emitted via the
Layer-5 audit service before the raise — exactly the signal that audit exists
to capture. The emit is best-effort and never blocks the raise.

Scope (ART-2): cwd resolution + fail-closed + the cloud/OSS gate.

ART-3 (2026-06-26) — jail lifecycle, so scratch disk scales with active
concurrency, not user count. The jail is pure scratch (durability lives in blob
storage, a later task), so an idle jail is always safe to evict. Three bounds,
all cloud-only:

  * **Per-workspace quota** (``POCKETPAW_AGENT_JAIL_QUOTA_MB``, default 2048).
    ``check_workspace_jail_quota`` measures a workspace's total jail size at
    RUN-START and returns a clean rejection message when over the cap — the
    run is failed cleanly (``run_core`` turns it into a terminal ``failed``
    event), never crashing the box. We measure at run-start rather than gating
    each write because the agent writes through its native subprocess tools
    (Write/Bash), so individual writes can't be cheaply intercepted; run-start
    is the enforceable, testable granularity.
  * **TTL garbage-collection + disk-watermark eviction** live in the sibling
    ``agent_jail_gc`` module (``sweep_agent_jails``), registered on cloud
    startup and the 5-minute heartbeat exactly like the stale-run sweeper. This
    module owns the filesystem primitives the sweep composes (scan, enumerate,
    evict, disk probe); the sweep owns the run-state coordination (it never
    evicts a jail while its run is still queued or running — and a retried
    interrupted run re-protects the jail by spawning a fresh queued run).

Changes: 2026-06-27 (fix/cloud-artifacts-reland) — ``resolve_agent_cwd``'s
fail-closed is now ALSO gated on the per-run ``current_cloud_chat_run()`` marker,
not ``is_multi_tenant_cloud()`` alone. The latter is a PROCESS-GLOBAL (True
whenever the cloud Mongo client is connected), so the original gate hard-failed
EVERY workspace-less run in a cloud-connected process — direct claude_sdk backend
tests, the CLI, background jobs — and would have hard-failed legitimate
non-tenant agent runs in a real cloud process. The marker (set by
``run_core.execute_run`` around the run lifecycle) fires the fail-closed only for
an actual cloud chat dispatch; otherwise the resolver returns ``None`` and the
core falls back to ``settings.file_jail_path``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# A jail path segment is a single workspace / session id. Ids are Mongo
# ObjectId hex in practice; restrict to a safe charset (no path separators)
# so a malformed or hostile id can never escape its workspace subtree. The
# anchor is ``\Z`` (very end of string), NOT ``$`` — ``$`` also matches just
# before a trailing newline, so ``"abc\n"`` would slip past a ``$`` guard.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+\Z")

# Group/DM-bridge runs bind a workspace but no session; they share one
# per-workspace dir under this name (still tenant-isolated, just not
# session-granular).
_SESSIONLESS_DIRNAME = "_shared"


def _safe_segment(value: str, *, label: str) -> str:
    """Return *value* if it is a safe single path segment, else raise.

    Guards the jail against path traversal: the charset excludes ``/`` so a
    crafted id cannot climb out of its workspace dir, and the literal ``.`` /
    ``..`` are rejected outright.
    """
    if value in {".", ".."} or not _SAFE_SEGMENT.match(value):
        raise ValueError(f"unsafe {label} for agent jail path: {value!r}")
    return value


def _emit_fail_closed_audit(actor_id: str | None, session_id: str | None) -> None:
    """Best-effort high-severity audit alert for a fail-closed cloud run.

    A cloud agent run reaching the backend with no resolvable workspace is a
    mis-tenanting signal — exactly what Layer-5 audit exists to capture. Emit it
    through the cloud audit service, fire-and-forget on the running loop (the
    resolver is sync but runs inside the async agent loop). NEVER raises and
    NEVER blocks the fail-closed raise that follows: a missing alert must not
    turn a closed door into an open one. No running loop (e.g. a sync unit test)
    → skip silently; the raise still happens.
    """
    try:
        import asyncio

        from pocketpaw_ee.cloud.audit import service as audit_service

        async def _record() -> None:
            # ``record`` itself never raises; the workspace is a sentinel
            # because the whole alert IS "this run had no workspace".
            await audit_service.record(
                "__no_workspace__",
                actor_id or "unknown",
                "agent.cwd_jail.fail_closed",
                target_type="agent_run",
                target_id=session_id,
                metadata={
                    "severity": "high",
                    "reason": "no resolvable workspace_id",
                },
            )

        asyncio.get_running_loop().create_task(_record())
    except Exception:  # noqa: BLE001 — telemetry must never break fail-closed
        logger.debug("fail-closed audit alert could not be scheduled", exc_info=True)


def workspace_jail_root() -> Path:
    """Root under which every workspace's agent jail lives.

    Defaults to ``~/.pocketpaw/workspaces``. Override with
    ``POCKETPAW_WORKSPACE_JAIL_ROOT`` to anchor the jail on a data volume (and
    so tests can redirect it off the real home dir).
    """
    override = os.environ.get("POCKETPAW_WORKSPACE_JAIL_ROOT", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".pocketpaw" / "workspaces"


def resolve_agent_cwd() -> str | None:
    """Resolve the per-session agent working directory for the active run.

    Returns:
        - ``<root>/<workspace_id>/agent/<session_id>/`` (created on demand) when
          a workspace is bound to the active run.
        - ``None`` when the process is not in multi-tenant cloud mode, so the
          core agent falls back to ``settings.file_jail_path``.

    Raises:
        RuntimeError: cloud mode is active but no workspace is resolvable — the
        fail-closed guard against tenant file co-mingling.
    """
    from pocketpaw_ee.cloud.chat.agent_service import (
        current_cloud_chat_run,
        current_session_mongo_id,
        current_user_id,
        current_workspace_id,
    )

    workspace_id = current_workspace_id()
    if not workspace_id:
        # No tenancy bound. Distinguish a multi-tenant cloud CHAT run that lost
        # its workspace (a bug we must NOT paper over by writing into the shared
        # home dir) from every OTHER workspace-less run: an OSS / dedicated run
        # where identity is legitimately never bound, OR a non-chat run (the CLI,
        # a background job, a direct backend test) that merely shares a
        # cloud-connected process. ``is_multi_tenant_cloud()`` is a PROCESS-GLOBAL
        # — True whenever ``init_cloud_db`` ran (the cloud Mongo client is set, so
        # the box is serving tenants) — so it CANNOT tell a real chat dispatch
        # from any other run in the same process. The per-run
        # ``current_cloud_chat_run()`` marker (set by ``run_core.execute_run``
        # around the run lifecycle, BEFORE identity is bound) carries that
        # distinction. Fail closed ONLY for a cloud CHAT run; otherwise return
        # ``None`` so the core falls back to ``settings.file_jail_path`` — the
        # pre-ART-2 behavior — instead of hard-failing a legitimately un-tenanted
        # run.
        from pocketpaw_ee.cloud.shared.db import is_multi_tenant_cloud

        if is_multi_tenant_cloud() and current_cloud_chat_run():
            # Mis-tenanting signal — alert before failing closed (best-effort).
            _emit_fail_closed_audit(current_user_id(), current_session_mongo_id())
            raise RuntimeError(
                "cloud agent run reached the backend with no resolvable "
                "workspace_id; refusing to fall back to the shared home "
                "directory (would co-mingle tenant files). A cloud run must "
                "bind identity via attach_agent_identity before the agent runs."
            )
        return None

    ws_segment = _safe_segment(workspace_id, label="workspace_id")
    # ``session_mongo_id`` is set on the SSE chat path so a multi-turn chat
    # reuses one dir; the group/DM bridge binds a workspace but no session, so
    # those runs share a per-workspace ``_shared`` dir.
    session_raw = current_session_mongo_id()
    session_segment = (
        _safe_segment(session_raw, label="session_mongo_id")
        if session_raw
        else _SESSIONLESS_DIRNAME
    )

    cwd = workspace_jail_root() / ws_segment / "agent" / session_segment
    cwd.mkdir(parents=True, exist_ok=True)
    return str(cwd)


# --------------------------------------------------------------------------- #
# ART-3 — jail lifecycle primitives (quota / TTL-GC / watermark).
#
# Settings are read straight from the environment, like ``workspace_jail_root``
# above, so the jail module stays self-contained (no Settings import). Every
# knob is cloud-only; off-cloud there is no jail to bound.
# --------------------------------------------------------------------------- #

_DEFAULT_QUOTA_MB = 2048.0
_DEFAULT_TTL_GRACE_SECONDS = 3600.0
_DEFAULT_DISK_WATERMARK_PCT = 90.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default


def jail_quota_bytes() -> int:
    """Per-workspace jail size cap in bytes (``POCKETPAW_AGENT_JAIL_QUOTA_MB``,
    default 2048 MB). ``<= 0`` disables the quota."""
    return int(_env_float("POCKETPAW_AGENT_JAIL_QUOTA_MB", _DEFAULT_QUOTA_MB) * 1024 * 1024)


def jail_ttl_grace_seconds() -> float:
    """Idle grace before a jail with no active run is GC'd, in seconds
    (``POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS``, default 3600)."""
    return _env_float("POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS", _DEFAULT_TTL_GRACE_SECONDS)


def jail_disk_watermark_pct() -> float:
    """Box disk-usage %% high-water mark that triggers LRU eviction
    (``POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT``, default 90). ``<= 0`` disables
    watermark eviction."""
    return _env_float("POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT", _DEFAULT_DISK_WATERMARK_PCT)


def jail_gc_enabled() -> bool:
    """Whether the background jail GC sweep runs (default on). Set
    ``POCKETPAW_AGENT_JAIL_GC_ENABLED=false`` to disable it as an escape hatch."""
    return os.environ.get("POCKETPAW_AGENT_JAIL_GC_ENABLED", "true").strip().lower() not in {
        "false",
        "0",
        "no",
        "off",
    }


def scan_jail_dir(path: Path) -> tuple[int, float]:
    """Return ``(size_bytes, last_activity_epoch)`` for one jail dir.

    Walks the subtree ONCE (cheap ``os.scandir`` with the stat cached on the
    DirEntry), summing file sizes and tracking the newest mtime. The newest
    mtime over the WHOLE tree — not the top dir's own mtime — is the activity
    signal: a directory's mtime does not advance when a file in a NESTED subdir
    is written (``node_modules/x/y``), so the top mtime alone would read a busy
    jail as idle and let the GC evict it mid-build. Vanishing / unreadable
    entries (mid-build races, broken symlinks) are skipped; an empty dir reports
    its own mtime. Symlinks are never followed, so a link can't inflate the size
    or drag the scan outside the jail.
    """
    try:
        latest = path.stat().st_mtime
    except OSError:
        return (0, 0.0)

    size = 0
    stack = [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            latest = max(latest, st.st_mtime)
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            else:
                size += st.st_size
    return (size, latest)


def workspace_jail_over_quota(workspace_id: str, limit_bytes: int) -> bool:
    """``True`` as soon as a workspace's total jail size crosses ``limit_bytes``.

    Short-circuits the moment the running sum passes the limit — it does NOT
    finish the walk or compute the exact total. The run-start quota gate only
    needs the boolean, and it runs on EVERY run's critical path: a workspace
    whose jail holds a ``node_modules`` (tens of thousands of files) would
    otherwise pay a full ``O(files)`` stat-walk before every run — exactly our
    file-creating use case. With the early-exit the walk is bounded by roughly
    ``limit_bytes`` worth of file entries even when a runaway jail is many times
    over quota. (The off-critical-path GC sweep keeps the full ``scan_jail_dir``
    walk; it needs the exact size + last-activity and runs on a 5-minute cadence.)
    """
    # Iterative DFS over the whole ``<root>/<ws>/agent`` subtree (all session
    # dirs at once), summing file sizes and bailing the instant we cross.
    total = 0
    stack = [workspace_jail_root() / workspace_id / "agent"]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            else:
                total += st.st_size
                if total > limit_bytes:
                    return True
    return False


def check_workspace_jail_quota(workspace_id: str | None) -> str | None:
    """Return a human-readable rejection message if a workspace's jail is over
    quota, else ``None``.

    Called at RUN-START by ``run_core`` to reject a run before it spins up the
    agent, so it leans on ``workspace_jail_over_quota``'s early-exit rather than
    a full size walk. A ``None`` / empty workspace (no jail) and a disabled quota
    (``<= 0``) are always allowed.
    """
    if not workspace_id:
        return None
    quota = jail_quota_bytes()
    if quota <= 0:
        return None
    if not workspace_jail_over_quota(workspace_id, quota):
        return None
    return (
        f"agent workspace storage is full: the jail exceeds its "
        f"{quota / 1024 / 1024:.0f} MB per-workspace limit. Deliver or remove "
        f"build artifacts to free space, then retry."
    )


def iter_workspace_jail_dirs() -> Iterator[tuple[str, str, Path]]:
    """Yield ``(workspace_id, session_segment, path)`` for every existing jail
    dir ``<root>/<ws>/agent/<seg>``. Silent when the root doesn't exist yet."""
    try:
        ws_entries = list(os.scandir(workspace_jail_root()))
    except OSError:
        return
    for ws in ws_entries:
        if not ws.is_dir(follow_symlinks=False):
            continue
        try:
            sess_entries = list(os.scandir(Path(ws.path) / "agent"))
        except OSError:
            continue
        for sess in sess_entries:
            if sess.is_dir(follow_symlinks=False):
                yield (ws.name, sess.name, Path(sess.path))


def disk_usage_pct() -> float:
    """Percent (0–100) of the jail-root volume in use, or ``0.0`` if it can't be
    probed (root missing, no psutil). 0.0 is the safe default — it reads as
    "below any watermark" so a probe failure never triggers eviction."""
    try:
        import psutil

        return float(psutil.disk_usage(str(workspace_jail_root())).percent)
    except Exception:  # noqa: BLE001 — a probe failure must not break the sweep
        logger.debug("jail disk-usage probe failed", exc_info=True)
        return 0.0


def evict_jail_dir(path: Path) -> bool:
    """Delete a jail dir tree. Returns ``True`` on success (or if already gone).

    Refuses any path that does not live UNDER the jail root — defense against a
    bug handing this an arbitrary path. Refusing the root itself (a dir is not
    its own parent) means a stray call can never wipe every tenant's jail.
    """
    root = workspace_jail_root().resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if root not in resolved.parents:
        logger.error("refusing to evict path outside the jail root: %s", path)
        return False
    try:
        shutil.rmtree(resolved)
        return True
    except FileNotFoundError:
        return True  # already gone — eviction is idempotent
    except OSError:
        logger.exception("failed to evict jail dir %s", path)
        return False
