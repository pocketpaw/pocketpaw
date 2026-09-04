"""Gates on request-log telemetry being batched instead of one insert each.

Request volume and telemetry write volume used to be 1:1, which made
``request_logs`` a strong candidate for the busiest write target in the
database - competing with the traffic it exists to describe, for the same
connection pool and the same cache.

Entries now go onto a bounded queue drained by one consumer into
``insert_many``. The gates below are the three properties that make that safe:
a burst really does become one write, a database that stops keeping up sheds
telemetry rather than growing without bound, and the consumer survives a failed
batch instead of silently taking every later request log with it.

``_LINGER_SECONDS`` is set to 0 throughout so batching is driven by what is
already queued rather than by wall-clock timing. Mutations live in
``tests/mutations/partials.json``.
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import asyncio

import pytest_asyncio
from pocketpaw_ee.cloud._core import request_log as middleware
from pocketpaw_ee.cloud.request_log import service as request_log_service


def _entry(path: str = "/api/v1/thing", status_code: int = 200) -> dict:
    return {
        "method": "GET",
        "path": path,
        "status_code": status_code,
        "duration_ms": 1.25,
        "actor_id": "u1",
        "workspace_id": "ws1",
        "is_error": status_code >= 400,
        "user_agent": "pytest",
        "ip": "127.0.0.1",
    }


@pytest_asyncio.fixture
async def batches(monkeypatch):
    """Capture what reaches the write layer, with the linger disabled."""
    middleware._reset_for_tests()
    monkeypatch.setattr(middleware, "_LINGER_SECONDS", 0.0)
    seen: list[list[dict]] = []

    async def _record_many(entries):  # noqa: ANN001
        seen.append(list(entries))
        return len(entries)

    monkeypatch.setattr(request_log_service, "record_many", _record_many)
    yield seen
    await _teardown()


async def _settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def _teardown() -> None:
    """Reset, then give the cancelled consumer a tick to actually finish.

    Without the tick the task is still pending when the loop closes and
    CPython raises "Event loop is closed" out of its finalizer, which surfaces
    as an unraisable-exception warning attached to whichever test runs next.
    """
    middleware._reset_for_tests()
    await asyncio.sleep(0)


async def test_a_burst_of_requests_becomes_one_write(batches):
    """Mutation: write each entry on its own instead of collecting a batch.

    Nothing awaits between the five calls, so all five are queued before the
    consumer gets a turn. That is the shape of a real burst on one event loop,
    and it must cost one round trip.
    """
    for index in range(5):
        middleware._log_request(**_entry(path=f"/p/{index}"))

    await _settle()

    assert len(batches) == 1
    assert len(batches[0]) == 5
    assert [e["path"] for e in batches[0]] == [f"/p/{i}" for i in range(5)]


async def test_a_batch_is_bounded(batches, monkeypatch):
    """Mutation: drop the _BATCH_MAX ceiling from the collect loop.

    One unbounded ``insert_many`` of a whole backlog is its own stall. The
    backlog should be drained in several bounded writes.
    """
    monkeypatch.setattr(middleware, "_BATCH_MAX", 3)
    for index in range(7):
        middleware._log_request(**_entry(path=f"/p/{index}"))

    await _settle(20)

    assert [len(b) for b in batches] == [3, 3, 1]


async def test_the_entry_carries_the_fields_the_document_needs(batches):
    """The middleware's kwargs and the document's fields must line up.

    ``workspace_id`` becomes ``workspace``, and the duration is rounded here
    rather than at the write. Getting either wrong makes every insert fail at
    a layer that swallows its own errors.
    """
    middleware._log_request(**_entry())
    await _settle()

    entry = batches[0][0]
    assert entry["workspace"] == "ws1"
    assert "workspace_id" not in entry
    assert entry["duration_ms"] == 1.2
    assert set(entry) == {
        "workspace",
        "actor_id",
        "method",
        "path",
        "status_code",
        "duration_ms",
        "is_error",
        "ip",
        "user_agent",
    }


async def test_a_full_queue_sheds_telemetry_instead_of_growing(batches, monkeypatch):
    """Mutation: drop the ceiling and let the queue grow.

    An unbounded backlog is how a Mongo stall becomes an OOM. Nothing awaits
    in this loop, so the consumer never runs and the queue really does fill.
    """
    monkeypatch.setattr(middleware, "_QUEUE_MAX", 4)
    middleware._reset_for_tests()

    for index in range(30):
        middleware._log_request(**_entry(path=f"/p/{index}"))

    assert middleware._dropped == 26
    assert middleware._queue is not None
    assert middleware._queue.qsize() == 4


async def test_shedding_telemetry_never_raises_into_the_request(monkeypatch):
    """A dropped log must not become a 500 on a request that already worked."""
    middleware._reset_for_tests()
    monkeypatch.setattr(middleware, "_QUEUE_MAX", 1)
    middleware._log_request(**_entry())
    middleware._log_request(**_entry())  # would raise QueueFull if unguarded
    await _teardown()


async def test_the_consumer_survives_a_failed_batch(monkeypatch):
    """Mutation: let the write exception escape the drain loop.

    Asserted on the TASK, not on the second batch landing. The second batch
    lands either way, because a dead consumer is replaced on the next request
    (the test below covers that). What a dying consumer actually costs is the
    backlog it was holding, which no later write reveals - so the identity of
    the task is the only thing that separates "survived" from "was replaced".
    """
    middleware._reset_for_tests()
    monkeypatch.setattr(middleware, "_LINGER_SECONDS", 0.0)
    seen: list[list[dict]] = []

    async def _flaky(entries):  # noqa: ANN001
        seen.append(list(entries))
        if len(seen) == 1:
            raise RuntimeError("mongo said no")
        return len(entries)

    monkeypatch.setattr(request_log_service, "record_many", _flaky)

    middleware._log_request(**_entry(path="/first"))
    consumer = middleware._drain_task
    await _settle()

    assert consumer is not None and not consumer.done(), "the consumer died on a failed batch"

    middleware._log_request(**_entry(path="/second"))
    await _settle()

    assert middleware._drain_task is consumer
    assert [b[0]["path"] for b in seen] == ["/first", "/second"]
    await _teardown()


async def test_a_consumer_that_does_die_is_replaced(monkeypatch):
    """Mutation: keep a finished consumer instead of rebuilding.

    The belt to the previous test's braces. If something does kill the
    consumer, the next request must get a live one rather than queueing into
    a backlog nobody drains - which would look exactly like telemetry quietly
    switching itself off.
    """
    middleware._reset_for_tests()
    monkeypatch.setattr(middleware, "_LINGER_SECONDS", 0.0)
    seen: list[list[dict]] = []

    async def _record_many(entries):  # noqa: ANN001
        seen.append(list(entries))
        return len(entries)

    monkeypatch.setattr(request_log_service, "record_many", _record_many)

    middleware._log_request(**_entry(path="/first"))
    await _settle()
    dead = middleware._drain_task
    assert dead is not None
    dead.cancel()
    await _settle()
    assert dead.done()

    middleware._log_request(**_entry(path="/second"))
    await _settle()

    assert middleware._drain_task is not dead
    assert [b[0]["path"] for b in seen] == ["/first", "/second"]
    await _teardown()


async def test_shutdown_flushes_the_queued_tail(monkeypatch):
    """Mutation: return from shutdown without draining the queue.

    A restart is exactly when someone is reading /audit, and the queued tail
    is otherwise lost on every deploy.
    """
    middleware._reset_for_tests()
    monkeypatch.setattr(middleware, "_LINGER_SECONDS", 0.0)
    written: list[list[dict]] = []

    async def _record_many(entries):  # noqa: ANN001
        written.append(list(entries))
        return len(entries)

    monkeypatch.setattr(request_log_service, "record_many", _record_many)

    for index in range(3):
        middleware._log_request(**_entry(path=f"/p/{index}"))
    # No await: the consumer has not run, so everything is still queued.
    count = await middleware.shutdown_request_log()

    assert count == 3
    assert [e["path"] for e in written[0]] == ["/p/0", "/p/1", "/p/2"]


async def test_shutdown_stops_the_consumer(monkeypatch):
    """After a flush, nothing is left running or holding a queue."""
    middleware._reset_for_tests()
    monkeypatch.setattr(middleware, "_LINGER_SECONDS", 0.0)
    middleware._log_request(**_entry())
    task = middleware._drain_task
    await middleware.shutdown_request_log()

    assert task is not None and task.done()
    assert middleware._queue is None
    assert middleware._drain_task is None


def test_logging_with_no_running_loop_is_a_no_op():
    """Mutation: drop the no-running-loop guard.

    Sync test harnesses and interpreter shutdown both reach this with no loop.
    Telemetry about a request that has already been answered is never worth
    raising over, so it has to return rather than throw.
    """
    middleware._reset_for_tests()
    middleware._log_request(**_entry())
    assert middleware._queue is None


async def test_the_consumer_is_rebuilt_when_the_loop_changes(monkeypatch):
    """Mutation: keep the queue across loops (drop the loop identity check).

    A queue belongs to the loop whose futures it holds. Handing it entries
    from another loop is not recoverable, and in a test session that means one
    module's torn-down loop swallowing the next module's telemetry.
    """
    middleware._reset_for_tests()
    real_loop = asyncio.get_running_loop()
    first = middleware._ensure_consumer()
    assert first is not None

    class _OtherLoop:
        """A different loop object, so the identity check has to notice."""

        def create_task(self, coro):  # noqa: ANN001
            coro.close()
            return real_loop.create_future()

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _OtherLoop())
    second = middleware._ensure_consumer()
    monkeypatch.undo()

    assert second is not first
    await _teardown()


# ---------------------------------------------------------------------------
# The write layer itself, against a real collection.
# ---------------------------------------------------------------------------


async def test_record_many_writes_every_entry(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.models.request_log import RequestLog

    entries = [
        {k: v for k, v in _entry(path=f"/p/{i}").items() if k != "workspace_id"}
        | {"workspace": "ws1"}
        for i in range(4)
    ]

    written = await request_log_service.record_many(entries)

    assert written == 4
    assert await RequestLog.find({"workspace": "ws1"}).count() == 4


async def test_one_bad_entry_does_not_cost_the_batch(mongo_db):  # noqa: ARG001
    """Mutation: build the documents in one comprehension with no guard.

    Telemetry describing requests that have already been served must not be
    discarded wholesale because one row would not build.
    """
    from pocketpaw_ee.cloud.models.request_log import RequestLog

    good = {k: v for k, v in _entry().items() if k != "workspace_id"} | {"workspace": "ws1"}
    bad = {"workspace": "ws1"}  # missing every required field

    written = await request_log_service.record_many([good, bad, dict(good)])

    assert written == 2
    assert await RequestLog.find({"workspace": "ws1"}).count() == 2


async def test_an_empty_batch_writes_nothing(mongo_db):  # noqa: ARG001
    assert await request_log_service.record_many([]) == 0
