# tests/cloud/runs/test_queue_saturation.py — a chat run never hangs silently
# when every worker slot is busy.
#
# Covers the four seams that decide what a client sees while its run waits:
# the ``queued`` frame written at enqueue, the sweeper's terminal frame for a run
# that never started (the stream may not exist), the friendly error for a busy
# provider, and the jail-quota walk running off the event loop. The worker
# dropping an already-interrupted run is covered by
# ``test_long_run_survives.py::test_worker_does_not_run_a_job_the_sweeper_already_interrupted``.

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.chat.runs import arq_executor, domain, run_core, sweeper, transport
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.arq_executor import ArqExecutor
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

from tests.cloud.runs.test_run_core_session_supervisor import _drive

pytestmark = pytest.mark.asyncio


class _RecordingTransport(InMemoryStreamTransport):
    """In-memory transport that also records every TTL it was asked to set."""

    def __init__(self) -> None:
        super().__init__()
        self.ttls: dict[str, int] = {}

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None:
        self.ttls[run_id] = ttl_seconds


@pytest.fixture
def mem_transport(monkeypatch) -> _RecordingTransport:
    t = _RecordingTransport()
    monkeypatch.setattr(transport, "_transport", t)
    monkeypatch.setattr(sweeper, "get_stream_transport", lambda: t)
    return t


