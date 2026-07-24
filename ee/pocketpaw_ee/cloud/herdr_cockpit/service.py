# service.py — fail-open telemetry logic over the HerdrRuntime adapter.
#
# Created: 2026-07-24 (feat/herdr-cockpit-sse, HR-10a) — builds a cockpit
# snapshot (one "dot" per herdr pane) and reads an on-demand pane preview. All
# herdr access goes through the injected ``HerdrRuntime`` and every call is
# wrapped so ``HerdrUnavailable`` (flag off, binary missing, server down) never
# propagates: the snapshot degrades to ``herdr_available=False`` + empty panes,
# the preview degrades to empty text. This is the single place the fail-open
# contract is enforced, so the router stays a thin HTTP shell.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw.agents.errors import HerdrUnavailable
from pocketpaw.agents.herdr_runtime import HerdrRuntime
from pocketpaw.mission_control.models import AgentStatus
from pocketpaw_ee.cloud.herdr_cockpit.dto import (
    CockpitPaneOut,
    CockpitSnapshot,
    PanePreviewOut,
)

# Upper bound on preview scrollback so a caller can't ask herdr for an unbounded
# read. Clamped (not rejected) — a too-large request is silently capped here.
PREVIEW_MAX_LINES = 500
PREVIEW_DEFAULT_LINES = 25


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (snapshot timestamp)."""
    return datetime.now(UTC).isoformat()


def clamp_lines(lines: int | None) -> int:
    """Clamp a requested preview line count into ``[1, PREVIEW_MAX_LINES]``.

    ``None`` (or anything below 1) falls back to a floor of 1; values above the
    cap are silently reduced to it. This is a clamp, not a validation gate — the
    caller never gets a 4xx for an out-of-range ``lines``.
    """
    if lines is None:
        return PREVIEW_DEFAULT_LINES
    return max(1, min(int(lines), PREVIEW_MAX_LINES))


async def build_snapshot(runtime: HerdrRuntime) -> CockpitSnapshot:
    """One telemetry tick: every herdr pane with its live ``AgentStatus``.

    Fail-open contract:
    - herdr disabled / binary absent (cheap ``available`` guard, no subprocess)
      OR ``list_panes()`` raising ``HerdrUnavailable`` (server down mid-tick) →
      ``herdr_available=False`` with an empty pane list.
    - a single pane's ``status()`` failing (e.g. the pane closed between the list
      and the status call) → that pane reports ``offline`` rather than sinking
      the whole frame.

    Never raises; the SSE loop can call this forever without a try/except.
    """
    ts = _now_iso()

    # Cheap guard first — skip the subprocess entirely when herdr is off/absent.
    if not runtime.available:
        return CockpitSnapshot(ts=ts, herdr_available=False, panes=[])

    try:
        panes = await runtime.list_panes()
    except HerdrUnavailable:
        # Flag on but herdr unreachable this tick (server down, socket error).
        return CockpitSnapshot(ts=ts, herdr_available=False, panes=[])

    out: list[CockpitPaneOut] = []
    for pane in panes:
        try:
            status = await runtime.status(pane)
            status_value = status.value
        except HerdrUnavailable:
            # Pane vanished (or is not an agent pane) between list and status —
            # fail that dot to offline, keep the rest of the frame intact.
            status_value = AgentStatus.OFFLINE.value
        out.append(
            CockpitPaneOut(
                pane_id=pane.pane_id,
                workspace_id=pane.workspace_id,
                agent=pane.agent,
                status=status_value,
                tab_id=pane.tab_id,
                terminal_id=pane.terminal_id,
            )
        )

    return CockpitSnapshot(ts=ts, herdr_available=True, panes=out)


async def read_preview(
    runtime: HerdrRuntime,
    pane_id: str,
    lines: int | None = None,
) -> PanePreviewOut:
    """On-demand scrollback preview for one pane, fail-open to empty text.

    ``lines`` is clamped to ``[1, PREVIEW_MAX_LINES]``. When herdr is
    unavailable or the pane cannot be read the preview comes back with
    ``text=""`` instead of raising — the caller sees an empty pane, never a 500.
    """
    clamped = clamp_lines(lines)
    try:
        text = await runtime.read(pane_id, lines=clamped)
    except HerdrUnavailable:
        text = ""
    return PanePreviewOut(pane_id=pane_id, text=text)
