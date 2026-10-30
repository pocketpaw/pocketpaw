"""Per-member action grants apply, and never substitute for membership.

``_has_action_override`` was sync and reached an async service through::

    asyncio.get_event_loop().run_until_complete(get_member_action_overrides(...))

Every caller is an async request handler, so a loop is already running and that
raises ``RuntimeError: This event loop is already running``. A bare
``except Exception`` swallowed it, ``overrides`` became ``[]``, and the empty
list was cached in a dict with no expiry ("Lives for process lifetime").

So every per-member grant an admin made in the UI silently did nothing, for the
life of the process. It failed CLOSED — users were denied actions they had been
explicitly granted — so this is a permissions bug rather than an escalation.

WHY THE OBVIOUS FIX IS WORSE THAN THE BUG

``check_workspace_action`` had both checks in one ``try``::

    try:
        role = resolve_workspace_role(user, workspace_id)   # may raise not_member
        check_action(action, role)                          # may raise denied
    except Forbidden:
        if _has_action_override(...):
            return role                                     # role may be UNBOUND

Two different failures arrived at one handler. If ``resolve_workspace_role``
raised, ``role`` was never bound — and that path was unreachable ONLY because
the override lookup always returned False. Make the lookup work without
restructuring and it either raises ``UnboundLocalError``, or, if somebody gives
``role`` a default to silence that, it hands a NON-MEMBER access on the
strength of an override row.

Membership is now resolved in its own ``try``, before the override path exists.

Mutations that must fail these tests: putting the two checks back in one try,
returning True from the override lookup for a non-member, dropping the await,
and removing the cache invalidation.
"""

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.guards import deps as guards
from pocketpaw_ee.guards.rbac import Forbidden

WS = "ws-overrides"
#: A real entry in both ACTIONS and OVERRIDABLE_ACTIONS that a "member"
#: role is denied — an invented action name raises KeyError from get_rule
#: before any of this logic runs, which would test nothing.
GRANTED = "channel.create"


class _Membership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _User:
    def __init__(self, user_id: str, role: str | None) -> None:
        self.id = user_id
        self.workspaces = [_Membership(WS, role)] if role else []


@pytest.fixture(autouse=True)
def _clear_cache():
    guards._ACTION_OVERRIDE_CACHE.clear()
    yield
    guards._ACTION_OVERRIDE_CACHE.clear()


@pytest.fixture
def overrides(monkeypatch):
    """Stub the service read, recording how many times it is actually awaited."""
    calls: list[tuple[str, str]] = []
    table: dict[str, list[str]] = {}

    async def _get(workspace_id: str, user_id: str) -> list[str]:
        calls.append((workspace_id, user_id))
        return table.get(user_id, [])

    import sys
    import types

    module = types.ModuleType("pocketpaw_ee.cloud.workspace.service")
    module.get_member_action_overrides = _get
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.workspace.service", module)
    return table, calls


async def test_the_lookup_is_actually_awaited(overrides):
    """The reproduction. Before the fix this raised inside a running loop,
    was swallowed, and returned False for every caller forever."""
    table, calls = overrides
    table["u1"] = [GRANTED]

    assert await guards._has_action_override(WS, "u1", GRANTED) is True
    assert calls == [(WS, "u1")], "the service read never happened"


async def test_a_member_whose_role_is_too_low_is_allowed_by_a_grant(overrides):
    table, _ = overrides
    table["u1"] = [GRANTED]

    role = await guards.check_workspace_action(_User("u1", "member"), WS, GRANTED)
    assert role is not None


async def test_a_member_with_no_grant_is_still_denied(overrides):
    with pytest.raises(Forbidden):
        await guards.check_workspace_action(_User("u1", "member"), WS, GRANTED)


