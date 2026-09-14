# tests/test_agent_pool_turn_images.py — pictures reaching (and not reaching)
# the backend through ``AgentPool.run``.
#
# Created 2026-09-09 (feat/other-hand-vision).
#
# The forward is gated on the backend's SIGNATURE, not on truthiness alone, and
# that is the whole point of this file. Seven of the eight backends take a
# narrow ``run`` with no ``**kwargs``; the surface that sends images sends them
# on EVERY turn; and the one that would break is ``claude_agent_sdk``, the
# self-hosted default. An unconditional forward is not a rare edge there, it is
# every Otherhand turn.
#
# ``**kwargs`` deliberately does NOT count as declaring the parameter. That
# matches ``_accepts_prompt_digest_kwarg``, and pool.py's own header records
# what happens when one copy of this question starts counting ``**kwargs``.

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.pool import AgentPool

pytestmark = pytest.mark.asyncio

PNG = (b"\x89PNG\r\n\x1a\n" + b"page" * 8, "image/png")


class _SeeingBackend:
    """A backend that DECLARES ``images``, as pydantic_ai does."""

    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    async def run(self, message: str, *, images=(), **kwargs) -> AsyncIterator[object]:
        self.last_kwargs = {"images": images, **kwargs}
        return
        yield  # pragma: no cover — makes this an async generator


class _BlindBackend:
    """A backend with the narrow signature, as the Claude SDK has. No
    ``**kwargs``, so an unwanted forward is a TypeError rather than a no-op."""

    def __init__(self) -> None:
        self.called = False

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[object]:
        self.called = True
        return
        yield  # pragma: no cover


async def _drive(monkeypatch, backend, **run_kwargs):
    inst = SimpleNamespace(
        backend=backend,
        soul_manager=None,
        config={"soul_persona": "P", "system_prompt": ""},
        last_active=datetime.now(UTC),
        active_runs=0,
    )
    pool = AgentPool()

    async def _fake_get(agent_id):
        return inst

    monkeypatch.setattr(pool, "get", _fake_get)
    async for _ in pool.run("a1", "what did I write?", "session:s1", **run_kwargs):
        pass
    return backend


async def test_a_seeing_backend_gets_the_pictures(monkeypatch) -> None:
    backend = await _drive(monkeypatch, _SeeingBackend(), images=(PNG,))
    assert backend.last_kwargs is not None
    assert backend.last_kwargs["images"] == (PNG,)


async def test_a_turn_with_no_pictures_forwards_nothing(monkeypatch) -> None:
    # Withhold-when-empty: every surface that sends none keeps the call it has
    # always made, so this change cannot move a single existing turn.
    backend = await _drive(monkeypatch, _SeeingBackend())
    assert backend.last_kwargs is not None
    assert backend.last_kwargs["images"] == ()


async def test_a_blind_backend_is_never_handed_one(monkeypatch) -> None:
    # The regression this file exists for. Without the signature gate this
    # raises TypeError, which on a self-hosted install is every Otherhand turn.
    backend = await _drive(monkeypatch, _BlindBackend(), images=(PNG,))
    assert backend.called, "the turn must still run — just without the picture"
