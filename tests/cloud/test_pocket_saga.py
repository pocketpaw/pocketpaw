# tests/cloud/test_pocket_saga.py — RFC 05 Saga Compensate (first pass).
# Created: 2026-06-01 — coverage for the multi-step write-SEQUENCE rollback
# runtime (`ee/pocketpaw_ee/cloud/pockets/saga.py`). No real network: the
# outbound HTTP that `action_executor.run_action` makes is faked via
# httpx.MockTransport, and socket.getaddrinfo is monkeypatched so fake
# hostnames resolve to a public IP (mirrors test_pocket_action_executor.py).
#
# CRITICAL TEST-DESIGN NOTE (cross-slice integration, soul lesson
# 2026-05-26): the saga ORCHESTRATES the real `run_action`. These tests
# drive the PRODUCTION executor end-to-end — they do NOT stub `run_action`
# at the saga↔executor seam. A 3-step saga where step 3 fails exercises
# the real allowlist / SSRF / HTTP path on every forward step AND every
# compensation, so a doubling / missing-emit bug between the saga and the
# executor would surface here, not hide behind a mock.
#
# What this pins:
#   - CompensateSpec parses; ActionBinding.compensate parses + defaults None.
#   - Happy path: every step fires → ok:true, no compensations, forward
#     outcomes emitted.
#   - The headline: a 3-step write where step 3 fails fires step 1+2
#     compensations in REVERSE order, with compensating outcomes emitted.
#   - A completed step with no `compensate` is a `no_compensator` gap.
#   - A compensation that itself fails does NOT abort the rollback; it is
#     surfaced as `failed` and `rolled_back` is False.
#   - A parked (instinct_pending) forward step is treated as a sequence
#     failure and the prior committed steps roll back.

from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.cloud.pockets import action_executor, source_executor
from pocketpaw_ee.cloud.pockets.action_executor import ActionBinding, CompensateSpec
from pocketpaw_ee.cloud.pockets.saga import SagaStep, run_action_sequence

BASE = "https://api.example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear the executor rate-limit logs between tests."""
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


@pytest.fixture(autouse=True)
def _capture_outcomes(monkeypatch):
    """Capture every ``emit_pocket_outcome`` call the saga makes.

    The saga emits forward + compensating outcomes through
    ``outcomes.service.emit_pocket_outcome``. We patch it on the saga's
    lazy-import target so the assertions can inspect ``compensated`` per
    emit without a running bus / ledger. Returns the capture list.
    """
    captured: list[dict] = []

    async def _fake_emit(**kwargs):
        captured.append(kwargs)

    import pocketpaw_ee.cloud.outcomes.service as outcomes_service

    monkeypatch.setattr(outcomes_service, "emit_pocket_outcome", _fake_emit)
    return captured


class _Recorder:
    """Records each request the executor makes and replies per a route map.

    ``routes`` maps ``(METHOD, path)`` → either an int status or a
    ``(status, json_body)`` tuple. An unmapped route replies 200 ``{}``.
    ``calls`` is the ordered list of ``(method, path)`` actually hit — the
    rollback-order assertions read it.
    """

    def __init__(self, routes: dict[tuple[str, str], object]):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append(key)
        spec = self.routes.get(key, 200)
        if isinstance(spec, tuple):
            status, body = spec
            return httpx.Response(status, json=body)
        return httpx.Response(int(spec), json={})


def _patch_transport(monkeypatch, recorder: _Recorder) -> None:
    """Route the executor's AsyncClient through the recorder's MockTransport."""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _factory)


def _step(
    action: str,
    method: str,
    path: str,
    *,
    compensate: dict | None = None,
    outcome: str | None = None,
    idempotency_key: str | None = None,
) -> SagaStep:
    """Build a SagaStep whose raw_action carries an optional compensate spec.

    Steps are built `instinct_exempt` because these tests exercise the saga
    MECHANICS (ordering, rollback, idempotency) — W2a's deny-by-default
    would otherwise park every step at the gate. The gated-step test flips
    the exemption off explicitly to cover the park-then-rollback path.
    """
    raw: dict = {
        "kind": "write_binding",
        "method": method,
        "path": path,
        "params": {},
        "instinct_exempt": True,
    }
    if compensate is not None:
        raw["compensate"] = compensate
    if outcome is not None:
        raw["outcome"] = outcome
    return SagaStep(
        action=action,
        raw_action=raw,
        path=path,
        params={},
        idempotency_key=idempotency_key,
    )


