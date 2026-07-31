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
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.ship import engine as ship_engine
from pocketpaw_ee.cloud.ship import store
from pocketpaw_ee.cloud.ship.router import router as ship_router
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
    # Wave 2 (SHIP-17): postgres/redis database plugins, zero-downtime checks,
    # and process scaling — the same box-free transcript replay.
    f"dokku postgres:create {SERVICE}": "postgres_create.txt",
    f"dokku postgres:link {SERVICE} {APP}": "postgres_link.txt",
    f"dokku redis:create {SERVICE}": "redis_create.txt",
    f"dokku redis:link {SERVICE} {APP}": "redis_link.txt",
    f"dokku checks:enable {APP}": "checks_enable.txt",
    f"dokku checks:disable {APP}": "checks_disable.txt",
    f"dokku ps:scale {APP} web=2 worker=1": "ps_scale.txt",
    # Wave 3 (SHIP-18): resource limits, persistent volumes, lifecycle bounces.
    f"dokku resource:limit --cpu 1000 --memory 512 {APP}": "resource_limit.txt",
    f"dokku storage:create {APP}-data": "storage_create.txt",
    f"dokku storage:mount {APP} {APP}-data --container-dir /data": "storage_mount.txt",
    f"dokku ps:restart {APP}": "ps_restart.txt",
    f"dokku ps:rebuild {APP}": "ps_rebuild.txt",
    # The APPROVED-teardown + approved-deploy paths (fix/ship-review-p0). These
    # were never mapped because tests/cloud/ship/test_instinct_gate.py stubs
    # ``_run_verb`` out, so the executor's real verb bodies never ran under test.
    f"dokku --force apps:destroy {APP}": "apps_destroy.txt",
    (f"dokku config:set --no-restart {APP} API_KEY=hunter2-super-secret-value"): "config_set.txt",
    f"dokku logs {APP} --num 100": "logs.txt",
    ship_engine.BOX_METRICS_COMMAND: "box_metrics.txt",
    # App-level metrics (SHIP-12): ps:report state + df + real docker stats.
    f"dokku ps:report {APP}": "ps_report.txt",
    "df -Pk /": "df_root.txt",
    (
        f"docker stats --no-stream --no-trunc "
        f"--format '{{{{.CPUPerc}}}} {{{{.MemPerc}}}}' --filter name={APP}."
    ): "docker_stats.txt",
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


def install_refused_engine(monkeypatch) -> None:
    """Point ``engine.box_session`` at a box that refuses the SSH connection.

    The message deliberately carries the box's address so a test can prove the
    recorded summary does not echo it back.
    """

    @asynccontextmanager
    async def _refused(box):  # noqa: ARG001 — the fake ignores the box
        raise ConnectionRefusedError("[Errno 61] connect to 203.0.113.9 port 22")
        yield  # pragma: no cover — never reached; keeps this an async generator

    monkeypatch.setattr(ship_engine, "box_session", _refused)


# ---------------------------------------------------------------------------
# The HTTP client fixtures + box/app helpers. They live here (not in a test
# module) so every /ship suite — the router tests and the Instinct-gate tests —
# gets them by pytest discovery instead of importing fixtures across modules.
# ---------------------------------------------------------------------------


def _build_app(workspace_id: str) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(ship_router)

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def w1(mongo_db, enc_key, arq_pool) -> AsyncClient:  # noqa: ARG001 — fixtures init state
    transport = ASGITransport(app=_build_app("w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db, enc_key, arq_pool) -> AsyncClient:  # noqa: ARG001 — fixtures init state
    transport = ASGITransport(app=_build_app("w2"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _ready_box(client: AsyncClient) -> str:
    """Provision a box through the API and flip it ``ready`` (the arq job's job)."""
    resp = await client.post("/ship/boxes", json={"provider": "hcloud"})
    assert resp.status_code == 200, resp.text
    box_id = resp.json()["id"]
    box = await store.get_box("w1", box_id)
    assert box is not None
    await store.mark_ready(box, server_id="srv-1", ip="203.0.113.9", price_monthly=8.25)
    return box_id


async def _app_on_box(client: AsyncClient, box_id: str) -> str:
    resp = await client.post("/ship/apps", json={"name": APP, "box_id": box_id, "image": IMAGE})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]
