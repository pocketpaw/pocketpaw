# tests/test_agent_failover.py — L2 cross-backend harness failover (MCG-10).
#
# Created: 2026-06-26 (integration/model-catalog-v2, WU-D / MCG-10).
#
# Covers the harness-failover MECHANISM in ``pocketpaw.agents.failover`` and its
# OSS hook ``AgentRouter.run_with_failover``:
#   * lane-down BEFORE streaming → next harness tried, run completes, switch
#     audit-logged;
#   * error AFTER streaming → NO failover (error surfaced, no replay);
#   * a normal (non-lane-down) error → NO failover (surfaced);
#   * chain exhausted → last error surfaced;
#   * disabled-by-default → delegates to ``run`` (no harness switch);
#   * lane-down classification table (overload / unavailable / auth vs a normal
#     task error);
#   * both signal shapes — a RAISED exception and an error AgentEvent.

from __future__ import annotations

import pytest

from pocketpaw.agents.failover import (
    BackendFailoverRunner,
    classify_lane_failure,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.router import AgentRouter
from pocketpaw.config import Settings

# ── Fake backends ────────────────────────────────────────────────────────


class _RaisesLaneDown:
    """Harness whose lane is down — raises an overload error before streaming."""

    def __init__(self, settings=None):
        pass

    async def run(self, message, **kwargs):
        raise RuntimeError("anthropic.APIStatusError: 529 {'type': 'overloaded_error'}")
        yield  # pragma: no cover — makes this an async generator.


class _ErrorEventLaneDown:
    """Harness that EMITS a lane-down error event (the common path) pre-stream."""

    def __init__(self, settings=None):
        pass

    async def run(self, message, **kwargs):
        yield AgentEvent(type="error", content="503 Service Unavailable (upstream)")
        yield AgentEvent(type="done", content="")


class _NormalTaskError:
    """Harness that fails with a NORMAL task error (not lane-down)."""

    def __init__(self, settings=None):
        pass

    async def run(self, message, **kwargs):
        yield AgentEvent(type="error", content="the tool returned an invalid result")
        yield AgentEvent(type="done", content="")


class _StreamsThenLaneDown:
    """Harness that streams a token, THEN hits a lane-down error.

    The no-replay guard must prevent failover here even though the error text
    is lane-down — output already went to the user.
    """

    def __init__(self, settings=None):
        pass

    async def run(self, message, **kwargs):
        yield AgentEvent(type="message", content="partial answer...")
        raise RuntimeError("429 rate limit exceeded")
        yield  # pragma: no cover


class _StreamsThenErrorEvent:
    """Streams a token, then emits a lane-down ERROR EVENT (no raise)."""

    def __init__(self, settings=None):
        pass

    async def run(self, message, **kwargs):
        yield AgentEvent(type="message", content="partial answer...")
        yield AgentEvent(type="error", content="529 overloaded_error")
        yield AgentEvent(type="done", content="")


class _Succeeds:
    """Healthy harness — streams a message and completes."""

    def __init__(self, settings=None):
        pass

    async def run(self, message, **kwargs):
        yield AgentEvent(type="message", content="recovered on fallback harness")
        yield AgentEvent(type="done", content="")


class _RecordsKwargs:
    """Healthy harness that records the kwargs it was called with."""

    seen: dict = {}

    def __init__(self, settings=None):
        pass

    async def run(self, message, **kwargs):
        _RecordsKwargs.seen = {"message": message, **kwargs}
        yield AgentEvent(type="message", content="ok")
        yield AgentEvent(type="done", content="")


def _factory(mapping):
    """Build a get_backend(name) factory from a {name: instance} mapping."""

    def _get(name):
        return mapping.get(name)

    return _get


async def _collect(runner, message="hi", **kw):
    return [e async for e in runner.run(message, **kw)]


# ── classify_lane_failure ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        # Overload / capacity
        ("anthropic.APIStatusError: 529 overloaded_error", True),
        ("Overloaded", True),
        ("429 Too Many Requests", True),
        ("rate limit exceeded", True),
        ("you have exceeded your quota", True),
        ("usage limit reached for this account", True),
        # Service unavailable / outage
        ("503 Service Unavailable", True),
        ("502 Bad Gateway", True),
        ("upstream connect error", True),
        ("the service is temporarily unavailable", True),
        # Auth / credential
        ("401 Unauthorized", True),
        ("invalid api key", True),
        ("403 Forbidden: permission denied", True),
        # NOT lane-down — normal task errors
        ("the model produced an invalid JSON response", False),
        ("tool 'search' raised ValueError", False),
        ("I cannot help with that request", False),
        ("", False),
        (None, False),
    ],
)
def test_classify_lane_failure(text, expected):
    assert classify_lane_failure(text) is expected