# A three-rule allowlist covering the forward writes AND their inverses.
_ALLOW = [
    {"method": "POST", "path_pattern": "/reserve"},
    {"method": "DELETE", "path_pattern": "/reserve/*"},
    {"method": "POST", "path_pattern": "/charge"},
    {"method": "POST", "path_pattern": "/refund"},
    {"method": "POST", "path_pattern": "/confirm"},
]


async def _run(steps, monkeypatch, recorder, **overrides):
    """Drive `run_action_sequence` against the recorder's backend."""
    _patch_transport(monkeypatch, recorder)
    kwargs = dict(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        steps=steps,
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_ALLOW,
    )
    kwargs.update(overrides)
    return await run_action_sequence(**kwargs)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_compensate_spec_parses():
    spec = CompensateSpec.model_validate(
        {"method": "POST", "path": "/refund", "params": {"id": 7}, "outcome": "refunded"}
    )
    assert spec.method == "POST"
    assert spec.path == "/refund"
    assert spec.params == {"id": 7}
    assert spec.outcome == "refunded"


def test_compensate_spec_rejects_non_write_verb():
    with pytest.raises(Exception):
        CompensateSpec.model_validate({"method": "GET", "path": "/x"})


def test_action_binding_parses_compensate():
    binding = ActionBinding.model_validate(
        {
            "kind": "write_binding",
            "method": "POST",
            "path": "/charge",
            "compensate": {"method": "POST", "path": "/refund", "outcome": "refunded"},
        }
    )
    assert binding.compensate is not None
    assert binding.compensate.method == "POST"
    assert binding.compensate.path == "/refund"
    assert binding.compensate.outcome == "refunded"


def test_action_binding_compensate_defaults_none():
    binding = ActionBinding.model_validate(
        {"kind": "write_binding", "method": "POST", "path": "/charge"}
    )
    assert binding.compensate is None


# ---------------------------------------------------------------------------
# Happy path — every step fires, no rollback
# ---------------------------------------------------------------------------


async def test_all_steps_succeed_no_compensation(monkeypatch, _capture_outcomes):
    rec = _Recorder({})  # everything 200
    steps = [
        _step("reserve", "POST", "/reserve", compensate={"method": "DELETE", "path": "/reserve/1"}),
        _step("charge", "POST", "/charge", compensate={"method": "POST", "path": "/refund"}),
        _step("confirm", "POST", "/confirm"),
    ]
    result = await _run(steps, monkeypatch, rec)

    assert result.ok is True
    assert result.completed == ["reserve", "charge", "confirm"]
    assert result.compensations == []
    assert result.rolled_back is False  # nothing to roll back
    # Only the three forward writes fired — no compensations.
    assert rec.calls == [("POST", "/reserve"), ("POST", "/charge"), ("POST", "/confirm")]


async def test_forward_outcomes_emitted_on_success(monkeypatch, _capture_outcomes):
    rec = _Recorder({})
    steps = [
        _step("reserve", "POST", "/reserve", outcome="reserved"),
        _step("charge", "POST", "/charge", outcome="charged"),
    ]
    result = await _run(steps, monkeypatch, rec)

    assert result.ok is True
    emitted = [(c["outcome"], c["compensated"]) for c in _capture_outcomes]
    assert emitted == [("reserved", False), ("charged", False)]


# ---------------------------------------------------------------------------
# THE HEADLINE — step 3 fails → step 1+2 compensations fire in REVERSE
# ---------------------------------------------------------------------------


