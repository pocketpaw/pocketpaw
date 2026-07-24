# tests/cloud/test_herdr_cockpit.py — herdr cockpit telemetry backend (HR-10a).
#
# Created: 2026-07-24 (feat/herdr-cockpit-sse) — pins the read-only cockpit
# surface end-to-end where cheap, injecting a FAKE HerdrRuntime (canned panes,
# no real herdr server). Coverage:
#   * service.build_snapshot — frame shape + AgentStatus value strings; the
#     three fail-open branches (flag off, list_panes down, single-pane status
#     failure isolated); read_preview text + line clamp + fail-open.
#   * router — GET /cockpit/stream emits a named ``cockpit.snapshot`` frame;
#     GET /cockpit/pane/{id}/preview returns {pane_id,text}; both fail open;
#     both are ADMIN-gated (member -> 403, admin -> 200) via the REAL RBAC guard.
#
# Spy-don't-over-mock: the fake is only the herdr I/O boundary; the real PaneRef
# / AgentStatus value objects and the real require_action_any_workspace guard run.

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pocketpaw_ee.cloud.herdr_cockpit import service  # noqa: E402

from pocketpaw.agents.errors import HerdrUnavailable  # noqa: E402
from pocketpaw.agents.herdr_runtime import PaneRef  # noqa: E402
from pocketpaw.mission_control.models import AgentStatus  # noqa: E402

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake HerdrRuntime — the herdr I/O boundary only. Records read() calls so tests
# can assert the line clamp; raises HerdrUnavailable on demand for fail-open.
# ---------------------------------------------------------------------------


class FakeHerdrRuntime:
    def __init__(
        self,
        *,
        available: bool = True,
        panes: list[PaneRef] | None = None,
        statuses: dict[str, AgentStatus] | None = None,
        text: str = "scrollback text",
        raise_on: set[str] | None = None,
    ) -> None:
        self._available = available
        self._panes = panes or []
        self._statuses = statuses or {}
        self._text = text
        # membership: a bare method name ("list_panes"/"status"/"read") fails that
        # call globally; a pane_id fails only that pane's status() call.
        self._raise_on = raise_on or set()
        self.read_calls: list[tuple[str, int | None]] = []

    @property
    def available(self) -> bool:
        return self._available

    async def list_panes(self) -> list[PaneRef]:
        if "list_panes" in self._raise_on:
            raise HerdrUnavailable("test: herdr unreachable")
        return list(self._panes)

    async def status(self, ref: PaneRef | str) -> AgentStatus:
        pane_id = ref.pane_id if isinstance(ref, PaneRef) else str(ref)
        if "status" in self._raise_on or pane_id in self._raise_on:
            raise HerdrUnavailable("test: status failed")
        return self._statuses.get(pane_id, AgentStatus.IDLE)

    async def read(
        self, ref: PaneRef | str, *, source: str = "visible", lines: int | None = None
    ) -> str:
        pane_id = ref.pane_id if isinstance(ref, PaneRef) else str(ref)
        self.read_calls.append((pane_id, lines))
        if "read" in self._raise_on:
            raise HerdrUnavailable("test: read failed")
        return self._text


def _pane(pane_id: str, **kw) -> PaneRef:
    return PaneRef(
        pane_id=pane_id,
        workspace_id=kw.get("workspace_id", "hw-1"),
        agent=kw.get("agent", "claude"),
        tab_id=kw.get("tab_id", "tab-1"),
        terminal_id=kw.get("terminal_id", "term-1"),
    )


# ===========================================================================
# Service layer — build_snapshot
# ===========================================================================


