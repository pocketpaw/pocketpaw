# router.py — FastAPI router for the workspace agent-activity board (HR-12a).
#
# Created: 2026-07-28 (feat/cockpit-agent-activity) — one read-only route:
#   * GET /agent-activity — the caller's workspace, one entry per agent with a
#     run in the recent window.
#
# Discipline (ee/cloud 4-file rules): no logic and no Beanie doc here — the fold
# lives in service.py, and the runs it folds come from ``chat.runs.service``,
# the only module allowed to touch ``ChatRunDoc``. Mounted under /api/v1 from
# ee/pocketpaw_ee/cloud/__init__.py.
#
# Auth: MEMBER, via a new ``agent_activity.read`` action. It deliberately does
# NOT reuse the herdr cockpit's ADMIN ``cockpit.read``: that guards a host-level
# surface whose panes are not paw-workspace-scoped, which is precisely why it is
# admin-only. Different surface, different tenancy story, different action.
#
# THIS IS A TEAM BOARD — a deliberate decision, recorded here because it departs
# from a sibling in the same entity. ``chat.runs.router._authorize`` 404s a run
# belonging to another member of the same workspace, commented "so we don't leak
# run existence to a workspace teammate who didn't own the run". This board does
# NOT filter on ``user_id``: every member sees every agent's aggregate state.
#
# Why the departure is intended rather than an oversight: an Agent is a
# WORKSPACE resource, not a personal one — several members share one agent, and
# "is this agent busy right now" is the shared fact a team needs to coordinate
# around. What stays private is the individual turn: the board carries agent
# state, run COUNTS and timing, never message content, never ``user_id``, and
# (see dto.py) never a run id, so it cannot be used as a handle to open a
# teammate's run. ``/cloud/chat/runs/{run_id}/stream`` remains 404 for a
# non-owner, unchanged.
#
# If this should ever become a personal board, the change is one filter on both
# reads in ``chat.runs.service`` plus flipping ``test_team_board_shows_every_
# members_activity`` — which exists to make that a conscious edit, not a drift.
#
# v1 is a plain GET, polled by the client. An SSE stream like the cockpit's was
# rejected on cost: the cockpit re-polls a local subprocess for one operator,
# whereas this would be a Mongo query every 1.5s per connected user across the
# whole tenant base. The upgrade path when polling stops being enough is
# event-driven, not a faster poll: ``run_core`` already marks every run
# transition through ``chat.runs.service``, so those marks can publish a
# ``run.status_changed`` event on the realtime bus and this surface can gain a
# push stream that only wakes on a real state change.

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_action_any_workspace
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.agent_activity import service
from pocketpaw_ee.cloud.agent_activity.dto import AgentActivityResponse
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/agent-activity",
    tags=["Agent Activity"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "",
    response_model=AgentActivityResponse,
    dependencies=[Depends(require_action_any_workspace("agent_activity.read"))],
)
async def get_agent_activity(
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
) -> AgentActivityResponse:
    """Which of the caller's agents are working right now.

    One entry per agent with at least one run in the last
    ``service.RECENT_WINDOW``; agents with nothing recent are omitted. Tenancy
    comes from the auth context — a ``workspace_id`` query param is rejected
    rather than ignored, so a caller can never read another workspace's board
    and nobody can later make that param real by accident.
    """
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "agent_activity.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )
    return await service.build_activity(workspace_id)
