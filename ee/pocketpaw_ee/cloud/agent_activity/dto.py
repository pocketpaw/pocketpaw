# dto.py — wire schemas for the workspace agent-activity board.
#
# Created: 2026-07-28 (feat/cockpit-agent-activity, HR-12a) — response-only:
# the endpoint takes no body and no filters, so there is no request model.
# Tenancy is never a field here — the workspace comes from the auth context and
# is applied at the query (see router.py / service.py).
#
# ``status`` is a Mission Control ``AgentStatus`` value string, the same
# vocabulary the herdr cockpit's pane dots use, so one UI legend covers both
# boards.

from __future__ import annotations

from pydantic import BaseModel


class AgentActivityOut(BaseModel):
    """One agent's activity in the caller's workspace over the recent window.

    ``status`` is an ``AgentStatus`` value (``active`` | ``blocked`` | ``idle``).
    ``active_runs`` is how many of this agent's runs are currently ``queued`` or
    ``running`` — 0 whenever the status is not ``active``. ``last_active`` is an
    ISO-8601 UTC timestamp of the most recent state change on the agent's newest
    run (its end, else its start, else its creation).

    An agent with no run in the window is absent from the board entirely rather
    than reported ``offline`` — see ``service.build_activity``.

    Deliberately NOT here: a run id. This is a TEAM board (see the router
    header), so an agent's newest run often belongs to a different member — and
    ``chat.runs.router._authorize`` 404s another member's run by design. A run
    id would therefore be a handle the recipient cannot open, while still
    proving that specific run exists. Aggregate agent state is the shared fact;
    an individual member's turn is not.
    """

    agent_id: str
    status: str
    active_runs: int
    last_active: str


class AgentActivityResponse(BaseModel):
    """The workspace's agent-activity board at one instant.

    ``agents`` is ordered for display: working agents first, then most recently
    active. ``ts`` is the ISO-8601 UTC time the board was built — a polling
    client uses it to label staleness.
    """

    agents: list[AgentActivityOut]
    ts: str


__all__ = ["AgentActivityOut", "AgentActivityResponse"]
