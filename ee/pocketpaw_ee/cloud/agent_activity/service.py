# service.py — folds a workspace's recent runs into a per-agent activity board.
#
# Created: 2026-07-28 (feat/cockpit-agent-activity, HR-12a) — all the logic of
# this surface lives here; the router is a thin HTTP shell.
#
# This module never imports a Beanie document. ``ChatRunDoc`` is owned by
# ``chat.runs.service`` (EE Rule 1), which hands back ``RunActivityRow`` value
# objects; the workspace filter and the window therefore live at the query, not
# in a post-filter here — there is no code path in which rows for another
# workspace reach this fold at all.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pocketpaw.mission_control.models import AgentStatus
from pocketpaw_ee.cloud.agent_activity.dto import AgentActivityOut, AgentActivityResponse
from pocketpaw_ee.cloud.chat.runs import service as runs_service
from pocketpaw_ee.cloud.chat.runs.domain import RunActivityRow

# How far back a run counts as "recent". Bounds every query this module makes
# and keeps the read on the indexed prefix of ChatRunDoc's
# (workspace, context_type, scope_id, createdAt) index. An agent whose last run
# fell out of this window drops off the board.
RECENT_WINDOW = timedelta(hours=24)

# Hard caps on rows pulled per request, so one busy workspace can never turn a
# board refresh into an unbounded scan. The active cap is the smaller of the two
# because concurrent runs are naturally few; the recent cap is what a very busy
# workspace's 24h of chat turns is truncated to (newest first — see
# ``find_recent_runs_for_workspace``).
MAX_ACTIVE_RUNS_SCANNED = 200
MAX_RECENT_RUNS_SCANNED = 500

# A terminal run in one of these states means the agent stopped short of an
# answer, which is the "needs a human" signal BLOCKED carries on the cockpit.
# ``cancelled`` is excluded deliberately: a user who stops their own turn has
# not blocked the agent, so that agent reads IDLE.
_BLOCKED_STATUSES = frozenset({"failed", "interrupted"})


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime so timestamps are always comparable.

    Runs are written with tz-aware UTC, but a document that round-trips through
    BSON comes back naive — mixing the two raises TypeError on comparison.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _last_activity_at(row: RunActivityRow) -> datetime:
    """When this run last changed state: its end, else its start, else creation.

    A live run has no ``ended_at``, so it reports when it started; a queued run
    has neither and reports when it was created.
    """
    return _as_utc(row.ended_at or row.started_at or row.created_at)


def _newest_first(iso_timestamp: str) -> float:
    """Sort key that orders ISO-8601 timestamps newest-first inside an
    otherwise ascending tuple sort."""
    return -datetime.fromisoformat(iso_timestamp).timestamp()


def _status_for(rows: list[RunActivityRow], newest: RunActivityRow) -> AgentStatus:
    """Map an agent's runs onto the Mission Control status vocabulary.

    ACTIVE beats everything: an agent with work in flight is working, whatever
    its previous turn did. Otherwise the newest run decides — it failed or was
    interrupted (BLOCKED), or it finished (IDLE).
    """
    if any(r.status in runs_service.ACTIVE_RUN_STATUSES for r in rows):
        return AgentStatus.ACTIVE
    if newest.status in _BLOCKED_STATUSES:
        return AgentStatus.BLOCKED
    return AgentStatus.IDLE


async def build_activity(
    workspace_id: str,
    *,
    now: datetime | None = None,
) -> AgentActivityResponse:
    """Build the agent-activity board for one workspace.

    Two windowed reads rather than one: the recent read is capped, so on a very
    busy workspace a long-running turn started hours ago could be truncated out
    of it — and losing precisely the agent that IS working would defeat the
    board. The active read carries those rows independently of that cap.

    Agents with no run in ``RECENT_WINDOW`` are OMITTED rather than reported
    ``OFFLINE``. Rendering OFFLINE would mean knowing the workspace's full agent
    roster, which this module deliberately does not read (it would couple an
    activity view to the agents entity); and a configured-but-unused agent is
    not "not responding" — it simply has no activity to show. A client that
    wants an exhaustive roster joins this board against ``GET /agents`` and
    treats the absent ones as offline.

    ``now`` is injectable so tests can pin the window edge.
    """
    now = now or datetime.now(UTC)
    since = now - RECENT_WINDOW

    active_rows = await runs_service.find_active_runs_for_workspace(
        workspace_id=workspace_id,
        since=since,
        limit=MAX_ACTIVE_RUNS_SCANNED,
    )
    recent_rows = await runs_service.find_recent_runs_for_workspace(
        workspace_id=workspace_id,
        since=since,
        limit=MAX_RECENT_RUNS_SCANNED,
    )

    # The two reads overlap (an active run is also a recent run), so dedupe by
    # run_id or a live run would be counted twice in ``active_runs``.
    by_agent: dict[str, list[RunActivityRow]] = {}
    seen: set[str] = set()
    for row in (*active_rows, *recent_rows):
        if row.run_id in seen:
            continue
        seen.add(row.run_id)
        by_agent.setdefault(row.agent_id, []).append(row)

    agents: list[AgentActivityOut] = []
    for agent_id, rows in by_agent.items():
        # ``run_id`` breaks ties so two runs created in the same millisecond
        # can't reorder the board between two otherwise identical polls.
        newest = max(rows, key=lambda r: (_as_utc(r.created_at), r.run_id))
        agents.append(
            AgentActivityOut(
                agent_id=agent_id,
                status=_status_for(rows, newest).value,
                active_runs=sum(1 for r in rows if r.status in runs_service.ACTIVE_RUN_STATUSES),
                last_active=_last_activity_at(newest).isoformat(),
            )
        )

    # Working agents first, then most recently active, then by id for a stable
    # order across polls.
    agents.sort(
        key=lambda a: (
            a.status != AgentStatus.ACTIVE.value,
            _newest_first(a.last_active),
            a.agent_id,
        )
    )
    return AgentActivityResponse(agents=agents, ts=now.isoformat())