async def test_build_snapshot_shape_and_status_values():
    """A healthy tick: herdr_available True, one dot per pane, each carrying the
    full wire shape with the AgentStatus VALUE string."""
    runtime = FakeHerdrRuntime(
        panes=[_pane("p1", agent="claude"), _pane("p2", agent="codex")],
        statuses={"p1": AgentStatus.ACTIVE, "p2": AgentStatus.BLOCKED},
    )

    snap = await service.build_snapshot(runtime)

    assert snap.herdr_available is True
    assert snap.ts  # ISO-8601 timestamp present
    assert len(snap.panes) == 2
    by_id = {p.pane_id: p for p in snap.panes}
    assert by_id["p1"].status == "active"
    assert by_id["p2"].status == "blocked"
    # Full wire shape on every dot.
    p1 = by_id["p1"]
    assert (p1.workspace_id, p1.agent, p1.tab_id, p1.terminal_id) == (
        "hw-1",
        "claude",
        "tab-1",
        "term-1",
    )
    # status is always a valid AgentStatus value string.
    assert {p.status for p in snap.panes} <= {s.value for s in AgentStatus}


async def test_build_snapshot_flag_off_is_fail_open():
    """herdr disabled/absent (available False) -> no subprocess, empty fail-open
    frame, never an error."""
    runtime = FakeHerdrRuntime(available=False, panes=[_pane("p1")])

    snap = await service.build_snapshot(runtime)

    assert snap.herdr_available is False
    assert snap.panes == []


async def test_build_snapshot_list_panes_unavailable_is_fail_open():
    """Flag on but herdr unreachable this tick -> herdr_available False, empty."""
    runtime = FakeHerdrRuntime(panes=[_pane("p1")], raise_on={"list_panes"})

    snap = await service.build_snapshot(runtime)

    assert snap.herdr_available is False
    assert snap.panes == []


async def test_build_snapshot_single_pane_status_failure_is_isolated():
    """One pane's status() failing (pane closed mid-tick) -> that dot goes
    offline; the rest of the frame survives and herdr stays available."""
    runtime = FakeHerdrRuntime(
        panes=[_pane("p1"), _pane("p2")],
        statuses={"p2": AgentStatus.ACTIVE},
        raise_on={"p1"},  # only p1's status() raises
    )

    snap = await service.build_snapshot(runtime)

    assert snap.herdr_available is True
    by_id = {p.pane_id: p for p in snap.panes}
    assert by_id["p1"].status == "offline"  # failed pane fails safe
    assert by_id["p2"].status == "active"  # healthy pane intact


# ===========================================================================
# Service layer — read_preview
# ===========================================================================


async def test_read_preview_returns_text():
    runtime = FakeHerdrRuntime(text="hello from pane")
    out = await service.read_preview(runtime, "p1", 25)
    assert out.pane_id == "p1"
    assert out.text == "hello from pane"


async def test_read_preview_clamps_lines():
    """A too-large request is capped to PREVIEW_MAX_LINES; sub-1 floors to 1."""
    runtime = FakeHerdrRuntime()

    await service.read_preview(runtime, "p1", 10_000)
    await service.read_preview(runtime, "p1", 0)
    await service.read_preview(runtime, "p1", None)

    lines_seen = [lines for _pid, lines in runtime.read_calls]
    assert lines_seen[0] == service.PREVIEW_MAX_LINES  # 10_000 -> 500
    assert lines_seen[1] == 1  # 0 -> floor 1
    assert lines_seen[2] == service.PREVIEW_DEFAULT_LINES  # None -> default


async def test_read_preview_fail_open_to_empty_text():
    runtime = FakeHerdrRuntime(raise_on={"read"})
    out = await service.read_preview(runtime, "p1", 25)
    assert out.pane_id == "p1"
    assert out.text == ""


# ===========================================================================
# Router / HTTP — real RBAC guard, fake runtime injected
# ===========================================================================


