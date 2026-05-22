# service.py — Pocket-outcomes entity business logic.
# Created: 2026-05-22 (RFC 05 M2b.2) — the minimal outcome meter.
#   `emit_pocket_outcome` is the producer: it builds and emits a
#   `pocket.outcome` event after a successful write (a no-op when the
#   binding declared no `outcome`). `record_outcome` is the consumer —
#   the `pocket.outcome` bus subscriber that appends one JSON line to a
#   workspace-scoped ledger file. `count_outcomes` reads a workspace's
#   ledger back and groups the rows. There is no Beanie here — the ledger
#   is an append-only JSONL file, so this entity's "repository" is the
#   filesystem. No billing: the Layer-4 `outcome_value`/`outcome_unit`
#   slots stay null.
#
#   The ledger lives at `<dir>/<workspace_id>.jsonl`. `<dir>` defaults to
#   `~/.pocketpaw/outcomes/` and is overridable via `set_ledger_dir` so
#   tests write to a tmp path instead of the real home directory.
#
# Updated: 2026-05-22 (security-review fix for PR #1183, SHOULD-FIX 3) —
#   `count_outcomes` now skips any ledger row whose `workspace_id` does
#   not match the caller's workspace. The ledger file is already keyed by
#   workspace, so this is defense-in-depth: a corrupt or hand-edited line
#   carrying a foreign tenant id is no longer counted into the totals.
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import PocketOutcomeEvent
from pocketpaw_ee.cloud.outcomes.domain import OutcomeRecord
from pocketpaw_ee.cloud.outcomes.dto import CountOutcomesRequest, OutcomeCountResponse

logger = logging.getLogger(__name__)

# Module-level ledger directory. Default is the real home dir; tests call
# ``set_ledger_dir(tmp_path)`` so they never touch `~/.pocketpaw`.
_LEDGER_DIR: Path = Path.home() / ".pocketpaw" / "outcomes"


def set_ledger_dir(path: str | Path) -> None:
    """Override the outcomes ledger directory (test seam)."""
    global _LEDGER_DIR
    _LEDGER_DIR = Path(path)


def _ledger_path(workspace_id: str) -> Path:
    """Return the JSONL ledger path for a workspace.

    A workspace id is an opaque token (Mongo ObjectId hex or a test
    string); ``Path`` joins it as a single filename. An empty id would
    collide on a bare ``.jsonl`` — callers guard against that upstream
    (a record with no workspace is dropped in ``record_outcome``).
    """
    return _LEDGER_DIR / f"{workspace_id}.jsonl"


async def emit_pocket_outcome(
    *,
    outcome: str | None,
    pocket_id: str,
    workspace_id: str,
    action: str,
    actor: str,
    via_instinct: bool,
    instinct_action_id: str | None = None,
) -> None:
    """Emit a ``pocket.outcome`` event for a successful write action.

    Called by the pockets router (a direct, non-gated write) and by
    ``instinct_bridge.execute_approved_write`` (a write fired after
    Instinct approval) AFTER ``run_action`` returns ``ok:true``.

    A binding that declared no ``outcome`` passes ``outcome=None`` — this
    is a NO-OP, no event is emitted. Only a named outcome produces an
    event. ``emit`` itself never raises, so a bus failure here can never
    break the write that already succeeded.

    ``outcome_value`` / ``outcome_unit`` are emitted as ``None`` — Layer 4
    (billing) is reserved and this build never assigns a monetary value.
    """
    if not outcome:
        # No declared outcome — nothing to meter. The write still
        # succeeded; it just isn't a named business event.
        return
    await emit(
        PocketOutcomeEvent(
            data={
                "outcome": outcome,
                "pocket_id": pocket_id,
                "workspace_id": workspace_id,
                "action": action,
                "actor": actor,
                "via_instinct": via_instinct,
                "instinct_action_id": instinct_action_id,
                "occurred_at": datetime.now(UTC).isoformat(),
                # Layer 4 reserved — billing is not wired in this build.
                "outcome_value": None,
                "outcome_unit": None,
            }
        )
    )


