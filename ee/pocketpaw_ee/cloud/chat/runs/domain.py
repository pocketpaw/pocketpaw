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
rather than importing the document class."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
