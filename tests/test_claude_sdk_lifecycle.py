# tests/test_claude_sdk_lifecycle.py
# Pins the warm-client lifecycle of the Claude SDK backend: who may evict,
# interrupt or disconnect a CLI subprocess, what a retry carries, and what the
# user sees when a turn ends in an error.
#
# Guarantees held here (one block per audit finding):
#   B1  prewarm never evicts a client a run is using, and never replaces a client
#       that already served this session; pool.prewarm threads the per-send model
#       and tool switch so the prewarmed key matches the turn's.
#   B2  the Bun-crash retry forwards EVERY run() parameter (checked against the
#       signature, so a new kwarg cannot be silently dropped).
#   B4  a connect() that hangs is abandoned after the configured timeout and the
#       turn falls back like any failed connect.
#   B5  a leased warm client abandoned mid-stream is never reused dirty.
#   B6  an "API Error" result is one clear error, never assistant text.
#   B7  a max-turns stop names the limit instead of "None".
#   B8  cleanup() does not break a lease it does not own, a run's teardown only
#       touches its own client, and stop() is scoped to one session.
#   B9  a failed persistent client is disconnected before it is dropped, the
#       spill file is refreshed and written with LF, CLI detection honours the
#       bundled CLI and cli_path, and a parse error on the stateless path is
#       surfaced instead of ending the reply silently.
#
# The fake-SDK harness is shared with tests/test_claude_sdk_prewarm.py.

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pocketpaw.agents.backend import LeasedClient
from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from tests.test_claude_sdk_prewarm import _make_sdk, _make_settings, _patched

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Scripted fake SDK                                                            #
# --------------------------------------------------------------------------- #


class _SE:
    """A streamed text delta (``StreamEvent``)."""

    def __init__(self, text: str) -> None:
        self.event = {"type": "content_block_delta", "delta": {"text": text}}


class _AM:
    """An ``AssistantMessage``; ``error`` is set on a synthetic API-error reply."""

    def __init__(self, text: str, *, error: str | None = None) -> None:
        self.content = [SimpleNamespace(text=text)]
        self.model = "claude-test"
        self.error = error


