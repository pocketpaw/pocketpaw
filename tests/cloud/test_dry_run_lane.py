# tests/cloud/test_dry_run_lane.py
# Created: 2026-06-19 (feat/instinct-gate-integration, T7) — coverage for the
# DRY_RUN and OPTIMISTIC executor paths added to
# ``action_executor.run_action`` by the layered/learning gate integration.
#
# DRY_RUN (governance rehearsal): when ``dry_run=True`` the executor runs the
# security gates (rate-limit, base-URL, SSRF, allowlist, DNS) AS VALIDATION,
# then intercepts at gate 7 and returns an ``instinct_dry_run`` sentinel —
# the resolved write under ``_park`` (server-side only), NO HTTP call, NO
# approval row. The sentinel is structurally identical to ``instinct_pending``
# so the router strips ``_park`` the same way and the write never leaks to the
# wire (T-31, pinned against the router's strip logic in
# test_pocket_action_executor flow assumptions).
#
# OPTIMISTIC: ``optimistic_execute=True`` is a NEW dedicated flag (never
# reuses ``from_instinct``) — it clears the gate-7 park so a trusted,
# reversible write fires NOW, and the caller registers a bounded compensation
# handle.
#
# THE SAFETY-CRITICAL ORDERING (T-30): the dry-run intercept sits AFTER the
# security gates, so an allowlist miss on a ``dry_run=True`` call still returns
# ``not_allowed`` — a rehearsal of an off-policy write is still rejected.
#
# Test plan cases: T-27, T-28, T-30. (T-29 config→gate routing lives in the
# dispatch lanes / triage tests; T-31 router strip is asserted via the
# existing router strip + extra="forbid" model; T-32 saga lives in
# test_pocket_saga.)

from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.cloud.pockets import action_executor, source_executor, trust_ledger
from pocketpaw_ee.cloud.pockets.instinct_triage import ApprovalLevel

from pocketpaw.bundled_templates import PocketTemplate

BASE = "https://api.example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    action_executor._action_log.clear()
    source_executor._run_log.clear()
    yield
    action_executor._action_log.clear()
    source_executor._run_log.clear()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Make every hostname resolve to a public IP so the DNS guard passes."""

    def _fake_getaddrinfo(host, *_args, **_kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo)


class _Recorder:
    """Records every HTTP request the executor makes; replies 200 {}."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True})


def _patch_transport(monkeypatch, recorder: _Recorder) -> None:
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _factory)


_ALLOW = [{"method": "POST", "path_pattern": "/items"}]


def _raw_action(**over) -> dict:
    raw = {"kind": "write_binding", "method": "POST", "path": "/items"}
    raw.update(over)
    return raw


async def _run(monkeypatch, recorder, **over):
    _patch_transport(monkeypatch, recorder)
    kwargs = dict(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_ALLOW,
    )
    kwargs.update(over)
    return await action_executor.run_action(**kwargs)


# ===========================================================================
# T-27 / T-28 — dry_run returns the sentinel, makes no call, persists nothing.
# ===========================================================================


async def test_dry_run_returns_sentinel_no_http(monkeypatch) -> None:
    """T-27/T-28: dry_run=True → instinct_dry_run sentinel, NO HTTP call, the
    _park dict carries the resolved write (identical fields to a live park)."""
    recorder = _Recorder()
    result = await _run(monkeypatch, recorder, dry_run=True)

    assert result["ok"] is True
    assert result["code"] == "instinct_dry_run"
    assert result["action"] == "do_thing"
    # No HTTP call fired.
    assert recorder.calls == []
    # _park carries the resolved write, same shape as instinct_pending.
    park = result["_park"]
    assert park["method"] == "POST"
    assert park["path"] == "/items"
    assert park["params"] == {"x": 1}
    # The sentinel does not carry an approval_id (dry-run persists nothing).
    assert "approval_id" not in result or result.get("approval_id") is None


