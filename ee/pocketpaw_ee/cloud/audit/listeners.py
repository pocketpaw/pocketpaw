# listeners.py — Bridge AuditLogger (JSONL) writes into AuditStore (SQLite).
# Created: 2026-05-24 (#1202) — Cloud audit writers across the EE codebase
#   (pockets/action_executor, pockets/source_executor, pockets/service,
#   skills/service, agent/pocket_router) all call
#   ``pocketpaw.security.audit.get_audit_logger().log(AuditEvent.create(...))``
#   which appends a row to ``~/.pocketpaw/audit.jsonl``. The cloud reader
#   ``GET /api/v1/audit`` (``ee/pocketpaw_ee/cloud/audit/service.py``) reads
#   from ``pocketpaw.audit.store.AuditStore``, a SQLite file at
#   ``~/.pocketpaw/audit.db``. Nothing was bridging the two sinks, so every
#   query returned 0 rows — even ``?pocket_id={id}`` against a pocket that
#   had just been written to.
#
#   This module installs an ``AuditLogger.on_log`` callback at ``mount_cloud``
#   time that mirrors each event into ``AuditStore`` so the GET surface sees
#   the writes. The mapping preserves the original (free-form) writer
#   category in ``metadata.source_category`` and picks a coarse
#   ``AuditEntry.category`` (the reader DTO restricts it to a literal set)
#   so existing rows stay queryable. ``workspace_id`` rides on
#   ``context.workspace_id`` to match the reader's tenant rollup.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.audit.store import get_audit_store
from pocketpaw.security.audit import get_audit_logger

logger = logging.getLogger(__name__)

# Module-level guard so a second ``register_audit_bridge()`` call (e.g. a
# test that re-runs ``mount_cloud``) does not append the same callback
# twice. ``AuditLogger.on_log`` is a thin list append with no de-dup.
_BRIDGE_REGISTERED = False


# Map free-form writer categories (``pocket_backend_config``,
# ``pocket_embed``, ``skills_config``, ``pocket_router``, …) to the strict
# ``AuditEntry.category`` literal set
# (``decision`` / ``data`` / ``config`` / ``security``). The original value
# is preserved in ``metadata.source_category`` so nothing is lost.
_CATEGORY_MAP: dict[str, str] = {
    "pocket_backend_config": "config",
    "pocket_embed": "config",
    "skills_config": "config",
    "pocket_router": "decision",
}

# Writer ``status`` values from ``AuditEvent`` are free-form
# (``success`` / ``error`` / ``rejected`` / ``rate-limited`` /
# ``instinct-pending`` / ``allow`` / ``block`` …). ``AuditEntry.status`` is
# a strict literal set. Map to the closest match; preserve the original in
# ``metadata.source_status``.
_STATUS_MAP: dict[str, str] = {
    "success": "completed",
    "allow": "completed",
    "approved": "approved",
    "rejected": "rejected",
    "block": "rejected",
    "pending": "pending",
    "instinct-pending": "pending",
}


def _coerce_category(raw: Any) -> str:
    """Return a valid ``AuditEntry.category`` from a free-form writer value.

    Unknown values fall back to ``"config"`` — every writer this bridge
    sees today emits a configuration- or governance-shaped event, so that
    is the least-surprising default. The original value is preserved by
    the caller under ``metadata.source_category``.
    """
    if isinstance(raw, str) and raw in _CATEGORY_MAP:
        return _CATEGORY_MAP[raw]
    if isinstance(raw, str) and raw in ("decision", "data", "config", "security"):
        return raw
    return "config"


def _coerce_status(raw: Any) -> str:
    """Return a valid ``AuditEntry.status`` from a free-form writer value.

    Unknown values fall back to ``"completed"`` — most writer ``status``
    strings (``error``, ``timeout``, …) describe a finished attempt and
    line up best with ``completed``. The original lives under
    ``metadata.source_status`` so a downstream UI can still surface the
    nuance.
    """
    if isinstance(raw, str) and raw in _STATUS_MAP:
        return _STATUS_MAP[raw]
    if isinstance(raw, str) and raw in ("completed", "approved", "rejected", "pending"):
        return raw
    return "completed"


