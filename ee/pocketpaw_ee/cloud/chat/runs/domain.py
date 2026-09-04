"""Value objects for chat runs. ``RunSpec`` must survive an arq pickle
round-trip — primitives only.

Changes: 2026-06-05 (fix/sites-surface-through-runspec) — ``RunSpec`` grows
``surface`` + ``surface_meta``. The HTTP handler resolves the per-turn
``SurfaceContext`` but submits a ``RunSpec`` to the run executor, which
rebuilds its own ctx from this spec — so without these fields the surface
hint was dropped at the boundary and the whole SurfaceProfile gate (tool-deny,
ripple-block omission, preamble, create-svelte-site skill) silently no-oped on
the real ``/agent`` path. Both default to the legacy shape (``None`` / ``{}``),
which the resolver turns into a GENERIC context with an empty deny — so
non-/sites and older clients are unchanged.

Changes: 2026-07-08 (CS-13, feat/per-send-model-override) — ``RunSpec`` grows
``model_override``: the optional per-send model id from ``CloudAgentChatRequest.model``.
Same boundary reason as ``surface`` — the HTTP handler has the value but submits a
``RunSpec`` to the executor, so without carrying it the executor's rebuilt ctx would
never see the client's model choice. ``None`` (the default / older clients) leaves the
backend's own model selection untouched, byte-identical to today. It's a bare ``str``,
so it survives the pickle round-trip like every other field.

Changes: 2026-07-28 (HR-12a, feat/cockpit-agent-activity) — added
``RunActivityRow``, the read-side projection of a run. ``ChatRunDoc`` is owned by
``chat.runs.service`` (EE Rule 1: Beanie only from service.py), so a consumer
outside this entity — ``ee.cloud.agent_activity``, which answers "which of my
agents are working right now" — reads runs as these Beanie-free value objects
rather than importing the document class.

Changes: 2026-09-04 (fix/unblock-event-loop, backend-perf M7) — this module also
holds the per-run TIMEOUT resolver now. It used to be private to ``worker.py``,
which meant the SSE reader in ``router.py`` could not see it: the stream loop
had no maximum lifetime at all, so a run whose terminal event never arrived
(worker OOM-killed after the events key existed, which defeats the
``stream_exists`` fallback) heartbeated forever, holding a blocked Redis
connection and a live asyncio task per abandoned client. The stream's natural
bound is the run's own timeout, and the two must not drift apart — raising
``POCKETPAW_CLOUD_RUN_JOB_TIMEOUT`` has to lengthen the stream cap too, or the
cap starts killing streams of runs that are still legitimately working. So both
read one resolver. It lives here rather than in ``worker.py`` because
``router.py`` importing the worker would pull arq and the whole executor graph
into the web process. Same reasoning, and the same shape, as
``jobs/domain.py::job_timeout_seconds``.

Changes: 2026-07-26 (concierge transcripts) — ``RunSpec`` grows
``persist_user_text``: the user's message text to WRITE DOWN on the run doc, as
opposed to ``content``, which is what the agent is asked. Every authed surface
leaves it "" because it already persisted the user turn as its own Message row;
the CONCIERGE surface sets it (when the site's retention toggle allows) because
its anonymous visitor has no Message row, so the run doc is the only place the
visitor half of a transcript can live. A bare ``str``, so it pickles like the
rest."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# arq's DEFAULT job_timeout is 300s, which CANCELS a long chat run mid-generation,
# so a big coding task halts after ~5 minutes. 30 minutes instead; the 10-minute
# stale-run sweeper remains the backstop against a genuinely runaway run holding
# a worker slot.
DEFAULT_RUN_JOB_TIMEOUT_SECONDS = 1800  # 30 minutes

# How long the SSE reader keeps a stream open PAST the run's own timeout before
# giving up. A run cancelled at the timeout boundary still has to write its
# terminal frame and have the reader observe it, and ``read_events`` blocks in
# 15s slices — so a cap equal to the job timeout would race the very frame it is
# waiting for and report a spurious error on a healthy run.
STREAM_LIFETIME_GRACE_SECONDS = 120


def run_job_timeout_seconds() -> int:
    """Resolve the per-run arq job_timeout from ``POCKETPAW_CLOUD_RUN_JOB_TIMEOUT``.

    Defaults to 30 minutes. An unparseable or non-positive value falls back to the
    default (rather than 0 / negative, which would disable or break the cap), so a
    typo can't silently let runs run forever or crash the worker.
    """
    raw = os.environ.get("POCKETPAW_CLOUD_RUN_JOB_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_RUN_JOB_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_CLOUD_RUN_JOB_TIMEOUT=%r is not an int; using default %ds",
            raw,
            DEFAULT_RUN_JOB_TIMEOUT_SECONDS,
        )
        return DEFAULT_RUN_JOB_TIMEOUT_SECONDS
    if val <= 0:
        logger.warning(
            "POCKETPAW_CLOUD_RUN_JOB_TIMEOUT=%d is not positive; using default %ds",
            val,
            DEFAULT_RUN_JOB_TIMEOUT_SECONDS,
        )
        return DEFAULT_RUN_JOB_TIMEOUT_SECONDS
    return val


def stream_max_lifetime_seconds() -> int:
    """Hard ceiling on one SSE subscription, derived from the run's own timeout.

    Derived rather than configured on purpose: an independent knob would drift,
    and the failure mode of drift is a cap SHORTER than the run it is watching,
    which severs healthy long runs and looks exactly like a backend bug.
    """
    return run_job_timeout_seconds() + STREAM_LIFETIME_GRACE_SECONDS


class RunSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    workspace_id: str
    context_type: str
    scope_id: str
    session_key: str
    group: str | None
    user_id: str
    agent_id: str
    client_message_id: str
    user_message_id: str
    content: str
    # The user's message text to PERSIST on the run doc (``ChatRunDoc.user_text``).
    # Distinct from ``content``: content is what the agent is asked, this is what we
    # choose to write down. "" on every authed surface (they persist a Message row
    # instead); set by the concierge surface when the site allows it.
    persist_user_text: str = ""
    history: list[dict[str, str]]
    intent: str | None
    attachments: list[dict[str, Any]] = []
    mentions: list[str] = []
    reply_to: str | None = None
    # Per-turn surface hint, mirrored from ``CloudAgentChatRequest`` so the
    # executor can re-resolve ``ctx.surface_context`` (the HTTP handler's
    # resolution doesn't survive the submit). ``None`` / ``{}`` keep the
    # legacy path (GENERIC context, empty deny).
    surface: str | None = None
    surface_meta: dict[str, Any] = Field(default_factory=dict)
    # Per-send model override (CS-13), mirrored from ``CloudAgentChatRequest.model``.
    # The executor rebuilds its own ctx from this spec, so the client's model choice
    # must ride the spec to survive the submit. ``None`` = backend picks the model
    # (the legacy path). Validated at the HTTP edge before it ever reaches here.
    model_override: str | None = None
    # Studio Flow build context, mirrored from ``CloudAgentChatRequest.flow_context``
    # so the executor (which rebuilds its own ctx from this spec) can inject the
    # ACTIVE FLOW ID into the agent's prompt and drive ``build_studio_flow`` into
    # the right flow project. ``None`` = no flow context (every non-studio run).
    flow_context: dict[str, Any] | None = None


class RunActivityRow(BaseModel):
    """One run flattened to the fields an activity view needs (HR-12a).

    The read-side counterpart to ``RunSpec``: no content, no history, no usage —
    just who ran, in what state, and when. Beanie-free by design so
    ``ee.cloud.agent_activity`` can fold runs into a per-agent board without
    importing ``ChatRunDoc`` (which only ``chat.runs.service`` may touch).

    ``workspace`` is deliberately absent: every read that produces these rows is
    already filtered to one workspace by the service, so carrying the tenant key
    onto the wire-adjacent projection would invite a caller to filter on it
    themselves instead of at the query.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    agent_id: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