def _spec(run_id: str = "r1") -> RunSpec:
    return RunSpec(
        run_id=run_id,
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{run_id}",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def _events(t: InMemoryStreamTransport, run_id: str) -> list[tuple[str, dict]]:
    return [(ev.event, ev.data) async for ev in t.read_events(run_id, block_ms=10)]


# --- 1. the queued frame -----------------------------------------------------


async def test_enqueue_writes_a_queued_frame_before_the_job(monkeypatch, mem_transport):
    """A run waiting for a slot used to have no stream at all, so its reader got
    only keep-alive pings. The frame lands before the job is enqueued (so no
    worker frame can precede it) and carries a TTL that outlives a full wait
    plus a full run."""
    order: list[str] = []

    class _Pool:
        async def enqueue_job(self, name, payload):  # noqa: ARG002
            order.append("enqueue")
            assert await mem_transport.stream_exists("r1"), "queued frame must come first"

    async def _pool():
        return _Pool()

    monkeypatch.setattr(arq_executor, "_get_pool", _pool)

    await ArqExecutor().submit(_spec())

    assert order == ["enqueue"]
    events = await _events(mem_transport, "r1")
    assert [name for name, _ in events] == ["queued"]
    assert events[0][1]["status"] == "queued"
    assert events[0][1]["message"]
    assert mem_transport.ttls["r1"] == domain.queued_stream_ttl_seconds()
    assert mem_transport.ttls["r1"] >= (
        domain.queued_timeout_minutes() * 60 + domain.run_job_timeout_seconds()
    )


async def test_a_resubmit_never_adds_a_second_queued_frame(monkeypatch, mem_transport):
    """``create_run`` is idempotent and the router submits again on a retried
    send. A ``queued`` frame after the worker's own frames would be a lie."""

    class _Pool:
        async def enqueue_job(self, name, payload):  # noqa: ARG002
            return None

    async def _pool():
        return _Pool()

    monkeypatch.setattr(arq_executor, "_get_pool", _pool)
    await mem_transport.append_event("r1", "stream_start", {"run_id": "r1"})

    await ArqExecutor().submit(_spec())

    assert [name for name, _ in await _events(mem_transport, "r1")] == ["stream_start"]


async def test_a_failed_queued_frame_does_not_block_the_enqueue(monkeypatch):
    class _Broken(InMemoryStreamTransport):
        async def append_event(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(transport, "_transport", _Broken())
    enqueued: list[str] = []

    class _Pool:
        async def enqueue_job(self, name, payload):  # noqa: ARG002
            enqueued.append(name)

    async def _pool():
        return _Pool()

    monkeypatch.setattr(arq_executor, "_get_pool", _pool)

    await ArqExecutor().submit(_spec())

    assert enqueued == ["execute_run_job"]


# --- 2. the sweeper's terminal frame for a never-started run -----------------


async def _backdate(run_id: str, *, minutes: int) -> None:
    await ChatRunDoc.get_pymongo_collection().update_one(
        {"run_id": run_id},
        {"$set": {"createdAt": datetime.now(UTC) - timedelta(minutes=minutes)}},
    )


async def test_sweeping_a_never_started_run_reaches_a_waiting_reader(
    mem_transport,
    runs_app_client,
    mongo_db,  # noqa: ARG001
):
    """The run was queued, no worker ever picked it up, and it has no stream.
    The sweeper used to append only to an existing stream, so the client waited
    about 32 minutes for the reader's own cap. Now a reader already subscribed
    gets a terminal ``interrupted`` frame that says the system was busy."""
    await run_service.create_run(_spec())
    await _backdate("r1", minutes=30)
    assert not await mem_transport.stream_exists("r1")

    reader = asyncio.create_task(runs_app_client.get("/cloud/chat/runs/r1/stream"))
    await asyncio.sleep(0.2)  # the reader is blocked on the missing stream

    assert await sweeper.sweep_stale_runs() == 1

    resp = await asyncio.wait_for(reader, 5)
    assert "event: interrupted" in resp.text
    assert "queue_timeout" in resp.text
    assert "too busy" in resp.text

    doc = await run_service.get_run("r1")
    assert doc.status == "interrupted"
    assert doc.error == sweeper.QUEUE_TIMEOUT_MESSAGE
    assert mem_transport.ttls["r1"] > 0


async def test_a_running_run_without_a_stream_gets_no_frame(
    mem_transport,
    mongo_db,  # noqa: ARG001
):
    """Only the queued arm creates a stream: a dead running run whose key already
    expired keeps the existing rule (the history fallback answers its reader)."""
    await run_service.create_run(_spec())
    await ChatRunDoc.get_pymongo_collection().update_one(
        {"run_id": "r1"},
        {
            "$set": {
                "status": "running",
                "last_heartbeat_at": datetime.now(UTC) - timedelta(hours=1),
            }
        },
    )

    assert await sweeper.sweep_stale_runs() == 1
    assert not await mem_transport.stream_exists("r1")
    assert (await run_service.get_run("r1")).error is None


async def test_the_queued_cutoff_is_env_configurable(
    monkeypatch,
    mem_transport,  # noqa: ARG001
    mongo_db,  # noqa: ARG001
):
    """Queued runs have their own cutoff; running runs keep the 10-minute one."""
    monkeypatch.setenv("POCKETPAW_CLOUD_RUN_QUEUED_TIMEOUT_MINUTES", "2")
    await run_service.create_run(_spec("q"))
    await _backdate("q", minutes=3)
    await run_service.create_run(_spec("run"))
    await _backdate("run", minutes=3)
    await ChatRunDoc.get_pymongo_collection().update_one(
        {"run_id": "run"}, {"$set": {"status": "running"}}
    )

    assert await sweeper.sweep_stale_runs() == 1
    assert (await run_service.get_run("q")).status == "interrupted"
    assert (await run_service.get_run("run")).status == "running"


async def test_queued_cutoff_default_and_bad_values(monkeypatch):
    monkeypatch.delenv("POCKETPAW_CLOUD_RUN_QUEUED_TIMEOUT_MINUTES", raising=False)
    assert domain.queued_timeout_minutes() == 10
    monkeypatch.setenv("POCKETPAW_CLOUD_RUN_QUEUED_TIMEOUT_MINUTES", "nope")
    assert domain.queued_timeout_minutes() == 10
    monkeypatch.setenv("POCKETPAW_CLOUD_RUN_QUEUED_TIMEOUT_MINUTES", "0")
    assert domain.queued_timeout_minutes() == 1


# --- 3. a busy provider gets a friendly error --------------------------------


_LITELLM_401 = (
    "The LiteLLM proxy rejected model 'x' with a 401 from its UPSTREAM provider — "
    "not from your virtual key. Original error: status_code: 401 authentication_error"
)


@pytest.mark.parametrize(
    "raw",
    [
        "Pydantic AI error: status_code: 429, model_name: gpt, body: rate limited",
        "anthropic.APIStatusError: 529 {'type': 'overloaded_error'}",
        "Overloaded",
        "RateLimitError: Rate limit reached for requests",
        "Too Many Requests",
    ],
)
async def test_a_busy_provider_gets_the_friendly_code(monkeypatch, raw):
    """Driven through the real ``_drive_agent_loop``: the frame the client and
    the run doc get is the stable code and plain message, never the raw text.

    Mutation: yield ``{"code": "agent.backend_error", "message": message}`` at
    the error branch again and every case here fails."""
    events = [SimpleNamespace(type="error", content=raw, metadata={})]
    _pool, out = await _drive(monkeypatch, events=events, flag_on=False)

    errors = [data for name, data in out if name == "error"]
    assert errors == [
        {"code": run_core.PROVIDER_BUSY_CODE, "message": run_core.PROVIDER_BUSY_MESSAGE}
    ]


@pytest.mark.parametrize(
    "raw",
    [_LITELLM_401, "codex sdk missing", "context of 14290 tokens exceeds the limit"],
)
async def test_other_backend_errors_pass_through_unchanged(monkeypatch, raw):
    """The LiteLLM 401 rewrite says "401" and "UPSTREAM", which the broad
    failover classifier would read as lane-down; it must reach the user as-is."""
    events = [SimpleNamespace(type="error", content=raw, metadata={})]
    _pool, out = await _drive(monkeypatch, events=events, flag_on=False)

    errors = [data for name, data in out if name == "error"]
    assert errors == [{"code": "agent.backend_error", "message": raw}]


# --- 4. the jail quota walk runs off the event loop --------------------------


async def test_jail_quota_check_runs_off_the_event_loop(monkeypatch):
    """``check_workspace_jail_quota`` is a sync ``os.scandir`` walk, 0.3-3 s on a
    big jail, and it ran on the loop at every run start.

    Mutation: call it directly again and ``ran_on`` is the loop's thread."""
    from pocketpaw_ee.cloud import agent_jail

    ran_on: list[threading.Thread] = []

    def _check(workspace_id):  # noqa: ARG001
        ran_on.append(threading.current_thread())
        return None

    monkeypatch.setattr(agent_jail, "check_workspace_jail_quota", _check)
    ctx = SimpleNamespace(workspace_id="w1")

    rejected = await run_core._reject_if_over_jail_quota(_spec(), ctx, transport=None)

    assert rejected is False
    assert ran_on and ran_on[0] is not threading.current_thread()