async def record_outcome(event) -> None:  # type: ignore[no-untyped-def]
    """Append a ``pocket.outcome`` event to its workspace ledger.

    Registered as an in-process bus subscriber in ``mount_cloud()``. The
    bus already isolates a subscriber failure (logs + swallows), so a
    bad write here can never break the originating pocket write — but we
    still guard defensively and log, because a silently-dropped outcome
    is a metering gap an operator should see in the logs.

    ``event`` is a ``PocketOutcomeEvent``; its ``data`` dict is the
    payload built by ``emit_pocket_outcome``. A payload missing
    ``workspace_id`` or ``outcome`` is dropped — the ledger is keyed by
    workspace and a row with no outcome name is uncountable.
    """
    data = getattr(event, "data", None) or {}
    workspace_id = data.get("workspace_id")
    outcome = data.get("outcome")
    if not workspace_id or not outcome:
        logger.warning("pocket.outcome event dropped — missing workspace_id or outcome")
        return

    record = OutcomeRecord(
        outcome=str(outcome),
        pocket_id=str(data.get("pocket_id") or ""),
        workspace_id=str(workspace_id),
        action=str(data.get("action") or ""),
        actor=str(data.get("actor") or ""),
        via_instinct=bool(data.get("via_instinct")),
        instinct_action_id=data.get("instinct_action_id"),
        occurred_at=str(data.get("occurred_at") or ""),
        # Layer 4 reserved — never set here.
        outcome_value=None,
        outcome_unit=None,
    )
    path = _ledger_path(record.workspace_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("failed to append pocket.outcome to ledger %s", path, exc_info=True)


async def count_outcomes(
    workspace_id: str,
    body: CountOutcomesRequest | dict | None = None,
) -> OutcomeCountResponse:
    """Count a workspace's recorded outcomes, grouped by name and pocket.

    Reads the workspace JSONL ledger, filters by the optional
    ``pocket_id`` / ``since`` (inclusive ISO-8601 lower bound on
    ``occurred_at``), and returns the totals. A workspace with no ledger
    file yet returns zero counts — not an error.
    """
    body = CountOutcomesRequest.model_validate(body or {})
    path = _ledger_path(workspace_id)
    if not workspace_id or not path.exists():
        return OutcomeCountResponse(total=0)

    total = 0
    by_outcome: dict[str, int] = {}
    by_pocket: dict[str, int] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    # A torn write or hand-edited line — skip it rather
                    # than failing the whole count.
                    continue
                if not isinstance(row, dict):
                    continue
                # SHOULD-FIX 3 (PR #1183) — defense-in-depth tenant
                # filter. The ledger file is already keyed by workspace
                # (`<dir>/<workspace_id>.jsonl`), but a corrupt or
                # hand-edited line carrying a foreign `workspace_id`
                # must NOT be counted into this workspace's totals.
                if str(row.get("workspace_id") or "") != workspace_id:
                    continue
                if body.pocket_id is not None and row.get("pocket_id") != body.pocket_id:
                    continue
                if body.since is not None and str(row.get("occurred_at") or "") < body.since:
                    continue
                total += 1
                name = str(row.get("outcome") or "")
                pocket = str(row.get("pocket_id") or "")
                by_outcome[name] = by_outcome.get(name, 0) + 1
                by_pocket[pocket] = by_pocket.get(pocket, 0) + 1
    except OSError:
        logger.warning("failed to read outcomes ledger %s", path, exc_info=True)
        return OutcomeCountResponse(total=0)

    return OutcomeCountResponse(total=total, by_outcome=by_outcome, by_pocket=by_pocket)


__all__ = [
    "emit_pocket_outcome",
    "record_outcome",
    "count_outcomes",
    "set_ledger_dir",
]