# ── failover happy path: raised lane-down → next harness ─────────────────


@pytest.mark.asyncio
async def test_failover_on_raised_lane_down(monkeypatch):
    """Primary raises a lane-down error pre-stream → next harness completes."""
    audited = []
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: audited.append(kw),
    )

    mapping = {"primary": _RaisesLaneDown(), "secondary": _Succeeds()}
    runner = BackendFailoverRunner(["primary", "secondary"], _factory(mapping))

    events = await _collect(runner)

    # The run completed on the SECOND harness.
    assert any(e.type == "message" and e.content == "recovered on fallback harness" for e in events)
    assert events[-1].type == "done"
    # No error event leaked from the failed primary.
    assert not any(e.type == "error" for e in events)
    # The switch was audit-logged with the right from/to + error class.
    assert audited and audited[0]["from_backend"] == "primary"
    assert audited[0]["to_backend"] == "secondary"
    assert audited[0]["via"] == "exception"


@pytest.mark.asyncio
async def test_failover_on_error_event_lane_down(monkeypatch):
    """Primary EMITS a lane-down error event pre-stream → next harness completes."""
    audited = []
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: audited.append(kw),
    )

    mapping = {"primary": _ErrorEventLaneDown(), "secondary": _Succeeds()}
    runner = BackendFailoverRunner(["primary", "secondary"], _factory(mapping))

    events = await _collect(runner)

    assert any(e.content == "recovered on fallback harness" for e in events)
    # The primary's error event must NOT have been forwarded.
    assert not any(e.type == "error" and "Service Unavailable" in str(e.content) for e in events)
    # ``_audit_switch`` is called with via="error_event" for the error-event path.
    assert audited and audited[0]["via"] == "error_event"
    assert audited[0]["from_backend"] == "primary"


# ── no-replay guard: streamed already → NO failover ──────────────────────


@pytest.mark.asyncio
async def test_no_failover_after_streaming_raise(monkeypatch):
    """A lane-down RAISE after a token was streamed → surface error, NO replay."""
    switched = []
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: switched.append(kw),
    )

    mapping = {"primary": _StreamsThenLaneDown(), "secondary": _Succeeds()}
    runner = BackendFailoverRunner(["primary", "secondary"], _factory(mapping))

    events = await _collect(runner)

    # The partial token was delivered.
    assert any(e.type == "message" and e.content == "partial answer..." for e in events)
    # The error was surfaced (NOT swallowed) ...
    assert any(e.type == "error" for e in events)
    # ... and the secondary harness was NEVER tried.
    assert not any(e.content == "recovered on fallback harness" for e in events)
    assert switched == []


@pytest.mark.asyncio
async def test_no_failover_after_streaming_error_event(monkeypatch):
    """A lane-down ERROR EVENT after a streamed token → surface it, NO replay."""
    switched = []
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: switched.append(kw),
    )

    mapping = {"primary": _StreamsThenErrorEvent(), "secondary": _Succeeds()}
    runner = BackendFailoverRunner(["primary", "secondary"], _factory(mapping))

    events = await _collect(runner)

    assert any(e.type == "message" and e.content == "partial answer..." for e in events)
    # The error event IS forwarded once output has streamed.
    assert any(e.type == "error" and "overloaded" in str(e.content) for e in events)
    assert not any(e.content == "recovered on fallback harness" for e in events)
    assert switched == []


# ── normal task error → NO failover ──────────────────────────────────────


@pytest.mark.asyncio
async def test_no_failover_on_normal_task_error(monkeypatch):
    """A non-lane-down error (normal task failure) → surfaced, no harness switch."""
    switched = []
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: switched.append(kw),
    )

    mapping = {"primary": _NormalTaskError(), "secondary": _Succeeds()}
    runner = BackendFailoverRunner(["primary", "secondary"], _factory(mapping))

    events = await _collect(runner)

    # The task error was surfaced from the primary ...
    assert any(e.type == "error" and "invalid result" in str(e.content) for e in events)
    # ... and we did NOT fall through to the healthy secondary.
    assert not any(e.content == "recovered on fallback harness" for e in events)
    assert switched == []


# ── chain exhausted → last error surfaced ────────────────────────────────