class _RM:
    def __init__(
        self,
        *,
        is_error: bool = False,
        result: str | None = "ok",
        subtype: str = "success",
        num_turns: int = 1,
        terminal_reason: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        self.is_error = is_error
        self.result = result
        self.subtype = subtype
        self.num_turns = num_turns
        self.terminal_reason = terminal_reason
        self.errors = errors
        self.total_cost_usd = None
        self.usage = {}


class _ScriptClient:
    """Persistent client driven by a per-turn script.

    ``turns`` is a list of lists, one per ``query()``; each item is a message to
    yield or an ``asyncio.Event`` to wait on (a stream that is still running).
    """

    def __init__(self, registry: list, turns: list, *, options=None, connect_hangs=False, **_kw):
        registry.append(self)
        self.options = options
        self._turns = turns
        self._connect_hangs = connect_hangs
        self.queries: list = []
        self.interrupted = 0
        self.disconnected = False
        self._current: list = []

    async def connect(self, prompt=None):
        if self._connect_hangs:
            await asyncio.Event().wait()

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        self._current = list(self._turns.pop(0)) if self._turns else [_RM()]

    async def receive_messages(self):
        while self._current:
            item = self._current.pop(0)
            if isinstance(item, asyncio.Event):
                await item.wait()
                continue
            yield item

    async def interrupt(self):
        self.interrupted += 1

    async def disconnect(self):
        self.disconnected = True


def _scripted_sdk(turns: list | None = None, *, settings=None, connect_hangs=False):
    counter = [0]
    sdk = _make_sdk(counter, settings=settings)
    clients: list[_ScriptClient] = []
    script = turns if turns is not None else []
    sdk._ClaudeSDKClient = lambda **kw: _ScriptClient(
        clients, script, connect_hangs=connect_hangs, **kw
    )
    sdk._StreamEvent = _SE
    sdk._AssistantMessage = _AM
    sdk._ResultMessage = _RM
    return sdk, clients


def _stateless(sdk, messages: list, *, raise_after: BaseException | None = None):
    calls: list = []

    async def _query(prompt, options):
        calls.append(prompt)
        for m in messages:
            yield m
        if raise_after is not None:
            raise raise_after

    sdk._query = _query
    return calls


async def _collect(sdk, message="hi", **kw):
    kw.setdefault("system_prompt", "identity")
    kw.setdefault("session_key", "s1")

    async def _go():
        return [ev async for ev in sdk.run(message, **kw)]

    return await _patched(_go)()


# --------------------------------------------------------------------------- #
# B1 — prewarm must not evict                                                  #
# --------------------------------------------------------------------------- #


async def test_b1_prewarm_keeps_a_client_that_served_this_session():
    sdk, clients = _scripted_sdk()
    await _collect(sdk, model_override="claude-picked")
    assert len(clients) == 1

    async def _go():
        await sdk.prewarm(session_key="s1", system_prompt="identity")

    await _patched(_go)()
    assert len(clients) == 1, "prewarm connected a second client for a session already warm"
    assert not clients[0].disconnected, "prewarm evicted the session's served client"
    assert sdk._client is clients[0]


async def test_b1_prewarm_never_evicts_a_client_in_use():
    sdk, clients = _scripted_sdk()
    await _collect(sdk)
    live = clients[0]

    real_build = sdk._build_options

    async def _build_then_lease(*a, **k):
        built = await real_build(*a, **k)
        # A run acquires the lease while prewarm is between its entry check and
        # the client lock.
        sdk._client_in_use = True
        return built

    sdk._build_options = _build_then_lease

    async def _go():
        await sdk.prewarm(session_key="s2", system_prompt="identity")

    await _patched(_go)()
    assert not live.disconnected, "prewarm disconnected a client a run was streaming on"
    assert sdk._client is live


async def test_b1_prewarm_with_the_turns_model_is_reused_by_the_turn():
    sdk, clients = _scripted_sdk()

    async def _go():
        await sdk.prewarm(
            session_key="s1",
            system_prompt="identity",
            model_override="claude-picked",
            tools_enabled=False,
        )

    await _patched(_go)()
    assert len(clients) == 1
    await _collect(sdk, model_override="claude-picked", tools_enabled=False)
    assert len(clients) == 1, "the turn rebuilt a client prewarmed with its own model"


async def test_b1_pool_prewarm_forwards_model_and_tool_switch(monkeypatch):
    from pocketpaw.agents.pool import AgentPool

    seen: dict = {}

    class _Backend:
        async def prewarm(
            self, *, session_key, system_prompt, model_override=None, tools_enabled=True, **kw
        ):
            seen.update(model_override=model_override, tools_enabled=tools_enabled)

    instance = SimpleNamespace(
        agent_id="a1",
        agent_name="Paw",
        backend=_Backend(),
        soul_manager=None,
        config={"soul_persona": "P", "system_prompt": ""},
        memory_namespace="ns",
        created_from_updated_at=None,
        active_runs=0,
    )
    pool = AgentPool()

    async def _get(_aid):
        return instance

    monkeypatch.setattr(pool, "get", _get)
    await pool.prewarm("a1", "k", model_override="claude-picked", tools_enabled=False)
    assert seen == {"model_override": "claude-picked", "tools_enabled": False}


# --------------------------------------------------------------------------- #
# B2 — the Bun-crash retry forwards every parameter                            #
# --------------------------------------------------------------------------- #


async def test_b2_bun_crash_retry_forwards_every_run_parameter():
    sdk, _ = _scripted_sdk()

    async def _crashing_build(message, **kw):
        kw["stderr_sink"].append("Bun has crashed: switch on corrupt value")
        raise RuntimeError("Command failed with exit code 3")

    sdk._build_options = _crashing_build

    params = [
        p
        for name, p in inspect.signature(ClaudeSDKBackend.run).parameters.items()
        if name not in ("self", "message")
    ]
    sent = {p.name: object() for p in params}

    retried: dict = {}

    async def _spy(message, **kw):
        retried.update(kw)
        retried["__message__"] = message
        return
        yield  # pragma: no cover

    original = sdk.run
    sdk.run = _spy
    async for _ in original("again please", **sent):
        pass

    assert retried.get("__message__") == "again please"
    missing = [name for name in sent if name not in retried]
    assert not missing, f"the crash retry dropped run() parameters: {missing}"
    for name, value in sent.items():
        if name == "warm_client":
            # The crashed lease is not re-driven; the retry builds fresh.
            assert retried[name] is None
            continue
        assert retried[name] is value, f"the crash retry changed {name}"


# --------------------------------------------------------------------------- #
# B4 — a hung connect() is abandoned                                           #
# --------------------------------------------------------------------------- #


async def test_b4_hung_connect_times_out_and_falls_back():
    settings = _make_settings(claude_sdk_connect_timeout=0.05)
    sdk, clients = _scripted_sdk(settings=settings, connect_hangs=True)
    _stateless(sdk, [_RM()])

    events = await asyncio.wait_for(_collect(sdk), timeout=5)
    assert any(e.type == "done" for e in events)
    assert clients and clients[0].disconnected, "the hung client was not torn down"
    assert sdk._client is None
    assert sdk._client_in_use is False


# --------------------------------------------------------------------------- #
# B5 — an abandoned leased stream never serves the next turn dirty              #
# --------------------------------------------------------------------------- #


async def test_b5_abandoned_leased_client_is_not_reused():
    gate = asyncio.Event()
    sdk, clients = _scripted_sdk([[_RM()], [_SE("partial"), gate, _RM()], [_RM()]])
    bound: list = []

    def _on_built(client, key, teardown):
        bound.append(LeasedClient(client=client, options_key=key))

    await _collect(sdk, on_client_built=_on_built)
    assert len(bound) == 1
    lease = bound[0]

    async def _abandon():
        agen = sdk.run(
            "second", system_prompt="identity", session_key="s1", warm_client=lease,
            on_client_built=_on_built,
        )
        first = await agen.__anext__()
        assert first.type == "message"
        await agen.aclose()  # the consumer went away before the ResultMessage

    await _patched(_abandon)()
    assert lease.busy is False

    await _collect(sdk, "third", warm_client=lease, on_client_built=_on_built)
    assert len(bound) == 2, (
        "the next turn reused a leased client whose previous turn never finished, "
        "so it would read that turn's stale tail"
    )


# --------------------------------------------------------------------------- #
# B6 / B7 — error results                                                      #
# --------------------------------------------------------------------------- #

_VERSION_ERR = (
    "API Error: 400 Claude Code 2.1.276 does not support this model. "
    "Run 'claude update' to get the latest version."
)


async def test_b6_api_error_result_is_one_clear_error_and_no_reply_text():
    sdk, _ = _scripted_sdk(
        [[_AM(_VERSION_ERR, error="invalid_request"), _RM(is_error=True, result=_VERSION_ERR)]]
    )
    events = await _collect(sdk, model_override="claude-opus-5-5")
    assert not [e for e in events if e.type == "message"], "API error text reached the reply"
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1, f"expected one error, got {[e.content for e in errors]}"
    assert "POCKETPAW_CLAUDE_SDK_CLI_PATH" in errors[0].content


async def test_b7_max_turns_error_names_the_limit():
    sdk, _ = _scripted_sdk(
        [[_RM(is_error=True, result=None, subtype="error_max_turns", num_turns=4)]]
    )
    events = await _collect(sdk)
    errors = [e.content for e in events if e.type == "error"]
    assert errors and "None" not in errors[0]
    assert "limit" in errors[0].lower()


# --------------------------------------------------------------------------- #
# B8 — lease ownership and session-scoped stop                                 #
# --------------------------------------------------------------------------- #


async def _start_streaming_run(sdk, *, session_key="s1", message="long"):
    agen = sdk.run(message, system_prompt="identity", session_key=session_key)
    first = await agen.__anext__()
    assert first.type == "message"
    return agen


async def test_b8_cleanup_does_not_release_a_lease_it_does_not_own():
    gate = asyncio.Event()
    sdk, _ = _scripted_sdk([[_SE("a"), gate, _RM()]])

    async def _go():
        agen = await _start_streaming_run(sdk)
        await sdk.cleanup()
        in_use = sdk._client_in_use
        gate.set()
        async for _ in agen:
            pass
        return in_use

    assert await _patched(_go)() is True, "cleanup() released a running turn's lease"
    assert sdk._client_in_use is False


async def test_b8_run_teardown_only_disconnects_its_own_client():
    gate = asyncio.Event()
    sdk, clients = _scripted_sdk([[_SE("a"), gate, _RM()]])
    other = _ScriptClient([], [])

    async def _go():
        agen = await _start_streaming_run(sdk)
        # Another run now owns the backend's client slot.
        sdk._client = other
        await agen.aclose()

    await _patched(_go)()
    assert not other.disconnected, "a finishing run disconnected another run's client"
    assert clients[0].disconnected, "the abandoned run's own client must be torn down"


async def test_b8_stop_for_one_session_leaves_another_sessions_stream_alone():
    gate = asyncio.Event()
    sdk, clients = _scripted_sdk([[_SE("a"), gate, _SE("b"), _RM()]])

    async def _go():
        agen = await _start_streaming_run(sdk, session_key="s1")
        await sdk.stop(session_key="s2")
        gate.set()
        return [e async for e in agen]

    rest = await _patched(_go)()
    assert clients[0].interrupted == 0 and not clients[0].disconnected
    assert any(e.type == "message" and e.content == "b" for e in rest)


async def test_b8_a_new_run_does_not_cancel_a_pending_stop():
    gate = asyncio.Event()
    sdk, _ = _scripted_sdk([[_SE("a"), gate, _SE("b"), _RM()]])
    _stateless(sdk, [_RM()])

    async def _go():
        agen = await _start_streaming_run(sdk, session_key="s1")
        await sdk.stop(session_key="s1")
        # A sibling turn on another session starts while s1 is still unwinding.
        [ev async for ev in sdk.run("x", system_prompt="identity", session_key="s2")]
        gate.set()
        return [e async for e in agen]

    rest = await _patched(_go)()
    assert not any(e.type == "message" and e.content == "b" for e in rest), (
        "the stopped run kept streaming because a new run reset the stop flag"
    )


async def test_b8_router_stop_passes_the_session_to_a_backend_that_takes_it():
    from pocketpaw.agents.router import AgentRouter

    seen: list = []

    class _Backend:
        async def stop(self, session_key=None):
            seen.append(session_key)

    router = AgentRouter.__new__(AgentRouter)
    router._backend = _Backend()
    router._fallback_instances = {}
    await router.stop(session_key="s9")
    assert seen == ["s9"]


# --------------------------------------------------------------------------- #
# B9 — small leaks and papercuts                                               #
# --------------------------------------------------------------------------- #


async def test_b9_failed_persistent_client_is_disconnected_before_fallback():
    sdk, clients = _scripted_sdk()
    _stateless(sdk, [_RM()])

    async def _boom(prompt, session_id="default"):
        raise RuntimeError("pipe closed")

    real_factory = sdk._ClaudeSDKClient

    def _factory(**kw):
        c = real_factory(**kw)
        c.query = _boom
        return c

    sdk._ClaudeSDKClient = _factory
    events = await _collect(sdk)
    assert any(e.type == "done" for e in events)
    assert clients[0].disconnected, "the broken client was dropped without disconnect()"


async def test_b9_stateless_parse_error_is_surfaced():
    sdk, _ = _scripted_sdk()

    class MessageParseError(Exception):
        pass

    _stateless(sdk, [_SE("half a rep")], raise_after=MessageParseError("bad frame"))
    sdk._client_in_use = True  # a sibling holds the lease → stateless path
    events = await _collect(sdk)
    assert any(e.type == "error" for e in events), "the cut-off reply ended silently"


async def test_b9_spill_fast_path_refreshes_mtime(tmp_path, monkeypatch):
    from pocketpaw.agents import claude_sdk as mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    prompt = "line one\nline two\n" * 10
    path = mod._spill_prompt_to_file(prompt)
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))
    assert mod._spill_prompt_to_file(prompt) == path
    assert path.stat().st_mtime > old + 1800, "a reused spill kept its old mtime"


async def test_b9_spill_is_written_with_lf(tmp_path, monkeypatch):
    from pocketpaw.agents import claude_sdk as mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = mod._spill_prompt_to_file("a\nb\n")
    assert path.read_bytes() == b"a\nb\n"


def test_b9_cli_detection_honours_cli_path_and_bundled(tmp_path):
    from pocketpaw.agents import claude_sdk as mod

    exe = tmp_path / "claude-new"
    exe.write_text("#!/bin/sh\n")
    with (
        patch("shutil.which", return_value=None),
        patch.object(mod, "_bundled_cli_path", return_value=None),
    ):
        assert mod._claude_cli_available(str(exe)) is True
        assert mod._claude_cli_available(None) is False
        assert mod._claude_cli_available(str(tmp_path / "missing")) is False
    with (
        patch("shutil.which", return_value=None),
        patch.object(mod, "_bundled_cli_path", return_value=exe),
    ):
        assert mod._claude_cli_available(None) is True
