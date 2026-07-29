# dto.py — wire schemas for the herdr cockpit telemetry surface.
#
# Created: 2026-07-24 (feat/herdr-cockpit-sse, HR-10a) — plain snake_case
# Pydantic response models the cockpit UI reads. There is no request body: the
# stream takes no input and the preview takes only path/query params. Tenancy is
# never a field here — the ADMIN gate + auth context govern access (see router).
#
# ``CockpitSnapshot`` is the JSON ``data`` of each ``cockpit.snapshot`` SSE frame.
# ``PanePreviewOut`` is the JSON body of the on-demand preview endpoint.

from __future__ import annotations

from pydantic import BaseModel


class CockpitPaneOut(BaseModel):
    """One herdr pane's live "dot" in a snapshot frame.

    ``status`` is a Mission Control ``AgentStatus`` *value* string
    (``idle`` | ``active`` | ``blocked`` | ``offline``). All ids are opaque
    strings minted by herdr; the only one guaranteed present is ``pane_id``.
    """

    pane_id: str
    workspace_id: str | None = None
    agent: str | None = None
    status: str
    tab_id: str | None = None
    terminal_id: str | None = None


class CockpitSnapshot(BaseModel):
    """The ``data`` payload of one ``cockpit.snapshot`` SSE frame.

    ``herdr_available`` is False (and ``panes`` empty) whenever herdr is
    disabled, absent, or unreachable this tick — the stream never errors, it
    degrades. ``ts`` is an ISO-8601 UTC timestamp for the snapshot.
    """

    ts: str
    herdr_available: bool
    panes: list[CockpitPaneOut]


class PanePreviewOut(BaseModel):
    """On-demand pane scrollback preview.

    ``text`` is empty when herdr is unavailable or the pane cannot be read —
    the endpoint fails open rather than 500-ing.
    """

    pane_id: str
    text: str
