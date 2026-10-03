# agent_health.py — /agent-health (and /agent-health-lab) surface preamble.
#
# Tells the agent the user is looking at agent run health and to answer from
# the ``pocketpaw_lens`` read tools, which this surface's profile grants (they
# exist on no other surface). ``meta.run_id`` is the run the user has open; it
# is echoed only when it is a 32-lowercase-hex trace id, so a client-sent value
# can never inject markup into the prompt. Reads nothing: the preamble is a pure
# function of the run id, hence ``meta_key``.

from __future__ import annotations

import re

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key

_TRACE_ID = re.compile(r"[0-9a-f]{32}")

_INSTRUCTION = (
    "<agent-health-orientation>\n"
    "The user is looking at the health of this workspace's agent runs (paw-lens "
    "traces). Answer from the lens tools, not guesses: "
    "`mcp__pocketpaw_lens__lens_overview` (headline numbers), `lens_runs` (recent "
    "runs, filter by agent / status / automation), `lens_run` (one run's spans and "
    "findings; pass span_id for a span's messages and tool call), `lens_issues` "
    "(recurring failures) and `lens_monitors` (scheduled automations). "
    "{focus}"
    "If a result says content_hidden, message content is visible to workspace "
    "admins only; say so instead of guessing what was said.\n"
    "</agent-health-orientation>"
)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the agent-health surface tag plus the lens-tools instruction."""
    run_id = meta.run_id if meta.run_id and _TRACE_ID.fullmatch(meta.run_id) else None
    if run_id:
        tag = f'<surface kind="agent_health" run_id="{run_id}" />'
        focus = f"The user has run {run_id} open: start with `lens_run` on it. "
    else:
        tag = '<surface kind="agent_health" />'
        focus = ""
    return SurfacePreamble(
        text=f"{tag}\n{_INSTRUCTION.format(focus=focus)}",
        cache_key=meta_key("agent_health", run_id),
    )


__all__ = ["build_preamble"]