async def test_step3_failure_fires_step1_and_2_compensations_in_reverse(
    monkeypatch, _capture_outcomes
):
    """A 3-step write where step 3 fails (backend 500) rolls back steps 1+2
    by firing their compensations newest-first (step 2's, then step 1's),
    with compensating outcomes emitted."""
    rec = _Recorder(
        {
            # forward writes 1+2 succeed; step 3 fails with a backend 500.
            ("POST", "/confirm"): 500,
            # compensations succeed.
            ("POST", "/refund"): 200,
            ("DELETE", "/reserve/1"): 200,
        }
    )
    steps = [
        _step(
            "reserve",
            "POST",
            "/reserve",
            compensate={
                "method": "DELETE",
                "path": "/reserve/1",
                "outcome": "reservation_released",
            },
            outcome="reserved",
        ),
        _step(
            "charge",
            "POST",
            "/charge",
            compensate={"method": "POST", "path": "/refund", "outcome": "charge_refunded"},
            outcome="charged",
        ),
        _step("confirm", "POST", "/confirm"),
    ]
    result = await _run(steps, monkeypatch, rec)

    # Sequence failed at step 3.
    assert result.ok is False
    assert result.failed_index == 2
    assert result.failed_action == "confirm"
    assert result.failure["code"] == "http_error"
    assert result.completed == ["reserve", "charge"]

    # Compensations fired for the two completed steps, in REVERSE order:
    # charge first (newest), then reserve.
    assert [c.action for c in result.compensations] == ["charge", "reserve"]
    assert all(c.status == "compensated" for c in result.compensations)
    assert result.rolled_back is True
    assert result.compensation_failures == []

    # The ACTUAL backend call order proves reverse compensation: the two
    # forward writes, the failed confirm, then refund (charge's inverse),
    # then DELETE /reserve/1 (reserve's inverse) — newest-committed first.
    assert rec.calls == [
        ("POST", "/reserve"),
        ("POST", "/charge"),
        ("POST", "/confirm"),
        ("POST", "/refund"),
        ("DELETE", "/reserve/1"),
    ]

    # Outcomes: forward `reserved` + `charged` (compensated=False), then the
    # compensating `charge_refunded` + `reservation_released` (compensated=True),
    # in rollback order.
    emitted = [(c["outcome"], c["compensated"]) for c in _capture_outcomes]
    assert emitted == [
        ("reserved", False),
        ("charged", False),
        ("charge_refunded", True),
        ("reservation_released", True),
    ]


# ---------------------------------------------------------------------------
# Edge: a completed step with no compensate spec → no_compensator gap
# ---------------------------------------------------------------------------


async def test_step_without_compensator_records_gap(monkeypatch, _capture_outcomes):
    """When a completed step has no `compensate`, the rollback records a
    `no_compensator` gap (the backend is left partially inconsistent) and
    `rolled_back` is False."""
    rec = _Recorder({("POST", "/confirm"): 500, ("POST", "/refund"): 200})
    steps = [
        # step 1 has NO compensate spec.
        _step("reserve", "POST", "/reserve"),
        _step("charge", "POST", "/charge", compensate={"method": "POST", "path": "/refund"}),
        _step("confirm", "POST", "/confirm"),
    ]
    result = await _run(steps, monkeypatch, rec)

    assert result.ok is False
    assert result.completed == ["reserve", "charge"]
    # charge compensated; reserve has no compensator.
    by_action = {c.action: c.status for c in result.compensations}
    assert by_action == {"charge": "compensated", "reserve": "no_compensator"}
    assert result.rolled_back is False  # the gap means not fully rolled back
    failures = result.compensation_failures
    assert [c.action for c in failures] == ["reserve"]

    # Only charge's inverse (refund) actually hit the backend — there is no
    # inverse for reserve to fire.
    assert ("POST", "/refund") in rec.calls
    assert ("DELETE", "/reserve/1") not in rec.calls


# ---------------------------------------------------------------------------
# Edge: a compensation that itself fails → rollback continues, surfaced
# ---------------------------------------------------------------------------


async def test_failed_compensation_does_not_abort_rollback(monkeypatch, _capture_outcomes):
    """If step 2's compensation itself fails, the rollback STILL fires step
    1's compensation — a half-rolled-back saga is worse than a fully-
    attempted one. The failed leg is surfaced; `rolled_back` is False."""
    rec = _Recorder(
        {
            ("POST", "/confirm"): 500,  # step 3 fails
            ("POST", "/refund"): 500,  # charge's compensation ALSO fails
            ("DELETE", "/reserve/1"): 200,  # reserve's compensation succeeds
        }
    )
    steps = [
        _step(
            "reserve",
            "POST",
            "/reserve",
            compensate={"method": "DELETE", "path": "/reserve/1"},
        ),
        _step("charge", "POST", "/charge", compensate={"method": "POST", "path": "/refund"}),
        _step("confirm", "POST", "/confirm"),
    ]
    result = await _run(steps, monkeypatch, rec)

    assert result.ok is False
    by_action = {c.action: c.status for c in result.compensations}
    assert by_action == {"charge": "failed", "reserve": "compensated"}
    assert result.rolled_back is False
    assert [c.action for c in result.compensation_failures] == ["charge"]

    # Both compensations were ATTEMPTED despite charge's failing — reserve's
    # DELETE still fired after refund failed.
    assert ("POST", "/refund") in rec.calls
    assert ("DELETE", "/reserve/1") in rec.calls
    # Order: refund (attempted, failed) came before the reserve DELETE.
    assert rec.calls.index(("POST", "/refund")) < rec.calls.index(("DELETE", "/reserve/1"))


