"""Per-tenant agent working-directory jail (cloud only).

Created: 2026-06-26 (ART-2) — gives every multi-tenant cloud workspace its own
agent working directory so a tenant's file operations never co-mingle in the
shared home dir. Today the chat agent (``ClaudeSDKBackend``) runs with
``cwd = settings.file_jail_path``, which defaults to ``Path.home()`` — in cloud
every tenant's agent shares ``~`` and writes land on top of each other.

``resolve_agent_cwd()`` reads the run's identity from the
``attach_agent_identity`` ContextVars (``current_workspace_id`` /
``current_session_mongo_id``) and returns a per-workspace, per-session jail dir.
It FAILS CLOSED: a run that reaches the backend in multi-tenant cloud mode with
no resolvable workspace RAISES rather than silently falling back to ``~`` — that
silent fallback is the exact co-mingling bug this closes. OFF cloud (OSS /
dedicated, where the cloud DB was never initialized) it returns ``None`` so the
core agent keeps using ``settings.file_jail_path`` unchanged.

The multi-tenant-cloud signal is ``get_client() is not None`` — the cloud DB
client is set exactly when ``init_cloud_db`` ran (``CloudLifecycleHook`` on
``CLOUD_MONGODB_URI``), so it is the authoritative "this process is serving
tenants" flag without inventing a new one.

When the fail-closed fires (a cloud run that lost its workspace = a
mis-tenanting signal), a high-severity cloud AuditEvent is emitted via the
Layer-5 audit service before the raise — exactly the signal that audit exists
to capture. The emit is best-effort and never blocks the raise.

Scope (ART-2): cwd resolution + fail-closed + the cloud/OSS gate only. Quota,
TTL garbage-collection and disk-watermark eviction of these dirs are ART-3.
"""

from __future__ import annotations

import logging
import os
import re
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
        current_session_mongo_id,
        current_user_id,
        current_workspace_id,
    )

    workspace_id = current_workspace_id()
    if not workspace_id:
        # No tenancy bound. Distinguish a multi-tenant cloud run that lost its
        # workspace (a bug we must NOT paper over by writing into the shared
        # home dir) from an OSS / dedicated run where identity is legitimately
        # never bound. ``get_client()`` is non-None exactly when
        # ``init_cloud_db`` ran — the authoritative multi-tenant-cloud signal.
        from pocketpaw_ee.cloud.shared.db import get_client

        if get_client() is not None:
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
