# tests/test_agent_pool_image_attachments.py — the files a USER attached
# reaching (and not reaching) the backend through ``AgentPool.run``.
#
# Created 2026-09-15 (feat/chat-image-wiring). New file.
#
# The sibling of ``test_agent_pool_turn_images.py``, for the second picture
# channel. The two are not the same thing and must not be collapsed: ``images``
# is a snapshot the SURFACE chose to show, replaced every turn;
# ``image_attachments`` is a file the USER deliberately attached, and it is an
# ``ImageAttachment`` rather than a bare ``(bytes, media_type)`` pair.
#
# The forward is gated on the backend's SIGNATURE, and that is the whole point
# of this file. Only the two SDK backends translate an attachment into a shape a
# model can see; the other seven take a narrow ``run`` with no ``**kwargs``, so
# an unguarded forward is a TypeError on the first turn anyone attaches a photo
# to. Withhold-when-empty does NOT cover this: it narrows WHEN the kwarg rides,
# never WHERE. An attachment is rarer than an Otherhand snapshot, but rarity is
# not a guard — it only means the crash waits for the first user who attaches
# something.
#
# ``**kwargs`` deliberately does NOT count as declaring the parameter, matching
# every other signature guard in ``agents/backend.py``.

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.backend import ImageAttachment
from pocketpaw.agents.pool import AgentPool

pytestmark = pytest.mark.asyncio

IMG = ImageAttachment(
    data=b"\x89PNG\r\n\x1a\n" + b"logo" * 8,
    media_type="image/png",
    filename="logo.png",
)


class _SeeingBackend:
    """A backend that DECLARES ``image_attachments``, as both SDK backends do."""

    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    async def run(self, message: str, *, image_attachments=(), **kwargs) -> AsyncIterator[object]:
        self.last_kwargs = {"image_attachments": image_attachments, **kwargs}
        return
        yield  # pragma: no cover — makes this an async generator


class _BlindBackend:
    """A backend with the narrow signature, as the other seven have. No
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
    async for _ in pool.run("a1", "what is in this picture?", "session:s1", **run_kwargs):
        pass
    return backend


async def test_a_seeing_backend_gets_the_attachment(monkeypatch) -> None:
    backend = await _drive(monkeypatch, _SeeingBackend(), image_attachments=(IMG,))
    assert backend.last_kwargs is not None
    assert backend.last_kwargs["image_attachments"] == (IMG,)


async def test_a_turn_with_no_attachment_forwards_nothing(monkeypatch) -> None:
    # Withhold-when-empty: a turn that came without an attachment keeps the call
    # it has always made, so this cannot move a single existing turn.
    backend = await _drive(monkeypatch, _SeeingBackend())
    assert backend.last_kwargs is not None
    assert backend.last_kwargs["image_attachments"] == ()


async def test_a_blind_backend_is_never_handed_one(monkeypatch) -> None:
    # The regression this file exists for, and the one the original wiring
    # shipped: without the signature gate this raises TypeError on the first
    # turn a user attaches a photo to, on any of the seven narrow backends.
    backend = await _drive(monkeypatch, _BlindBackend(), image_attachments=(IMG,))
    assert backend.called, "the turn must still run — just without the picture"


async def test_the_two_picture_channels_are_forwarded_independently(monkeypatch) -> None:
    # A backend that declares one and not the other gets exactly the one it
    # declares. Collapsing the two guards into a single question would hand a
    # snapshot-only backend an attachment it never mentioned.
    class _SnapshotOnly:
        def __init__(self) -> None:
            self.last_kwargs: dict | None = None

        async def run(self, message: str, *, images=(), **kwargs) -> AsyncIterator[object]:
            self.last_kwargs = {"images": images, **kwargs}
            return
            yield  # pragma: no cover

    backend = await _drive(
        monkeypatch,
        _SnapshotOnly(),
        images=((IMG.data, IMG.media_type),),
        image_attachments=(IMG,),
    )
    assert backend.last_kwargs is not None
    assert backend.last_kwargs["images"] == ((IMG.data, IMG.media_type),)
    assert "image_attachments" not in backend.last_kwargs
