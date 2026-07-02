# tests/ee/game/conftest.py — shared fixtures for the game runtime + router
# tests. Created: 2026-07-02 (feat/game-surface, PE-A).
#
# Two autouse fixtures, both mirroring the sites test tree:
#   * ``_recording_bus_for_game`` — seed_example persists through
#     ``agent_create``, which ends with ``emit(PocketCreated)``; ``emit()``
#     asserts a bus is initialised (the "forgot init_realtime" guard), so a
#     recording bus is installed for the whole game tree (same shim as
#     tests/ee/sites/conftest.py).
#   * ``_fresh_world_registry`` — the runtime's world registry is
#     process-global (worlds die with the process, v0); reset it around every
#     test so a world started in one test can never leak a handle into the
#     next.
#
# NOTE on skips: ``pocketpaw_ee`` is required module-wide, but the
# soul-protocol GAME PROFILE is NOT — soul-protocol is a base dep of the OSS
# core, so a published (profile-less) install is the NORMAL CI condition. The
# 503 + seed_example tests must run there; only the live-world tests skip
# (see ``requires_game_profile`` in the test modules).

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _recording_bus_for_game():
    """Install a RecordingBus for every game test (mirrors the sites tree)."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _fresh_world_registry():
    """Empty the in-memory world registry around every test."""
    from pocketpaw_ee.game import runtime

    runtime.reset_worlds()
    yield
    runtime.reset_worlds()