def _build_app(*, role: str = "admin", runtime: FakeHerdrRuntime | None = None) -> FastAPI:
    """App over the cockpit router with the REAL RBAC guard; the caller's
    workspace role drives require_action_any_workspace. License bypassed; the
    HerdrRuntime provider is overridden with the injected fake."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.herdr_cockpit.router import get_herdr_runtime, router
    from pocketpaw_ee.cloud.license import require_license

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None

    user = SimpleNamespace(
        id="u1",
        active_workspace="w1",
        workspaces=[SimpleNamespace(workspace="w1", role=role)],
    )

    async def _fake_user():
        return user

    app.dependency_overrides[current_active_user] = _fake_user
    app.dependency_overrides[get_herdr_runtime] = lambda: runtime or FakeHerdrRuntime()
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _parse_one_frame(raw: bytes) -> tuple[str, dict]:
    """Parse one encoded SSE frame's event name + JSON data payload."""
    event = ""
    data = ""
    for line in raw.decode().splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = line[len("data:") :].strip()
    return event, json.loads(data)


async def _first_stream_frame(runtime: FakeHerdrRuntime) -> tuple[str, dict]:
    """Drive the stream handler directly and pull exactly the first frame.

    httpx's ASGITransport buffers the whole response body, so it cannot read an
    endless SSE stream incrementally (it hangs). Pulling the handler's
    StreamingResponse body_iterator by hand is the reliable way to assert the
    first frame; aclose() then stops the generator so its poll sleep never
    leaks. The ADMIN gate is exercised over real HTTP in
    test_endpoints_require_admin, so bypassing DI here is fine.
    """
    from pocketpaw_ee.cloud.herdr_cockpit.router import stream_cockpit

    resp = await stream_cockpit(runtime=runtime)
    assert resp.media_type == "text/event-stream"
    agen = resp.body_iterator
    try:
        raw = await agen.__anext__()
    finally:
        await agen.aclose()
    return _parse_one_frame(raw)


async def test_stream_emits_named_cockpit_snapshot_frame():
    runtime = FakeHerdrRuntime(
        panes=[_pane("p1", agent="claude")],
        statuses={"p1": AgentStatus.ACTIVE},
    )
    event, data = await _first_stream_frame(runtime)

    assert event == "cockpit.snapshot"
    assert data["herdr_available"] is True
    assert data["ts"]
    assert data["panes"][0]["pane_id"] == "p1"
    assert data["panes"][0]["status"] == "active"


async def test_stream_fails_open_when_herdr_off():
    """Flag off -> the stream still emits frames, just herdr_available:false with
    no panes (never a 500, never a crash)."""
    event, data = await _first_stream_frame(FakeHerdrRuntime(available=False))

    assert event == "cockpit.snapshot"
    assert data["herdr_available"] is False
    assert data["panes"] == []


async def test_preview_endpoint_returns_text():
    runtime = FakeHerdrRuntime(text="pane scrollback")
    app = _build_app(role="admin", runtime=runtime)
    async with _client(app) as client:
        resp = await client.get("/api/v1/cockpit/pane/p1/preview", params={"lines": 25})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"pane_id": "p1", "text": "pane scrollback"}


async def test_preview_endpoint_fails_open_to_empty_text():
    runtime = FakeHerdrRuntime(raise_on={"read"})
    app = _build_app(role="admin", runtime=runtime)
    async with _client(app) as client:
        resp = await client.get("/api/v1/cockpit/pane/p1/preview")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"pane_id": "p1", "text": ""}


async def test_endpoints_require_admin():
    """A MEMBER is denied on BOTH routes (403); an ADMIN passes. Herdr panes are
    not workspace-scoped, so the surface is admin-only in v1."""
    member_app = _build_app(role="member", runtime=FakeHerdrRuntime())
    async with _client(member_app) as client:
        # A denied stream short-circuits at the guard (finite 403 body, no
        # streaming), so a plain GET is safe and does not hang.
        stream_resp = await client.get("/api/v1/cockpit/stream")
        preview_resp = await client.get("/api/v1/cockpit/pane/p1/preview")

    assert stream_resp.status_code == 403
    assert preview_resp.status_code == 403

    admin_app = _build_app(role="admin", runtime=FakeHerdrRuntime())
    async with _client(admin_app) as client:
        admin_preview = await client.get("/api/v1/cockpit/pane/p1/preview")
    assert admin_preview.status_code == 200
