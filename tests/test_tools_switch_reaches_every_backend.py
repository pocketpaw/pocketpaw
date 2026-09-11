# tests/test_tools_switch_reaches_every_backend.py — the per-send tool switch,
# at the pool seam and on the Claude SDK backend.
#
# Created 2026-09-12 (fix/tools-switch-sdk-backend), after the switch crashed
# in production with ``TypeError: run() got an unexpected keyword argument
# 'tools_enabled'``.
#
# The bug worth remembering: the forward was guarded by withhold-when-empty
# alone, on the reasoning that a default True says nothing so only an explicit
# False should ride. That is true and it is not enough. Withholding narrows
# WHEN the kwarg is sent, never WHERE it goes — and the moment a user actually
# turned the switch off, that False went to whatever backend their agent runs
# on. A logged-in workspace runs on ``claude_agent_sdk``, which did not declare
# the parameter. The comment three lines below the forward already said this,
# about a different kwarg.
#
# Mutations: tests/mutations/otherhand_tools_toggle.json.

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.backend import _accepts_tools_enabled_kwarg
from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.agents.pool import AgentPool
from pocketpaw.config import Settings


class _NarrowBackend:
    """A backend with the narrow signature six of the eight really have.

    NO ``**kwargs``. That is the whole point: a capturing double that swallows
    anything cannot reproduce the crash, which is why the first version of
    this test passed while production raised ``TypeError``.
    """

    def __init__(self) -> None:
        self.called = False

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[object]:
        self.called = True
        return
        yield  # pragma: no cover — makes this an async generator


class _WideBackend:
    """A backend that declares the switch, like the Claude SDK one now does."""

    def __init__(self) -> None:
        self.seen: dict | None = None

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list | None = None,
        session_key: str | None = None,
        tools_enabled: bool = True,
    ) -> AsyncIterator[object]:
        self.seen = {"tools_enabled": tools_enabled}
        return
        yield  # pragma: no cover — makes this an async generator


async def _drive(monkeypatch, backend, **run_kwargs) -> None:
    pool = AgentPool()
    inst = SimpleNamespace(
        backend=backend,
        soul_manager=None,
        config={"soul_persona": "P", "system_prompt": ""},
        last_active=datetime.now(UTC),
        active_runs=0,
    )

    async def _fake_get(agent_id):
        return inst

    monkeypatch.setattr(pool, "get", _fake_get)
    async for _ in pool.run("a1", "hello", "session:s1", **run_kwargs):
        pass


# ── the pool seam ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tools_off_turn_on_a_narrow_backend_does_not_crash(monkeypatch) -> None:
    """The production crash, reproduced at the seam that produced it.

    The double has NO ``**kwargs`` on purpose. The pool's existing capturing
    double does, which is exactly why nothing caught this: a double that
    swallows every keyword cannot fail the way a real backend fails.

    Mutation that must break this: drop ``_accepts_tools_enabled_kwarg`` from
    the pool's forward.
    """
    backend = _NarrowBackend()

    await _drive(monkeypatch, backend, tools_enabled=False)

    assert backend.called, "the turn never reached the backend"


@pytest.mark.asyncio
async def test_a_tools_off_turn_still_reaches_a_backend_that_declares_it(
    monkeypatch,
) -> None:
    """The other half. Skipping the forward entirely would pass the test above
    and leave the switch dead everywhere.
    """
    backend = _WideBackend()

    await _drive(monkeypatch, backend, tools_enabled=False)

    assert backend.seen == {"tools_enabled": False}


@pytest.mark.asyncio
async def test_a_tools_on_turn_sends_nothing(monkeypatch) -> None:
    """Withhold-when-empty, still. A default True says nothing, so sending it
    would only narrow which backends can be called.
    """
    backend = _WideBackend()

    await _drive(monkeypatch, backend)

    assert backend.seen == {"tools_enabled": True}, "the default was overwritten"


def test_a_narrow_backend_is_not_offered_the_kwarg() -> None:
    """The helper itself, in isolation."""

    async def narrow_run(message, *, system_prompt=None, session_key=None):
        yield None

    assert _accepts_tools_enabled_kwarg(narrow_run) is False


def test_a_backend_that_declares_it_is_offered_the_kwarg() -> None:
    """The other half. Without this, a helper that always answered False would
    pass the test above and silently disable the switch everywhere.
    """

    async def wide_run(message, *, tools_enabled: bool = True):
        yield None

    assert _accepts_tools_enabled_kwarg(wide_run) is True


def test_the_claude_sdk_backend_declares_it() -> None:
    """The backend the crash happened on. A signature test rather than a run
    test because the signature IS the contract the pool asks about.
    """
    params = inspect.signature(ClaudeSDKBackend.run).parameters

    assert "tools_enabled" in params
    assert params["tools_enabled"].default is True, "the switch must default to on"


# ── what "off" means on the Claude SDK backend ───────────────────────────


def _built(tools_enabled: bool):
    backend = ClaudeSDKBackend(Settings(agent_backend="claude_agent_sdk"))
    return asyncio.run(
        backend._build_options(
            "hi",
            system_prompt="test",
            history=None,
            session_key="s1",
            deny_mcp_tool_ids=frozenset(),
            allow_sdk_tools=frozenset(),
            allow_mcp_tool_ids=None,
            skill_names=frozenset(),
            stderr_sink=[],
            tools_enabled=tools_enabled,
        )
    )


def test_tools_off_sets_the_base_tool_set_empty() -> None:
    """``tools=[]`` is the lever, and emptying ``allowed_tools`` is NOT.

    Measured in the SDK's own CLI transport: it extends the command with
    ``--allowed-tools`` only ``if effective_allowed_tools:``. An empty allowlist
    is therefore not "allow nothing", it is "say nothing", and the CLI falls
    back to its DEFAULT tool set. A switch built on the allowlist alone would
    read Off and change nothing, which is how this switch already shipped
    broken once.

    ``tools=[]`` becomes ``--tools ""``.

    Mutation that must break this: drop the ``options_kwargs["tools"] = []``.
    """
    off = _built(False)

    assert getattr(off.options, "tools", None) == []


def test_tools_off_registers_no_mcp_servers() -> None:
    """An MCP server is a tool source, and one whose ids are off the allowlist
    still pays its startup.
    """
    off = _built(False)

    assert not (getattr(off.options, "mcp_servers", None) or {})


def test_tools_off_empties_the_allowlist_so_the_cache_key_differs() -> None:
    """The warm-client cache key is built from ``allowed_tools``. Without this
    a tools-off turn is served the client built WITH tools — the one-slot
    problem this switch has already hit once on the other backend.
    """
    off = _built(False)

    assert list(getattr(off.options, "allowed_tools", []) or []) == []


def test_tools_on_is_unchanged() -> None:
    """The default path. Without this, hard-coding the switch off would pass
    every test above.
    """
    on = _built(True)

    assert getattr(on.options, "tools", None) is None, "the base set must stay unset"
    assert list(getattr(on.options, "allowed_tools", []) or []), "the allowlist vanished"
