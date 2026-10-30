# ee/pocketpaw_ee/cloud/pockets/instinct_compensation_registry.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T4) — the in-memory
# registry that holds an OPTIMISTIC-lane write's compensation handle until
# the client either triggers rollback or the TTL expires (2026-06-18
# gate-layered-learning design). The OPTIMISTIC lane fires a write before a
# human has reviewed it, on the bet that it's reversible; this registry is
# the safety net that guarantees the bet is bounded in time.
#
# On the import-linter "Pockets" allowlist (no Beanie writes). It imports
# ``CompensateSpec`` from action_executor (import-linter-pure) and the OSS
# audit logger.
#
# TTL expiry is the security-critical path (design MF-8): if a compensation
# is never rolled back AND never expires loudly, an un-reviewed write sits
# committed with no human ever told. So expiry FIRES AN ALERT-severity
# audit event and PERSISTS the expired handle to
# ~/.pocketpaw/instinct/expired_compensations/<workspace_id>.jsonl (0700/
# 0600) before purging the entry. The persist is best-effort: if it fails
# we log a warning but STILL purge from memory so a write-failure can't leak
# the registry (MF-8). Hard expiry only — no heartbeat extension (design
# open-question #2, defaulted pending captain).
#
# Time is INJECTED (``now=...``) on register / sweep so the TTL is
# deterministic and testable; callers pass the real clock in production.

"""In-memory optimistic-compensation registry with TTL expiry."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger
from pocketpaw_ee.cloud.pockets.action_executor import CompensateSpec

logger = logging.getLogger(__name__)

_EXPIRED_SUBDIR = ("instinct", "expired_compensations")
_DIR_MODE = 0o700
_FILE_MODE = 0o600


@dataclass(frozen=True)
class CompensationHandle:
    """One live optimistic compensation awaiting rollback or expiry.

    Frozen so a holder cannot mutate the rollback target after the
    registry hands it back. ``compensate`` is the inverse write the saga
    runtime fires to undo the optimistic forward write.
    """

    compensation_id: str
    workspace_id: str
    pocket_id: str
    action: str
    compensate: CompensateSpec
    registered_at: datetime
    expires_at: datetime


class OptimisticCompensationRegistry:
    """Holds optimistic compensation handles until rollback or TTL expiry.

    Single-process, in-memory (a dict). A future multi-process deployment
    would back this with a shared store, but the optimistic window is short
    (default 300s) and the registry is consulted only by the same process
    that minted the handle, so in-memory is correct for the foundation.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._handles: dict[str, CompensationHandle] = {}

    def register(
        self,
        *,
        workspace_id: str,
        pocket_id: str,
        action: str,
        compensate: CompensateSpec,
        now: datetime | None = None,
    ) -> str:
        """Register a compensation handle; return its id (a UUID).

        The id keys the rollback endpoint the client receives on an
        ``optimistic_proceed`` response.
        """
        ts = now or datetime.now(UTC)
        cid = str(uuid.uuid4())
        self._handles[cid] = CompensationHandle(
            compensation_id=cid,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            compensate=compensate,
            registered_at=ts,
            expires_at=ts + self._ttl,
        )
        return cid

    def get(self, compensation_id: str) -> CompensationHandle | None:
        """Return the live handle, or ``None`` if unknown / already gone."""
        return self._handles.get(compensation_id)

    def pop(self, compensation_id: str) -> CompensationHandle | None:
        """Remove and return a handle (the rollback / consume path)."""
        return self._handles.pop(compensation_id, None)

    def sweep_expired(self, *, now: datetime | None = None) -> list[CompensationHandle]:
        """Expire every handle past its TTL; return the expired handles.

        For each expired handle: fire an ALERT audit event, persist it to
        the expired-compensations JSONL, then purge it from memory. The
        purge happens even if the persist raises (MF-8) so a disk error
        cannot leak the registry.
        """
        ts = now or datetime.now(UTC)
        expired = [h for h in self._handles.values() if h.expires_at <= ts]
        for handle in expired:
            self._on_expiry(handle)
            # purge unconditionally — even if _on_expiry's persist failed.
            self._handles.pop(handle.compensation_id, None)
        return expired

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_expiry(self, handle: CompensationHandle) -> None:
        """Fire the ALERT audit event + persist the expired handle.

        Audit first (it's the loud signal an operator must see), then
        persist. Both are wrapped so a failure in one does not prevent the
        purge in ``sweep_expired``.
        """
        try:
            get_audit_logger().log(
                AuditEvent.create(
                    severity=AuditSeverity.ALERT,
                    actor="system:triager",
                    action="instinct_optimistic_expired",
                    target=f"compensation:{handle.compensation_id}",
                    status="error",
                    workspace_id=handle.workspace_id,
                    pocket_id=handle.pocket_id,
                    action_name=handle.action,
                    compensation_id=handle.compensation_id,
                    registered_at=handle.registered_at.isoformat(),
                    expires_at=handle.expires_at.isoformat(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - audit must never block purge
            logger.warning(
                "optimistic-compensation: audit emit failed for %s (%s)",
                handle.compensation_id,
                exc,
            )
        try:
            self._persist_expired(handle)
        except Exception as exc:  # noqa: BLE001 - persist is best-effort (MF-8)
            logger.warning(
                "optimistic-compensation: expired-JSONL persist failed for %s (%s)",
                handle.compensation_id,
                exc,
            )

    def _persist_expired(self, handle: CompensationHandle) -> None:
        """Append the expired handle to the workspace JSONL at 0700/0600."""
        d = Path.home().joinpath(".pocketpaw", *_EXPIRED_SUBDIR)
        d.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        try:
            os.chmod(d, _DIR_MODE)
        except OSError:  # pragma: no cover - platform-dependent
            pass
        path = d / f"{handle.workspace_id}.jsonl"
        row = {
            "compensation_id": handle.compensation_id,
            "workspace_id": handle.workspace_id,
            "pocket_id": handle.pocket_id,
            "action": handle.action,
            "compensate": handle.compensate.model_dump(),
            "registered_at": handle.registered_at.isoformat(),
            "expires_at": handle.expires_at.isoformat(),
        }
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        finally:
            try:
                os.chmod(path, _FILE_MODE)
            except OSError:  # pragma: no cover
                pass


__all__ = ["CompensationHandle", "OptimisticCompensationRegistry"]
