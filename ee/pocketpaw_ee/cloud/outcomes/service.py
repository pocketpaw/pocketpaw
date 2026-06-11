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
# Updated: 2026-05-25 (RFC 07 Slice 2 — outcomes back-reference) —
#   `emit_pocket_outcome` now accepts an optional `decision_id` and the
#   bus listener (`record_outcome`) threads it onto the ledger row AND
#   feeds a synthetic `decision.outcome_attached` event into the
#   in-process DecisionProjection so the Decision row in the decision
#   graph mutates its outcome field in place. The journal subscription
#   that would do the same end-to-end is deferred to a follow-up; in
#   this PR the back-reference flows synchronously from the outcome
#   listener so the test suite can pin the contract.
#   `"decision.outcome_attached"` is used as a STRING LITERAL (the
#   namespace registration in soul-protocol is parallel work — Slice 0).
# Updated: 2026-06-11 (gap-3 outcome VALUE metering) — Layer 4 is no longer
#   hardcoded `None`. `emit_pocket_outcome` now accepts `outcome_value` /
#   `outcome_unit` (the author-time billable pair declared on the binding)
#   and `record_outcome` persists them on the ledger row (via the defensive
#   `_coerce_value` + whole-pair guard, so a torn half-pair degrades to a
#   count-only row rather than corrupting the total). New `meter_outcomes`
#   is the aggregation READ surface: it sums value BY unit over a
#   since/until window per workspace and returns count + total_value per
#   unit — the queryable "pay for governed outcomes" figure. Workspace-
#   scoped with the same defense-in-depth tenant filter as `count_outcomes`.
#   Deferred (NOT here): invoicing, payment, currency, pricing-rules engine,
#   disputes/clawback (outcome-spec.md Decisions 4/5/7).
# Updated: 2026-06-11 (gap-housekeeping) — `meter_outcomes` no longer compares
#   the period window lexicographically on raw ISO strings. The new `_parse_ts`
#   helper normalizes the stored `occurred_at` AND the since/until bounds to
#   UTC-aware datetimes before comparing, so the same instant written as `...Z`,
#   `...+00:00`, or an offset (`...-04:00`) sorts correctly into the window. The
#   since-inclusive / until-exclusive contract is unchanged; a torn/unparseable
#   timestamp falls back to the old byte-wise compare rather than crashing.
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import PocketOutcomeEvent
from pocketpaw_ee.cloud.outcomes.domain import OutcomeRecord
from pocketpaw_ee.cloud.outcomes.dto import (
    CountOutcomesRequest,
    MeterOutcomesRequest,
    MeterOutcomesResponse,
    OutcomeCountResponse,
    UnitMeter,
)

logger = logging.getLogger(__name__)

# Module-level ledger directory. Default is the real home dir; tests call
# ``set_ledger_dir(tmp_path)`` so they never touch `~/.pocketpaw`.
_LEDGER_DIR: Path = Path.home() / ".pocketpaw" / "outcomes"


def set_ledger_dir(path: str | Path) -> None:
    """Override the outcomes ledger directory (test seam)."""
    global _LEDGER_DIR
    _LEDGER_DIR = Path(path)


