# ee/pocketpaw_ee/cloud/pockets/trust_ledger.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T2) — the per-
# (workspace, pocket, action) trust score that feeds the layered/learning
# Instinct gate's lane classifier (2026-06-18 gate-layered-learning
# design). File-based, NOT Beanie: one append-only JSONL sidecar per
# workspace at ~/.pocketpaw/instinct/trust/<workspace_id>.jsonl, dir 0700 /
# file 0600 (same permission model as the internal-token + audit-log
# secrets). On the import-linter "Pockets" allowlist (no Beanie writes).
#
# Score model: each executed gated write appends one row tagged
# ``was_auto_approved`` (True when the system triager decided it, False
# when a human did — see instinct_bridge T8). ``get_trust_score`` returns
# (auto_count / total, total) over a rolling ``window_days`` window, where
# total is ``proposed_count`` — the warmup signal the classifier's
# cold-start floor reads (count==0 → ESCALATE).
#
# Reset: a backend-credential change invalidates accumulated trust for a
# pocket (anti-gaming — see design M-5). ``reset_pocket_trust`` appends a
# reset MARKER row; ``get_trust_score`` then counts only rows written
# AFTER the latest marker for that pocket.
#
# No cache: ``cachetools`` is not a current EE dependency (design M-7
# open-question #5), so this module reads the sidecar on every call. The
# files are small (one short line per executed write) and reads are
# infrequent relative to writes; a TTLCache can be layered later without
# changing the public surface.

"""File-based per-(workspace, pocket, action) trust ledger."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_TRUST_SUBDIR = ("instinct", "trust")
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _trust_dir() -> Path:
    """The per-host trust directory: ~/.pocketpaw/instinct/trust."""
    return Path.home().joinpath(".pocketpaw", *_TRUST_SUBDIR)


def _sidecar_path(workspace_id: str) -> Path:
    return _trust_dir() / f"{workspace_id}.jsonl"


def _ensure_trust_dir() -> Path:
    """Create the trust dir tree with 0700 perms and return it.

    ``mkdir(mode=0o700)`` is honored only for the leaf when the parents
    already exist with a wider mode; we ``chmod`` the leaf explicitly so a
    pre-existing ~/.pocketpaw at a wider mode cannot leave the trust dir
    group/other-readable.
    """
    d = _trust_dir()
    d.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    try:
        os.chmod(d, _DIR_MODE)
    except OSError as exc:  # pragma: no cover - platform-dependent
        logger.warning("trust_ledger: could not chmod %s (%s)", d, exc)
    return d


def _append_row(workspace_id: str, row: dict) -> None:
    """Append one JSON row to the workspace sidecar at 0600.

    Best-effort durability: the row is the trust feedback signal, not the
    write itself, so a failed append degrades trust accuracy but never
    blocks the (already-executed) write. The caller treats this as
    best-effort (see instinct_bridge T8).
    """
    _ensure_trust_dir()
    path = _sidecar_path(workspace_id)
    # Pin 0600 at create time so the file never exists at the default 0644
    # even briefly; chmod again after in case it pre-existed wider.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    finally:
        try:
            os.chmod(path, _FILE_MODE)
        except OSError as exc:  # pragma: no cover
            logger.warning("trust_ledger: could not chmod %s (%s)", path, exc)


def _read_rows(workspace_id: str) -> list[dict]:
    """Return every parsed row in the sidecar, oldest first.

    Malformed lines are skipped (forward-compat: a newer writer may add a
    row shape this reader doesn't recognize). Missing file → empty list.
    """
    path = _sidecar_path(workspace_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:  # pragma: no cover
        logger.warning("trust_ledger: could not read %s (%s)", path, exc)
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Treat naive timestamps as UTC so comparisons never raise.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def get_trust_score(
    workspace_id: str,
    pocket_id: str,
    action: str,
    window_days: int = 30,
) -> tuple[float, int]:
    """Return ``(score, proposed_count)`` for one (pocket, action) pair.

    * ``score`` — fraction of in-window executions that were
      auto-approved (``was_auto_approved=True``). ``0.0`` when there are no
      in-window rows.
    * ``proposed_count`` — total in-window executions for the pair. This is
      the warmup signal: ``0`` forces the classifier's cold-start ESCALATE.

    Rows older than ``window_days`` are excluded. Rows written before the
    latest ``reset_pocket_trust`` marker for this pocket are excluded
    (backend-change anti-gaming, design M-5).
    """
    rows = _read_rows(workspace_id)
    if not rows:
        return (0.0, 0)

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)

    # Find the latest reset marker for this pocket; rows at/before it don't
    # count.
    reset_ts: datetime | None = None
    for r in rows:
        if r.get("reset") and r.get("pocket_id") == pocket_id:
            ts = _parse_ts(r.get("ts"))
            if ts is not None and (reset_ts is None or ts > reset_ts):
                reset_ts = ts

    auto = 0
    total = 0
    for r in rows:
        if r.get("reset"):
            continue
        if r.get("pocket_id") != pocket_id or r.get("action") != action:
            continue
        ts = _parse_ts(r.get("ts"))
        if ts is None or ts < cutoff:
            continue
        if reset_ts is not None and ts <= reset_ts:
            continue
        total += 1
        if r.get("was_auto_approved") is True:
            auto += 1

    if total == 0:
        return (0.0, 0)
    return (auto / total, total)


async def record_correction(
    workspace_id: str,
    pocket_id: str,
    action: str,
    was_auto_approved: bool,
) -> None:
    """Append one trust-feedback row after an executed gated write.

    Called from EXACTLY ONE site — ``instinct_bridge.execute_approved_write``
    after ``mark_executed`` succeeds (design MF-3; ``outcomes/service.py``
    must NOT call this). ``was_auto_approved`` is ``True`` when the system
    triager decided the write, ``False`` when a human approver did.
    """
    _append_row(
        workspace_id,
        {
            "pocket_id": pocket_id,
            "action": action,
            "was_auto_approved": bool(was_auto_approved),
            "ts": datetime.now(UTC).isoformat(),
        },
    )


async def reset_pocket_trust(workspace_id: str, pocket_id: str) -> None:
    """Invalidate accumulated trust for every action on one pocket.

    Appends a reset MARKER row; subsequent ``get_trust_score`` calls count
    only rows written after this marker. Called by the pocket backend
    update path when a new base_url / credential is saved (design M-5) so a
    swapped backend cannot inherit the prior backend's earned trust.
    """
    _append_row(
        workspace_id,
        {
            "reset": True,
            "pocket_id": pocket_id,
            "ts": datetime.now(UTC).isoformat(),
        },
    )


__all__ = [
    "get_trust_score",
    "record_correction",
    "reset_pocket_trust",
]