# ---------------------------------------------------------------------------
# Edge: a parked (instinct) forward step → treated as failure, prior rolls back
# ---------------------------------------------------------------------------


async def test_parked_forward_step_rolls_back_prior_steps(monkeypatch, _capture_outcomes):
    """A forward step that PARKS (`requires_instinct` → instinct_pending)
    never fires, so the sequence cannot continue — the prior committed step
    rolls back and the park is the recorded failure."""
    rec = _Recorder({("POST", "/refund"): 200})
    # step 2 requires instinct → parks before any call. Drop the test
    # default exemption so W2a's gate actually engages.
    gated = _step("charge", "POST", "/charge")
    gated.raw_action["requires_instinct"] = True
    gated.raw_action["instinct_exempt"] = False

    steps = [
        _step("reserve", "POST", "/reserve", compensate={"method": "POST", "path": "/refund"}),
        gated,
    ]
    result = await _run(steps, monkeypatch, rec)

    assert result.ok is False
    assert result.failed_action == "charge"
    assert result.failure["code"] == "instinct_pending"
    assert result.completed == ["reserve"]
    # reserve rolled back via its compensation.
    assert [c.action for c in result.compensations] == ["reserve"]
    assert result.compensations[0].status == "compensated"
    assert result.rolled_back is True

    # /charge never hit the backend (it parked); reserve + its refund did.
    assert ("POST", "/charge") not in rec.calls
    assert ("POST", "/reserve") in rec.calls
    assert ("POST", "/refund") in rec.calls


# ---------------------------------------------------------------------------
# Edge: first step fails → nothing committed, nothing to compensate
# ---------------------------------------------------------------------------


async def test_first_step_failure_compensates_nothing(monkeypatch, _capture_outcomes):
    rec = _Recorder({("POST", "/reserve"): 500})
    steps = [
        _step("reserve", "POST", "/reserve", compensate={"method": "POST", "path": "/refund"}),
        _step("charge", "POST", "/charge", compensate={"method": "POST", "path": "/refund"}),
    ]
    result = await _run(steps, monkeypatch, rec)

    assert result.ok is False
    assert result.failed_index == 0
    assert result.completed == []
    assert result.compensations == []
    # Only the failed first write fired; charge never ran, no compensation.
    assert rec.calls == [("POST", "/reserve")]


# ---------------------------------------------------------------------------
# Idempotency: a compensation carries a derived, distinct idempotency key
# ---------------------------------------------------------------------------


async def test_compensation_idempotency_key_is_derived_and_distinct(monkeypatch, _capture_outcomes):
    """A forward step's idempotency key suffixes ``:compensate`` on its
    compensation so a retried rollback dedupes without colliding with the
    forward write's key on the backend."""
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Idempotency-Key")))
        # Step 2 fails so step 1 compensates.
        if request.url.path == "/charge":
            return httpx.Response(500, json={})
        return httpx.Response(200, json={})

    rec = _Recorder({})
    rec.handler = handler  # type: ignore[method-assign]

    steps = [
        _step(
            "reserve",
            "POST",
            "/reserve",
            compensate={"method": "POST", "path": "/refund"},
            idempotency_key="key-reserve",
        ),
        _step("charge", "POST", "/charge"),
    ]
    await _run(steps, monkeypatch, rec)

    keys = dict(seen)
    assert keys["/reserve"] == "key-reserve"
    # The compensation's key is the forward key + ":compensate".
    assert keys["/refund"] == "key-reserve:compensate"
