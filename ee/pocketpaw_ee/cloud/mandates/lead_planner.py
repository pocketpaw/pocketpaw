# ee/pocketpaw_ee/cloud/mandates/lead_planner.py — the feature-lead shift plan.
#
# A mandate whose charter says ``run_kind: "feature-lead"`` plans without the
# foreman: ONE ``feature-lead`` run per free feature issue, every shift. The
# foreman may split, merge or skip issues; a lead line needs exactly one run per
# issue, so its plan is deterministic. Pure: no I/O, no clock.
#
# ``plan`` takes the free sightings (the caller has already dropped the ones in
# flight, blocked by an open dependency or closed: ``service._backlog``), keeps
# those naming a GitHub issue, orders them p1 before p2 before p3 (issue labels,
# unlabelled last), then oldest first, and plans one task per sighting up to the
# shift's ``budget``: title = the issue title, why = a one-line pointer to the
# issue (``#<n>``, which the cloud station reads the lead's issue from), recipe
# = ``feature-lead``, evidence = the sighting. No free issue stands the shift
# down. The plan needs no human: each feature issue comes from an approved
# blueprint and packet, so the shift trigger approves it as ``APPROVER``.

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud.mandates.domain import LEAD_RUN_KIND
from pocketpaw_ee.cloud.mandates.foreman import PlannedTask, PlanProposal

APPROVER = "system:lead-planner"

# ``github:<owner>/<repo>#<n>``, the issues patrol's (and a hand-filed issue's) source.
_SOURCE = re.compile(r"^github:([\w.-]+/[\w.-]+)#(\d+)$")
_PRIORITY = {"p1": 1, "p2": 2, "p3": 3}
_UNRANKED = len(_PRIORITY) + 1


def priority(labels: list[str]) -> int:
    """1 for a ``p1`` issue, 2 for ``p2``, 3 for ``p3`` (the highest wins), 4 unlabelled."""
    ranks = [_PRIORITY.get(str(lb).strip().lower(), _UNRANKED) for lb in labels or []]
    return min(ranks, default=_UNRANKED)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _task(sighting: dict[str, Any], number: str) -> PlannedTask:
    ev = sighting.get("evidence") or {}
    title = str(ev.get("title") or "").strip() or f"Feature issue #{number}"
    url = str(ev.get("url") or "").strip()
    return PlannedTask(
        title=title,
        why=f"Feature issue #{number}" + (f": {url}" if url else ""),
        evidence_refs=[str(sighting["id"])],
        expected_outcome=(
            f"#{number} shipped by its feature lead: a merged feature-done PR, "
            "dev CI green and a contact sheet on the issue"
        ),
        recipe=LEAD_RUN_KIND,
    )


def plan(shift_no: int, sightings: list[dict[str, Any]], budget: int) -> PlanProposal:
    """One ``feature-lead`` task per free feature issue in ``sightings`` (each
    ``{id, evidence, ts}``), p1 first, then oldest, at most ``budget``."""
    issues = []
    for s in sightings:
        found = _SOURCE.match(str((s.get("evidence") or {}).get("source") or ""))
        if found:
            issues.append((s, found[2]))
    issues.sort(
        key=lambda pair: (
            priority(list((pair[0].get("evidence") or {}).get("labels") or [])),
            _aware(pair[0]["ts"]),
        )
    )
    tasks = [_task(s, number) for s, number in issues[: max(0, budget)]]
    if not tasks:
        return PlanProposal(
            shift_no=shift_no, no_action=True, no_action_reason="no free feature issue"
        )
    return PlanProposal(shift_no=shift_no, tasks=tasks)


__all__ = ["APPROVER", "plan", "priority"]
