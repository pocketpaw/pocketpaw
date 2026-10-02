"""Value objects and timing resolvers for chat runs.

``RunSpec`` is what the HTTP handler hands the run executor, and it must survive
an arq pickle round-trip, so every field is a primitive. The executor rebuilds
its own context from the spec, which is why per-turn choices the handler already
resolved (``surface``/``surface_meta``, ``model_override``, ``tools_enabled``,
``flow_context``, ``persist_user_text``) ride on it: a field left off is silently
dropped at the submit boundary.

``RunActivityRow`` and ``StrandedReply`` are Beanie-free read projections, so
consumers outside this entity never import ``ChatRunDoc`` (EE Rule 2).

The timing resolvers live here rather than in ``worker.py`` so the web process
(the SSE reader, the sweeper, the arq executor) can read them without importing
arq's worker graph. They derive from each other on purpose:
``stream_max_lifetime_seconds`` is the job timeout plus a grace, and
``queued_stream_ttl_seconds`` adds the queued cutoff on top, so raising one knob
can never make a stream expire under a run that is still legitimately alive.
"""

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


# How long a run may sit ``queued`` (waiting for a free worker slot) before the
# web sweeper gives up on it and tells the client. Not tied to the heartbeat: a
# queued run has no worker yet, so nothing beats for it.
DEFAULT_QUEUED_TIMEOUT_MINUTES = 10
_MIN_QUEUED_TIMEOUT_MINUTES = 1
_QUEUED_TIMEOUT_ENV = "POCKETPAW_CLOUD_RUN_QUEUED_TIMEOUT_MINUTES"


def queued_timeout_minutes() -> int:
    """Resolve the queued-run cutoff from ``POCKETPAW_CLOUD_RUN_QUEUED_TIMEOUT_MINUTES``.

    Defaults to 10. Unparseable falls back to the default; values under one
    minute are clamped, since a tiny cutoff would interrupt every run that waits
    even briefly behind a busy worker.
    """
    raw = os.environ.get(_QUEUED_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_QUEUED_TIMEOUT_MINUTES
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int; using default %dm",
            _QUEUED_TIMEOUT_ENV,
            raw,
            DEFAULT_QUEUED_TIMEOUT_MINUTES,
        )
        return DEFAULT_QUEUED_TIMEOUT_MINUTES
    return max(val, _MIN_QUEUED_TIMEOUT_MINUTES)


def queued_stream_ttl_seconds() -> int:
    """TTL for a stream created by the ``queued`` frame at enqueue time.

    Nothing else bounds the key until a terminal write refreshes it, so it must
    outlive the longest legitimate path: waiting the full queued cutoff, then
    running the full job timeout, then the reader's grace.
    """
    return queued_timeout_minutes() * 60 + stream_max_lifetime_seconds()


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
    tools_enabled: bool | None = None
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


class StrandedReply(BaseModel):
    """Assistant text that a terminal run stored and no ``Message`` row carries.

    The run document is the only copy, which is what makes this a REACHABILITY
    projection rather than a recovery one — nothing has to be restored, only
    read. ``status`` rides along because the history reader annotates the replay
    with it: a reply the user deliberately stopped and one the provider killed
    are both unfinished, and the model is told which.

    ``text`` is the VERBATIM stored partial. Nothing is prepended or appended
    here: the "this was cut off" marker is applied at read time by the history
    shaping, so the wording can change without a migration and the UI can still
    render the stored text truthfully.
    """

    model_config = ConfigDict(frozen=True)

    status: str
    text: str
    created_at: datetime