async def test_a_non_member_is_denied_even_with_a_grant_row(overrides):
    """The escalation the naive fix opens, asserted directly.

    A user who belongs to NO workspace, carrying an override row for the
    action. Membership is resolved first, so the override is never consulted
    and the denial is workspace.not_member — not an UnboundLocalError, and
    certainly not an allow.
    """
    table, calls = overrides
    table["ghost"] = [GRANTED]

    with pytest.raises(Forbidden) as exc:
        await guards.check_workspace_action(_User("ghost", None), WS, GRANTED)

    assert exc.value.code == "workspace.not_member"
    assert calls == [], (
        "the override lookup ran for a non-member; membership must be settled "
        "before overrides are consulted at all"
    )


async def test_a_lookup_failure_denies_and_is_not_cached(overrides, monkeypatch):
    """A transient read error must not pin a member to 'no overrides'.

    The old cache stored the failure permanently, which is what turned one bad
    read into a process-lifetime denial.
    """
    import sys
    import types

    boom = types.ModuleType("pocketpaw_ee.cloud.workspace.service")

    async def _explode(workspace_id: str, user_id: str) -> list[str]:
        raise RuntimeError("mongo is down")

    boom.get_member_action_overrides = _explode
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.workspace.service", boom)

    assert await guards._has_action_override(WS, "u1", GRANTED) is False
    assert (WS, "u1") not in guards._ACTION_OVERRIDE_CACHE, (
        "a failed read was cached; the next call would short-circuit on it"
    )


async def test_a_grant_is_cached_then_invalidated(overrides):
    """The cache exists, and an admin's change is not stuck behind its TTL."""
    table, calls = overrides
    table["u1"] = [GRANTED]

    assert await guards._has_action_override(WS, "u1", GRANTED) is True
    assert await guards._has_action_override(WS, "u1", GRANTED) is True
    assert len(calls) == 1, "the second call should have been served from cache"

    guards.invalidate_action_overrides(WS, "u1")
    assert await guards._has_action_override(WS, "u1", GRANTED) is True
    assert len(calls) == 2, "invalidation did not force a re-read"


async def test_invalidating_a_whole_workspace_clears_every_member(overrides):
    table, calls = overrides
    table["u1"] = [GRANTED]
    table["u2"] = [GRANTED]

    await guards._has_action_override(WS, "u1", GRANTED)
    await guards._has_action_override(WS, "u2", GRANTED)
    assert len(calls) == 2

    guards.invalidate_action_overrides(WS)
    await guards._has_action_override(WS, "u1", GRANTED)
    await guards._has_action_override(WS, "u2", GRANTED)
    assert len(calls) == 4


def test_the_guard_is_async_so_no_caller_can_silently_drop_the_await():
    """A sync guard returning a coroutine would be truthy and never checked.

    Asserted because the previous signature was sync: if it stayed sync and
    grew an async body, every call site would 'pass' while checking nothing.
    """
    assert asyncio.iscoroutinefunction(guards.check_workspace_action)
    assert asyncio.iscoroutinefunction(guards._has_action_override)


def test_every_call_site_awaits_the_guard():
    """A dropped await on an async guard is a silent auth bypass.

    ``check_workspace_action(...)`` without ``await`` builds a coroutine,
    never runs it, raises nothing, and is discarded. Every route that guards
    with it would allow. Python only warns ("coroutine was never awaited"), on
    stderr, at garbage-collection time — which is exactly how the ORIGINAL bug
    in this module went unnoticed.

    Converting 13 call sites by hand is precisely the change that loses one, so
    this walks the AST of both packages rather than trusting the conversion.
    """
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    unawaited: list[str] = []

    for package in ("src", "ee"):
        for path in (repo / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            awaited = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Await)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and not isinstance(node.func, ast.Attribute)):
                    continue
                if getattr(node.func, "id", None) != "check_workspace_action":
                    continue
                if id(node) not in awaited:
                    unawaited.append(f"{path.relative_to(repo)}:{node.lineno}")

    assert unawaited == [], (
        "check_workspace_action is called without await — the guard builds a "
        "coroutine, never runs, and the route allows:\n  " + "\n  ".join(unawaited)
    )