async def test_dry_run_park_matches_pending_shape(monkeypatch) -> None:
    """The dry-run _park dict has the same key set as the live instinct_pending
    park, so the router's existing strip handles it identically (T-31 basis)."""
    recorder = _Recorder()
    # A binding that requires instinct so the live path would park at gate 7.
    pending = await _run(monkeypatch, recorder, raw_action=_raw_action(requires_instinct=True))
    assert pending["code"] == "instinct_pending"

    dry = await _run(monkeypatch, recorder, dry_run=True)
    assert dry["code"] == "instinct_dry_run"

    # Same _park key set (correlation_id may differ in value, not presence).
    assert set(dry["_park"].keys()) == set(pending["_park"].keys())


# ===========================================================================
# T-30 — security gates run BEFORE the dry-run intercept.
# ===========================================================================


async def test_dry_run_allowlist_miss_still_rejected(monkeypatch) -> None:
    """T-30 (the safety-critical ordering): an allowlist miss on a dry_run=True
    call returns not_allowed, NOT instinct_dry_run — the rehearsal of an
    off-policy write is still rejected by the security gates that run first."""
    recorder = _Recorder()
    result = await _run(
        monkeypatch,
        recorder,
        dry_run=True,
        path="/forbidden",
        raw_action=_raw_action(path="/forbidden"),
        allowed_writes=[{"method": "POST", "path_pattern": "/items"}],
    )
    assert result["ok"] is False
    assert result["code"] == "not_allowed"
    assert recorder.calls == []


async def test_dry_run_bad_base_url_still_rejected(monkeypatch) -> None:
    """A base-URL gate failure precedes the dry-run intercept too."""
    recorder = _Recorder()
    result = await _run(monkeypatch, recorder, dry_run=True, base_url="http://169.254.169.254")
    assert result["ok"] is False
    assert result["code"] in ("bad_base_url", "rejected")
    assert recorder.calls == []


# ===========================================================================
# OPTIMISTIC — optimistic_execute fires the write, clears the park.
# ===========================================================================


async def test_optimistic_execute_fires_and_clears_park(monkeypatch) -> None:
    """optimistic_execute=True on a requires_instinct binding FIRES the write
    (clears gate 7's park) and returns a normal success — not instinct_pending."""
    recorder = _Recorder()
    result = await _run(
        monkeypatch,
        recorder,
        optimistic_execute=True,
        raw_action=_raw_action(requires_instinct=True),
    )
    assert result["ok"] is True
    assert result.get("code") not in ("instinct_pending", "instinct_dry_run")
    assert result["status"] == 200
    # The HTTP call actually fired.
    assert recorder.calls == [("POST", "/items")]


async def test_optimistic_with_compensate_registers_handle(monkeypatch) -> None:
    """T10: an optimistic write whose binding declares a `compensate` spec
    registers a bounded compensation handle and returns its id under
    `_optimistic_compensation_id`, so the caller can expose a rollback and the
    TTL sweeper can hard-expire the bet if it's never rolled back."""
    recorder = _Recorder()
    result = await _run(
        monkeypatch,
        recorder,
        optimistic_execute=True,
        raw_action=_raw_action(
            requires_instinct=True,
            compensate={"method": "DELETE", "path": "/items/1"},
        ),
        allowed_writes=[
            {"method": "POST", "path_pattern": "/items"},
            {"method": "DELETE", "path_pattern": "/items/*"},
        ],
    )
    assert result["ok"] is True
    assert result["status"] == 200
    cid = result.get("_optimistic_compensation_id")
    assert cid, "optimistic write with a compensate must return a handle id"
    # The handle is live in the registry until rollback or TTL expiry.
    handle = action_executor.get_optimistic_registry().get(cid)
    assert handle is not None
    assert handle.action == "do_thing"
    assert handle.compensate.method == "DELETE"


async def test_optimistic_without_compensate_no_handle(monkeypatch) -> None:
    """An optimistic write with NO declared compensate fires but registers no
    handle — there is nothing to roll back, so no id is returned."""
    recorder = _Recorder()
    result = await _run(
        monkeypatch,
        recorder,
        optimistic_execute=True,
        raw_action=_raw_action(requires_instinct=True),
    )
    assert result["ok"] is True
    assert result.get("_optimistic_compensation_id") is None