@pytest.mark.asyncio
async def test_chain_exhausted_surfaces_last_error(monkeypatch):
    """Every harness is lane-down → last error surfaced after the chain ends."""
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: None,
    )

    mapping = {
        "a": _RaisesLaneDown(),
        "b": _ErrorEventLaneDown(),
    }
    runner = BackendFailoverRunner(["a", "b"], _factory(mapping))

    events = await _collect(runner)

    # Exactly one error event at the end, carrying the LAST harness's error.
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1
    assert "503" in str(errors[0].content) or "Unavailable" in str(errors[0].content)
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_each_harness_tried_at_most_once(monkeypatch):
    """The chain is consulted once per harness — no infinite loop."""
    calls = {"n": 0}

    class _CountingLaneDown:
        def __init__(self, settings=None):
            pass

        async def run(self, message, **kwargs):
            calls["n"] += 1
            raise RuntimeError("529 overloaded")
            yield  # pragma: no cover

    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: None,
    )

    inst = _CountingLaneDown()
    mapping = {"a": inst, "b": inst, "c": inst}
    runner = BackendFailoverRunner(["a", "b", "c"], _factory(mapping))

    await _collect(runner)
    # Three distinct chain slots, each tried exactly once.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_unavailable_harness_is_skipped(monkeypatch):
    """A None (unavailable) harness in the chain is skipped, not fatal."""
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: None,
    )
    # 'missing' resolves to None; the run should still complete on 'good'.
    mapping = {"missing": None, "good": _Succeeds()}
    runner = BackendFailoverRunner(["missing", "good"], _factory(mapping))

    events = await _collect(runner)
    assert any(e.content == "recovered on fallback harness" for e in events)


# ── kwargs are forwarded verbatim ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_kwargs_forwarded():
    """system_prompt / history / session_key reach the backend unchanged."""
    mapping = {"only": _RecordsKwargs()}
    runner = BackendFailoverRunner(["only"], _factory(mapping))

    await _collect(
        runner,
        message="hello",
        system_prompt="be terse",
        history=[{"role": "user", "content": "x"}],
        session_key="sess-1",
    )

    seen = _RecordsKwargs.seen
    assert seen["message"] == "hello"
    assert seen["system_prompt"] == "be terse"
    assert seen["session_key"] == "sess-1"
    assert seen["history"] == [{"role": "user", "content": "x"}]


# ── audit log actually fires through the real logger ─────────────────────


@pytest.mark.asyncio
async def test_switch_writes_audit_event(monkeypatch):
    """The switch goes through the real audit logger (action=backend_failover)."""
    logged = []

    class _FakeAuditLogger:
        def log(self, event):
            logged.append(event)

    monkeypatch.setattr(
        "pocketpaw.security.audit.get_audit_logger",
        lambda: _FakeAuditLogger(),
    )

    mapping = {"primary": _RaisesLaneDown(), "secondary": _Succeeds()}
    runner = BackendFailoverRunner(["primary", "secondary"], _factory(mapping))
    await _collect(runner)

    switch_events = [e for e in logged if e.action == "backend_failover" and e.status == "switch"]
    assert switch_events, "expected a backend_failover switch audit event"
    ctx = switch_events[0].context
    assert ctx["from_backend"] == "primary"
    assert ctx["to_backend"] == "secondary"
    assert ctx["level"] == "L2_harness"


# ── AgentRouter.run_with_failover hook ───────────────────────────────────


@pytest.mark.asyncio
async def test_router_failover_disabled_by_default(monkeypatch):
    """Flag OFF (default) → run_with_failover delegates to run, NO harness switch.

    Primary raises a lane-down error; because failover is disabled and no
    ``fallback_backends`` are set, the router surfaces the error from ``run``
    rather than switching to the chain's healthy harnesses.
    """
    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "raises_lane_down",
        ("tests.test_agent_failover", "_RaisesLaneDown"),
    )
    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "healthy",
        ("tests.test_agent_failover", "_Succeeds"),
    )

    settings = Settings(
        agent_backend="raises_lane_down",
        # Chain names a healthy harness, but the flag is OFF so it's ignored.
        backend_failover_chain=["raises_lane_down", "healthy"],
        backend_failover_enabled=False,
    )
    router = AgentRouter(settings)

    events = [e async for e in router.run_with_failover("hi")]

    # No switch happened: the healthy harness was never reached.
    assert not any(e.content == "recovered on fallback harness" for e in events)
    assert any(e.type == "error" for e in events)


@pytest.mark.asyncio
async def test_router_failover_enabled_switches_harness(monkeypatch):
    """Flag ON → run_with_failover switches from a lane-down primary to the chain."""
    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "raises_lane_down",
        ("tests.test_agent_failover", "_RaisesLaneDown"),
    )
    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "healthy",
        ("tests.test_agent_failover", "_Succeeds"),
    )
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: None,
    )

    settings = Settings(
        agent_backend="raises_lane_down",
        backend_failover_chain=["raises_lane_down", "healthy"],
        backend_failover_enabled=True,
    )
    router = AgentRouter(settings)

    events = [e async for e in router.run_with_failover("hi")]

    assert any(e.content == "recovered on fallback harness" for e in events)
    assert events[-1].type == "done"
