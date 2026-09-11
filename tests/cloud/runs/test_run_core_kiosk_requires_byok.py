# tests/cloud/runs/test_run_core_kiosk_requires_byok.py — a signed-up kiosk
# turn must bring its own key too, while the flag is on.
#
# Created 2026-09-12 (feat/kiosk-require-byok). The account sibling of the
# guest no-keyless-turns rule, for the window between launching the kiosk and
# switching billing on.
#
# Four things these pin, in the order they would hurt if they broke:
#
#   * flag OFF is byte-identical — the default, so every existing deploy and
#     the whole of Paw OS are untouched until an operator sets one env var;
#   * a NON-kiosk surface is never gated, even with the flag on — the same
#     deployment serves the full Paw OS, where the platform fallback IS the
#     product;
#   * a guest still gets ``guest_key_required``, not the new code — two rules,
#     two codes, because a guest is told to create an account and an account
#     is told to add a key;
#   * a resolved key proceeds normally.
#
# Harness cloned from test_run_core_byok_wiring.py.
#
# Mutations: tests/mutations/kiosk_require_byok.json.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.byok.service import TurnCredentials
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, SurfaceMeta

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_settings():
    """``get_settings`` is ``lru_cache``d, so a flag set after the first call in
    the process is invisible. Without this the first test to run pins the value
    for the rest of the file, and the others pass or fail on ordering rather
    than on behaviour. Cleared on the way IN and OUT so neither this file nor
    the next one inherits a stale Settings.
    """
    from pocketpaw.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

_PLAINTEXT = "sk-ant-api03-" + "kiosk" * 9


class _Ev:
    def __init__(self, type_: str, content: str = "") -> None:
        self.type = type_
        self.content = content
        self.metadata: dict[str, Any] = {}


class _CapturePool:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None
        self.run_called = False

    async def get(self, _agent_id):
        return SimpleNamespace(config={"backend": "claude_agent_sdk", "model": ""}, agent_name="A")

    def run(self, agent_id, content, session_key, **kwargs):
        self.run_called = True
        self.run_kwargs = kwargs

        async def _gen():
            yield _Ev("text", "ok")
            yield _Ev("done")

        return _gen()


def _ctx(*, surface: SurfaceKind | None, user_id: str = "u1") -> ScopeContext:
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id=user_id,
        members=[user_id],
        target_agent_id="a1",
    )
    # The REAL SurfaceContext, not a namespace double: the turn path reads
    # ``preamble`` and ``preamble_cache_key`` off it long before the gate, so a
    # partial double fails for a reason that has nothing to do with the gate.
    ctx.surface_context = (
        None
        if surface is None
        else SurfaceContext(
            workspace_id="w1",
            user_id=user_id,
            kind=surface,
            meta=SurfaceMeta(),
            preamble="",
            preamble_cache_key=None,
        )
    )
    return ctx


async def _not_guest(_user_id):
    return None


async def _is_guest(_user_id):
    return SimpleNamespace(id="g1", active_workspace="w1")


async def _never_cancelled():
    return False


async def _drive(
    monkeypatch,
    ctx: ScopeContext,
    *,
    creds: TurnCredentials,
    guest_loader=_not_guest,
) -> tuple[_CapturePool, list[tuple[str, dict]]]:
    monkeypatch.delenv("POCKETPAW_SESSION_SUPERVISOR", raising=False)

    pool = _CapturePool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool, raising=False)

    from pocketpaw_ee.cloud.auth import guest_budget
    from pocketpaw_ee.cloud.byok import service as byok_service

    async def _resolve(_ws):
        return creds

    monkeypatch.setattr(byok_service, "resolve_turn_credentials", _resolve)
    monkeypatch.setattr(guest_budget, "load_guest", guest_loader)

    out: list[tuple[str, dict]] = []
    gen = run_core._drive_agent_loop(
        ctx,
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    )
    async for ev in gen:
        out.append(ev)
    return pool, out


def _codes(events) -> list[str]:
    return [e[1].get("code") for e in events if e[0] == "error"]


# ── the flag ─────────────────────────────────────────────────────────────


async def test_flag_off_keeps_the_platform_fallback(monkeypatch):
    """The DEFAULT, and the one that must not regress: with the flag unset a
    signed-up kiosk turn runs on platform credentials exactly as before.

    Mutation that must break this: invert the flag check.
    """
    monkeypatch.delenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", raising=False)

    pool, events = await _drive(
        monkeypatch,
        _ctx(surface=SurfaceKind.OTHER_HAND),
        creds=TurnCredentials(source="platform"),
    )

    assert pool.run_called, "the turn was refused with the flag off"
    assert "byok_key_required" not in _codes(events)


async def test_flag_on_refuses_a_keyless_account_on_the_kiosk(monkeypatch):
    """The rule itself.

    Mutation that must break this: return False from ``_requires_own_key``.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")

    pool, events = await _drive(
        monkeypatch,
        _ctx(surface=SurfaceKind.OTHER_HAND),
        creds=TurnCredentials(source="platform"),
    )

    assert _codes(events) == ["byok_key_required"]
    assert not pool.run_called, "a keyless turn reached the model anyway"


# ── scope ────────────────────────────────────────────────────────────────


async def test_another_surface_is_never_gated(monkeypatch):
    """The same deployment serves the full Paw OS, where the platform fallback
    is the product. A workspace-wide gate here would take chat off the air for
    every non-kiosk user the moment the env var is set.

    Mutation that must break this: drop the surface check.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")

    pool, events = await _drive(
        monkeypatch,
        _ctx(surface=SurfaceKind.GENERIC),
        creds=TurnCredentials(source="platform"),
    )

    assert pool.run_called
    assert "byok_key_required" not in _codes(events)


async def test_a_run_with_no_surface_is_never_gated(monkeypatch):
    """A legacy client that sends no surface hint resolves to no context. It
    is not on the kiosk, so it keeps the fallback.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")

    pool, _events = await _drive(
        monkeypatch,
        _ctx(surface=None),
        creds=TurnCredentials(source="platform"),
    )

    assert pool.run_called


# ── the two rules stay two rules ─────────────────────────────────────────


async def test_a_guest_still_gets_the_guest_code(monkeypatch):
    """Answering a guest with ``byok_key_required`` would show them "add a key
    in Settings" when what they need is an account. The guest branch runs
    first and returns, so this one never sees them.

    Mutation that must break this: move the account gate above the guest one.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")

    _pool, events = await _drive(
        monkeypatch,
        _ctx(surface=SurfaceKind.OTHER_HAND),
        creds=TurnCredentials(source="platform"),
        guest_loader=_is_guest,
    )

    assert _codes(events) == ["guest_key_required"]


async def test_an_account_with_a_key_proceeds(monkeypatch):
    """The happy path. Without it, refusing every kiosk turn would pass every
    test above.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")

    pool, events = await _drive(
        monkeypatch,
        _ctx(surface=SurfaceKind.OTHER_HAND),
        creds=TurnCredentials(source="byok", api_key=_PLAINTEXT, provider="anthropic"),
    )

    assert pool.run_called
    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("byok_api_key") == _PLAINTEXT
    assert _codes(events) == []