def _mirror_to_store(event_dict: dict) -> None:
    """Translate one ``AuditEvent`` dict and insert it into ``AuditStore``.

    Runs inside the ``AuditLogger.log()`` sync callback fan-out — must be
    fast and must NEVER raise. ``AuditLogger`` already wraps the callback
    in ``try / except: pass``, but a defensive ``try`` here lets us log
    the failure for debugging instead of silently dropping it.
    """
    try:
        # ``security.audit.AuditEvent`` stores everything that is not one of
        # the named fields under ``context``. The cloud writers stash
        # ``workspace_id`` / ``pocket_id`` / ``category`` there.
        ctx = dict(event_dict.get("context") or {})

        # Pocket id may arrive in context (cloud writers) OR via ``target``
        # (older code paths). Prefer the explicit context value so the
        # reader's ``pocket_id = ?`` filter lines up; fall back to target
        # when the action is a pocket.* action.
        pocket_id = ctx.pop("pocket_id", None)
        target = event_dict.get("target") or ""
        action = event_dict.get("action") or ""
        if not pocket_id and target and action.startswith("pocket."):
            pocket_id = target

        # Workspace id: must land in context so search_entries' workspace
        # rollup (``json_extract(context, '$.workspace_id') = ?``) matches.
        # The pop / re-add keeps the value if the writer already put it
        # there, and silently does nothing if the writer omitted it.
        workspace_id = ctx.get("workspace_id")

        source_category = ctx.pop("category", None)
        category = _coerce_category(source_category)
        status = _coerce_status(event_dict.get("status"))

        description = f"{action} on {target}" if target else action
        if not description:
            description = "audit event"

        metadata: dict[str, Any] = {
            "severity": event_dict.get("severity"),
            "source_id": event_dict.get("id"),
        }
        if source_category is not None:
            metadata["source_category"] = source_category
        source_status = event_dict.get("status")
        if source_status is not None and source_status != status:
            metadata["source_status"] = source_status

        store = get_audit_store()
        store._ensure_schema()
        # AuditStore.log_entry is async only because the public API is
        # async-shaped; the body is a plain sync SQLite insert. Call the
        # same code path via a dedicated sync helper so we do not need an
        # event loop in the on_log callback.
        store.log_entry_sync(
            actor=event_dict.get("actor") or "system",
            action=action or "unknown",
            category=category,
            description=description,
            pocket_id=pocket_id,
            context={**ctx, **({"workspace_id": workspace_id} if workspace_id else {})},
            status=status,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — never let an audit bridge break a write
        logger.warning("audit bridge: failed to mirror entry to AuditStore", exc_info=True)


def register_audit_bridge() -> None:
    """Install the JSONL-to-SQLite audit bridge.

    Called once from ``mount_cloud`` so every ``AuditLogger.log()`` write
    in the EE cloud surface (pocket action runs, source runs, skills
    config changes, …) is mirrored into ``AuditStore``. Without this the
    cloud ``GET /api/v1/audit`` reader sees an empty SQLite file even
    when ``~/.pocketpaw/audit.jsonl`` is full of rows.

    Idempotent on two axes: a module-level ``_BRIDGE_REGISTERED`` flag
    short-circuits repeat calls in the same process, and a containment
    check against the logger's callback list (which a test fixture may
    have manipulated) prevents duplicate installs even if the flag was
    reset externally.
    """
    global _BRIDGE_REGISTERED
    if _BRIDGE_REGISTERED:
        return
    logger = get_audit_logger()
    if _mirror_to_store not in logger._callbacks:
        logger.on_log(_mirror_to_store)
    _BRIDGE_REGISTERED = True


__all__ = ["register_audit_bridge"]
