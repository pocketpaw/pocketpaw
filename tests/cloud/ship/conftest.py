# tests/cloud/ship/conftest.py — shared wiring for the SHIP-3 HTTP + deploy
# tests: a Fernet key, an arq pool that records instead of dispatching, and a
# fake engine session built on SHIP-1's zero-network ``FakeSSHTransport``.
#
# The engine fake is the important one. It replays the SAME recorded Dokku
# transcripts the SHIP-1 contract suite uses, through the SAME ``DokkuDriver``,
# so an endpoint test exercises the real driver's command surface end to end
# without a box, a network, or a mock of the thing under test. An unmapped
# command fails loudly — if the driver's command surface changes, these tests
# say so.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud.ship import engine as ship_engine
from pocketpaw_ee.ship_engine.dokku import DokkuDriver
from pocketpaw_ee.ship_engine.transcripts import FakeSSHTransport

# The transcripts are recorded against these names — reuse them so the exact
# command strings the driver builds match a recorded reply.
APP = "demo"
IMAGE = "registry.paw.example/demo:9f3c2e1"
DOMAIN = "demo.paw.example"
SERVICE = "demo-db"

# Secrets that live INSIDE the transcripts. No response body, no persisted doc
# and no event payload may ever contain one.
SECRET_MARKERS = ("s3cr3tpass8f2a",)

# Every command the SHIP-3 surface can drive, mapped to its recorded output.
SHIP3_REPLIES: dict[str, str] = {
    f"dokku apps:exists {APP}": "apps_exists_missing.txt",
    f"dokku apps:create {APP}": "apps_create.txt",
    f"dokku git:from-image {APP} {IMAGE}": "git_from_image.txt",
    f"dokku domains:add {APP} {DOMAIN}": "domains_add.txt",
    f"dokku letsencrypt:enable {APP}": "letsencrypt_enable.txt",
    f"dokku mongo:create {SERVICE}": "mongo_create.txt",
    f"dokku mongo:link {SERVICE} {APP}": "mongo_link.txt",
    f"dokku logs {APP} --num 100": "logs.txt",
    ship_engine.BOX_METRICS_COMMAND: "box_metrics.txt",
}

# The same surface, but the image deploy fails.
FAILING_REPLIES: dict[str, str] = {
    **SHIP3_REPLIES,
    f"dokku git:from-image {APP} {IMAGE}": "git_from_image_fail.txt",
}

_KEY_ENV = "CLOUD_ENCRYPTION_KEY"


@pytest.fixture
def enc_key(monkeypatch):
    """A valid Fernet key so the store's at-rest SSH-key encryption works."""
    monkeypatch.setenv(_KEY_ENV, Fernet.generate_key().decode())


class FakePool:
    """Records arq dispatches instead of touching Redis."""

    def __init__(self) -> None:
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, *args, **kwargs):
        self.enqueued.append((args, kwargs))
        return type("Job", (), {"job_id": f"arq-{len(self.enqueued)}"})()


@pytest.fixture
def arq_pool(monkeypatch) -> FakePool:
    """Swap the shared arq pool getter the ship enqueues resolve through."""
    pool = FakePool()

    async def _get_pool():
        return pool

    from pocketpaw_ee.cloud.chat.runs import arq_executor

    monkeypatch.setattr(arq_executor, "_get_pool", _get_pool)
    return pool


def install_fake_engine(monkeypatch, replies: dict[str, str] | None = None) -> list[str]:
    """Point ``engine.box_session`` at a transcript-replaying DokkuDriver.

    Returns the shared list of commands issued, so a test can assert on the
    driver's exact command sequence (and prove no destroy ever ran).
    """
    issued: list[str] = []
    mapped = replies if replies is not None else SHIP3_REPLIES

    @asynccontextmanager
    async def _fake_session(box):  # noqa: ARG001 — the fake ignores the box
        transport = FakeSSHTransport(mapped)
        transport.calls = issued  # share the recorder across sessions
        yield ship_engine.BoxSession(engine=DokkuDriver(transport), transport=transport)

    monkeypatch.setattr(ship_engine, "box_session", _fake_session)
    return issued