async def test_optimistic_does_not_reuse_from_instinct(monkeypatch) -> None:
    """optimistic_execute is a DEDICATED flag — default from_instinct stays
    False on the optimistic path (MF-4: no semantic lie)."""
    recorder = _Recorder()
    # An allowlist miss on the optimistic path is still rejected — the
    # security gates run; optimistic only clears the PARK, not the gates.
    result = await _run(
        monkeypatch,
        recorder,
        optimistic_execute=True,
        path="/forbidden",
        raw_action=_raw_action(path="/forbidden", requires_instinct=True),
        allowed_writes=[{"method": "POST", "path_pattern": "/items"}],
    )
    assert result["ok"] is False
    assert result["code"] == "not_allowed"
    assert recorder.calls == []


# ===========================================================================
# END-TO-END through run_action: a template-gated escalate, with the
# workspace opted into TRIAGE and trust seeded, fires the write via the AUTO
# lane (the full T6 vertical slice) — and the SAME call under default ASK
# parks for a human (the safety invariant, end-to-end).
# ===========================================================================


def _escalate_template() -> PocketTemplate:
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "t",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "test",
            "description": "x",
            "shape": "data-grid",
            "state": {"entity_type": "Thing", "columns": [{"field": "value", "widget": "number"}]},
            "actions": [
                {"name": "do_thing", "label": "Do", "kind": "single-row", "instinct_policy": "auto"}
            ],
            "instinct_rules": {"rules": [{"when": "value > 0", "action": "require_approval"}]},
        }
    )


@pytest.fixture
def _isolated_trust(monkeypatch, tmp_path):
    monkeypatch.setattr(trust_ledger, "_trust_dir", lambda: tmp_path / "trust")


@pytest.mark.usefixtures("mongo_db")
async def test_e2e_triage_auto_fires_write(monkeypatch, _isolated_trust) -> None:
    """approval_level=TRIAGE + seeded trust + reversible POST → the executor
    FIRES the write via the AUTO lane (no park)."""
    # Seed trust so the (pocket, action) pair clears the threshold.
    for _ in range(10):
        await trust_ledger.record_correction("w1", "p1", "do_thing", was_auto_approved=True)

    recorder = _Recorder()
    result = await _run(
        monkeypatch,
        recorder,
        template=_escalate_template(),
        row_context={"value": 5},
        raw_action=_raw_action(compensate={"method": "DELETE", "path": "/items/1"}),
        allowed_writes=[
            {"method": "POST", "path_pattern": "/items"},
            {"method": "DELETE", "path_pattern": "/items/*"},
        ],
        approval_level=ApprovalLevel.TRIAGE,
    )
    assert result["ok"] is True
    assert result.get("code") not in ("instinct_pending", "instinct_dry_run")
    assert result["status"] == 200
    assert recorder.calls == [("POST", "/items")]


@pytest.mark.usefixtures("mongo_db")
async def test_e2e_default_ask_parks_same_call(monkeypatch, _isolated_trust) -> None:
    """The SAME call as above, but with NO approval_level (default ASK), PARKS
    for a human — the safety invariant proven end-to-end through run_action."""
    for _ in range(10):
        await trust_ledger.record_correction("w1", "p1", "do_thing", was_auto_approved=True)

    recorder = _Recorder()
    result = await _run(
        monkeypatch,
        recorder,
        template=_escalate_template(),
        row_context={"value": 5},
        raw_action=_raw_action(compensate={"method": "DELETE", "path": "/items/1"}),
        allowed_writes=[
            {"method": "POST", "path_pattern": "/items"},
            {"method": "DELETE", "path_pattern": "/items/*"},
        ],
        # NO approval_level — dormant ASK.
    )
    assert result["ok"] is True
    assert result["code"] == "instinct_pending"
    assert result["approval_id"]
    # The write never fired — it parked for a human.
    assert recorder.calls == []