def _parse_ts(raw: object) -> datetime | None:
    """Parse an ISO-8601 timestamp to a timezone-aware UTC ``datetime``.

    The stored ``occurred_at`` and the ``since`` / ``until`` bounds can each
    arrive in a DIFFERENT textual form for the SAME instant — ``...Z``,
    ``...+00:00``, or an offset like ``...-04:00``. A lexicographic string
    compare sorts those wrong (``"...Z" > "...+00:00"`` byte-wise even though
    they are equal instants, and an offset timestamp sorts on its local digits,
    not its UTC instant). Parsing to a UTC-normalized ``datetime`` makes the
    window comparison correct regardless of representation.

    ``Z`` is rewritten to ``+00:00`` for ``fromisoformat`` (Python < 3.11 does
    not accept ``Z``). A naive timestamp (no offset) is assumed UTC — the
    producer stamps ``datetime.now(UTC)``, so a stored value is always
    UTC-aware, and a hand-edited naive value is most safely read as UTC.
    Returns ``None`` when the value is empty or unparseable, so the caller can
    fall back to the byte-wise compare rather than crash on a torn row.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _coerce_value(raw: object) -> float | None:
    """Coerce a raw outcome value to ``float``, or ``None`` if it can't be.

    The value arrives off a bus event payload (or, on count, off a JSON
    ledger row), so it may be ``int``, ``float``, a numeric string, or
    junk. A bool is rejected explicitly — ``bool`` is an ``int`` subclass
    in Python and ``True`` would silently become ``1.0``. Anything that
    does not parse drops the row to count-only rather than corrupting the
    billable total.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
    decision_id: str | None = None,
    outcome_value: float | None = None,
    outcome_unit: str | None = None,
) -> None:
    """Emit a ``pocket.outcome`` event for a successful write action.

    Called by the pockets router (a direct, non-gated write) and by
    ``instinct_bridge.execute_approved_write`` (a write fired after
    Instinct approval) AFTER ``run_action`` returns ``ok:true``.

    A binding that declared no ``outcome`` passes ``outcome=None`` — this
    is a NO-OP, no event is emitted. Only a named outcome produces an
    event. ``emit`` itself never raises, so a bus failure here can never
    break the write that already succeeded.

    ``outcome_value`` / ``outcome_unit`` are the gap-3 billable-value pair.
    A producer that resolved a per-action value (from the binding's
    author-time declaration) passes them here; they ride the event onto the
    ledger row so the aggregation surface can sum value BY unit per
    workspace. Both default to ``None`` — a count-only outcome (no monetary
    value) leaves the meter exactly as it was before pricing. The binding
    validator guarantees the pair is whole (both set or both ``None``) by
    the time it reaches here, so the meter never sees a value with no unit.

    ``decision_id`` is the RFC 07 Slice 2 back-reference — pass the id
    of the Decision in the decision graph that this outcome resolved,
    when the caller has one in hand. ``None`` is fine for writers that
    don't yet know their Decision (legacy producers); the listener
    only fires the back-reference path when the id is present.
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
                # gap-3 — the billable value/unit. `None`/`None` for a
                # count-only outcome; a real figure when the binding
                # declared one. The aggregation surface sums value by unit.
                "outcome_value": outcome_value,
                "outcome_unit": outcome_unit,
                # RFC 07 Slice 2 — back-reference to the decision graph.
                "decision_id": decision_id,
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

    decision_id_raw = data.get("decision_id")
    decision_id = str(decision_id_raw) if decision_id_raw else None

    # gap-3 — the billable value/unit. The pair is whole or absent (the
    # binding validator enforced it upstream), but `record_outcome` is the
    # ledger's last gate and also runs on hand-published bus events, so be
    # defensive: a value that does not coerce to float, or a value with no
    # unit, is dropped to a count-only row rather than persisting a figure
    # the aggregation cannot total.
    outcome_value = _coerce_value(data.get("outcome_value"))
    unit_raw = data.get("outcome_unit")
    outcome_unit = str(unit_raw) if unit_raw else None
    if outcome_value is None or outcome_unit is None:
        # Half a pair (or none) → count-only row. Never persist a lone
        # value or a lone unit; the rollup sums value strictly by unit.
        outcome_value = None
        outcome_unit = None

    record = OutcomeRecord(
        outcome=str(outcome),
        pocket_id=str(data.get("pocket_id") or ""),
        workspace_id=str(workspace_id),
        action=str(data.get("action") or ""),
        actor=str(data.get("actor") or ""),
        via_instinct=bool(data.get("via_instinct")),
        instinct_action_id=data.get("instinct_action_id"),
        occurred_at=str(data.get("occurred_at") or ""),
        # gap-3 — persist the billable value/unit (replaces the hardcoded
        # `None`). A count-only outcome leaves both `None`, the prior shape.
        outcome_value=outcome_value,
        outcome_unit=outcome_unit,
        # RFC 07 Slice 2 — back-reference into the decision graph.
        decision_id=decision_id,
    )
    path = _ledger_path(record.workspace_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("failed to append pocket.outcome to ledger %s", path, exc_info=True)

    # RFC 07 Slice 2 — back-reference flow. When the producer passed a
    # decision_id, fold a synthetic `decision.outcome_attached` event
    # into the in-process DecisionProjection so the Decision row mutates
    # its outcome field. The journal subscription that would do the same
    # end-to-end is deferred to a follow-up; for now the back-reference
    # rides on the outcome listener itself.
    if decision_id:
        await _attach_outcome_to_decision(record=record)


async def _attach_outcome_to_decision(*, record: OutcomeRecord) -> None:
    """Fold a synthetic `decision.outcome_attached` event into the
    decision projection so the Decision row's outcome field mutates
    in place.

    The projection's `_apply_outcome_attached` handler (see
    `decisions/projection.py`) looks up the Decision by the
    `decision_id` carried in the event payload, then writes the
    outcome via `store.update_outcome`. The hash chain stays valid —
    outcome is intentionally excluded from the hash material.

    Imports are local so the outcomes service stays importable in
    environments where the decisions module isn't wired (e.g. unit
    tests that mock the cloud bootstrap). Failures here are logged
    and swallowed — a bad back-reference can never break the write
    that already succeeded.
    """
    if not record.decision_id or not record.outcome:
        return

    try:
        decision_uuid = UUID(record.decision_id)
    except (TypeError, ValueError):
        logger.warning(
            "outcomes back-reference: decision_id %r is not a UUID — dropped",
            record.decision_id,
        )
        return

    try:
        from soul_protocol.spec.journal import Actor, EventEntry

        from pocketpaw_ee.cloud.decisions.service import get_decision_graph
    except Exception:  # noqa: BLE001
        logger.debug("decisions module unavailable — skipping outcome back-reference")
        return

    occurred_at: datetime
    try:
        occurred_at = datetime.fromisoformat(record.occurred_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        occurred_at = datetime.now(UTC)

    # The projection treats `decision.outcome_attached` as the late-
    # attach signal. Only the payload's `decision_id` is load-bearing;
    # the rest of the payload feeds the OutcomeRef the projection
    # writes (status, landed_at, metered).
    event = EventEntry(
        id=uuid4(),
        ts=occurred_at,
        actor=Actor(
            kind="system",
            id="system:outcomes",
            scope_context=[f"workspace:{record.workspace_id}"],
        ),
        action="decision.outcome_attached",  # string literal — namespace in Slice 0
        scope=[f"workspace:{record.workspace_id}"],
        payload={
            "decision_id": record.decision_id,
            # Stable id from the decision_id so re-emits are idempotent.
            # `_apply_outcome_attached` uses this if present, else mints
            # its own uuid — either way the Decision row converges to
            # the same outcome state.
            "outcome_id": str(decision_uuid),
            "status": "landed",
            "landed_at": record.occurred_at,
            "metered": record.outcome_value is not None,
        },
    )

    try:
        graph = get_decision_graph()
        graph.projection.apply(event)
    except Exception:  # noqa: BLE001
        logger.warning(
            "outcomes back-reference: failed to fold decision.outcome_attached "
            "for decision %s — outcome ledger row still written",
            record.decision_id,
            exc_info=True,
        )


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


async def meter_outcomes(
    workspace_id: str,
    body: MeterOutcomesRequest | dict | None = None,
) -> MeterOutcomesResponse:
    """Aggregate a workspace's governed outcomes into a billable figure.

    This is the gap-3 "pay for governed outcomes" READ surface. It reads
    the workspace JSONL ledger over the optional ``pocket_id`` /
    ``since`` (inclusive) / ``until`` (EXCLUSIVE) window, then rolls the
    value-bearing rows up BY unit: per unit it returns the count of
    outcomes and the SUM of their ``outcome_value``. Count-only rows (no
    value/unit) are tallied into ``total_outcomes`` but excluded from the
    per-unit billable rollup — there is nothing to bill.

    Workspace-scoped exactly like ``count_outcomes``: the file is keyed by
    workspace, and a defense-in-depth filter drops any row whose
    ``workspace_id`` does not match the caller's. A workspace with no
    ledger returns a zeroed meter, not an error.

    Deliberately NOT here: pricing rules, currency, invoicing, payment,
    disputes/clawback. ``total_value`` is a raw sum of declared values, the
    queryable primitive those layers will build on later.
    """
    body = MeterOutcomesRequest.model_validate(body or {})
    path = _ledger_path(workspace_id)
    if not workspace_id or not path.exists():
        return MeterOutcomesResponse(total_outcomes=0, metered_count=0)

    total_outcomes = 0
    metered_count = 0
    # Accumulate (count, summed_value) per unit before building the DTOs.
    unit_count: dict[str, int] = {}
    unit_value: dict[str, float] = {}
    # Parse the window bounds ONCE to UTC-normalized datetimes so the per-row
    # comparison is on instants, not raw ISO strings — a `Z`-vs-`+00:00`-vs-
    # offset representation of the same instant compares correctly. When a bound
    # is present but unparseable we keep its raw string for a defensive
    # lexicographic fallback rather than dropping the bound entirely.
    since_dt = _parse_ts(body.since) if body.since is not None else None
    until_dt = _parse_ts(body.until) if body.until is not None else None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                # Defense-in-depth tenant filter — same as count_outcomes.
                if str(row.get("workspace_id") or "") != workspace_id:
                    continue
                if body.pocket_id is not None and row.get("pocket_id") != body.pocket_id:
                    continue
                occurred = str(row.get("occurred_at") or "")
                occurred_dt = _parse_ts(occurred)
                # `since` is INCLUSIVE, `until` EXCLUSIVE so back-to-back periods
                # never double-count a boundary outcome. Compare as datetimes
                # when BOTH sides parsed; otherwise fall back to the byte-wise
                # string compare (a torn/hand-edited timestamp) so the row is
                # still filtered rather than crashing the meter.
                if body.since is not None:
                    in_window = (
                        occurred_dt >= since_dt
                        if (occurred_dt is not None and since_dt is not None)
                        else occurred >= body.since
                    )
                    if not in_window:
                        continue
                if body.until is not None:
                    before_until = (
                        occurred_dt < until_dt
                        if (occurred_dt is not None and until_dt is not None)
                        else occurred < body.until
                    )
                    if not before_until:
                        continue

                total_outcomes += 1

                value = _coerce_value(row.get("outcome_value"))
                unit_raw = row.get("outcome_unit")
                unit = str(unit_raw) if unit_raw else None
                # Only a WHOLE value/unit pair is billable. A count-only row
                # (or a torn half-pair) contributes to total_outcomes only.
                if value is None or unit is None:
                    continue
                metered_count += 1
                unit_count[unit] = unit_count.get(unit, 0) + 1
                unit_value[unit] = unit_value.get(unit, 0.0) + value
    except OSError:
        logger.warning("failed to read outcomes ledger %s", path, exc_info=True)
        return MeterOutcomesResponse(total_outcomes=0, metered_count=0)

    by_unit = {
        unit: UnitMeter(unit=unit, count=count, total_value=unit_value[unit])
        for unit, count in unit_count.items()
    }
    return MeterOutcomesResponse(
        total_outcomes=total_outcomes,
        metered_count=metered_count,
        by_unit=by_unit,
    )


__all__ = [
    "emit_pocket_outcome",
    "record_outcome",
    "count_outcomes",
    "meter_outcomes",
    "set_ledger_dir",
]
