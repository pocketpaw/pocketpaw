import asyncio

import pytest

from pocketpaw.daemon.triggers import TriggerEngine


@pytest.mark.asyncio
async def test_idle_trigger():
    engine = TriggerEngine()

    triggered = False

    async def callback(intention):
        nonlocal triggered
        triggered = True

    engine.start(callback)

    intention = {
        "id": "test_idle",
        "name": "Idle Test",
        "enabled": True,
        "trigger": {
            "type": "idle",
            "idle_minutes": 0.01
        }
    }

    engine.add_intention(intention)

    await asyncio.sleep(1)

    assert triggered
