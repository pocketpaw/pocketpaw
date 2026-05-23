# Resumable Chat Runs Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make PocketPaw cloud agent chat survive page refresh and session switching mid-stream by decoupling the agent run from the HTTP request and streaming its events through a resumable Redis Stream.

**Architecture:** An agent turn becomes a first-class `Run` object. The web process persists the user message, creates a `chat_runs` document, and hands the run to a `RunExecutor`; it never runs the agent inline. The executor writes every agent event into a Redis Stream (`run:{id}:events`). A new `GET /cloud/chat/runs/{id}/stream` endpoint reads that stream — used identically for first view and for resume-after-reconnect. Tier 1 runs the agent in an in-process `asyncio` task; Tier 2 runs it in a separate `arq` worker service.

**Tech Stack:** Python 3.11, FastAPI, Beanie/MongoDB, `redis[hiredis]` (already a dependency), `arq` (added in PR2), `pytest`/`pytest-asyncio`, `fakeredis` (test-only). Frontend: SvelteKit 2, Svelte 5 runes, Bun.

**Design reference:** `docs/plans/2026-05-22-resumable-chat-runs-design.md` — read it first.

---

## Conventions & ground rules

- All backend paths are under `backend/`. Run backend commands from `backend/`.
- Cloud code obeys the **4-file entity rule** (see `backend/CLAUDE.md` → "pocketpaw_ee/cloud Code Rules"). The new entity is `ee/pocketpaw_ee/cloud/chat/runs/` with `domain.py`, `dto.py`, `service.py`, `router.py` plus the non-entity helpers `executor.py`, `redis_stream.py`, `redis_client.py`, and (PR2) `worker.py`.
- Only `runs/service.py` may import the `ChatRunDoc` Beanie document.
- Errors use `pocketpaw_ee.cloud._core.errors` (`NotFound`, `ValidationError`, etc.). Never `raise HTTPException` in services/routers.
- Every Mongo read includes a `workspace` tenant filter.
- Tests that touch `tests/cloud` need MongoDB; this plan adds `fakeredis` for unit tests so Redis is not required for the unit layer (see `reference_backend_test_env`).
- TDD: write the failing test, watch it fail, implement the minimum, watch it pass, commit.
- Commit after every task. Branch: `feat/resumable-chat-runs-tier1` for PR1, `feat/resumable-chat-runs-tier2` for PR2 (stacked on tier1).
- Commit messages use a `Co-Authored-By` trailer; **no AI-attribution footer in PR bodies** (see `feedback_no_ai_attribution`).

---

## Pre-flight

**Step 1:** Confirm the backend installs with the EE group.

Run: `cd backend && uv sync --dev --group ee`
Expected: completes; `pocketpaw-ee` installed editable.

**Step 2:** Add the test-only dependency.

Modify `backend/ee/pyproject.toml` — find the dev dependency group (the `[dependency-groups]` `dev` list or `[tool.uv]` dev section used by the EE package) and add `"fakeredis>=2.21.0"`. If the EE package has no dev group, add it to the root `backend/pyproject.toml` dev group instead.

Run: `cd backend && uv sync --dev --group ee`
Expected: `fakeredis` resolves and installs.

**Step 3:** Create the PR1 branch.

```bash
cd backend
git checkout -b feat/resumable-chat-runs-tier1
```

---

# PR 1 — Tier 1 (resumable streaming, in-process executor)

## Task 1: Redis client singleton

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/_core/redis_client.py`
- Test: `backend/tests/cloud/runs/test_redis_client.py`

**Step 1: Write the failing test**

```python
# backend/tests/cloud/runs/test_redis_client.py
import pytest

from pocketpaw_ee.cloud._core import redis_client


@pytest.mark.asyncio
async def test_get_redis_returns_singleton(monkeypatch):
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    redis_client._reset_for_tests()
    a = redis_client.get_redis()
    b = redis_client.get_redis()
    assert a is b  # same client instance reused


def test_get_redis_without_url_raises(monkeypatch):
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    redis_client._reset_for_tests()
    with pytest.raises(RuntimeError, match="POCKETPAW_REDIS_URL"):
        redis_client.get_redis()
```

Add an empty `backend/tests/cloud/runs/__init__.py`.

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_redis_client.py -v`
Expected: FAIL — `ModuleNotFoundError: pocketpaw_ee.cloud._core.redis_client`.

**Step 3: Write minimal implementation**

```python
# backend/ee/pocketpaw_ee/cloud/_core/redis_client.py
"""Process-wide Redis client singleton for cloud chat runs.

A future RedisBus (realtime) may share this. The connection URL comes from
``POCKETPAW_REDIS_URL`` (e.g. ``redis://redis:6379/0``).
"""

from __future__ import annotations

import os

from redis.asyncio import Redis

_client: Redis | None = None


def get_redis() -> Redis:
    """Return the shared async Redis client, creating it on first use."""
    global _client
    if _client is None:
        url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError(
                "POCKETPAW_REDIS_URL is not set — resumable chat runs need Redis."
            )
        # decode_responses=False: Redis Stream entry IDs and our JSON payloads
        # are handled as bytes/str explicitly in redis_stream.py.
        _client = Redis.from_url(url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _reset_for_tests() -> None:
    """Drop the cached client so a test can re-create it. Test-only."""
    global _client
    _client = None
```

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_redis_client.py -v`
Expected: PASS (both tests).

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/_core/redis_client.py tests/cloud/runs/
git commit -m "feat(runs): add Redis client singleton

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `ChatRunDoc` Beanie model

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/models/chat_run.py`
- Modify: `backend/ee/pocketpaw_ee/cloud/models/__init__.py` (add to `ALL_DOCUMENTS`)
- Test: `backend/tests/cloud/runs/test_chat_run_model.py`

**Step 1: Inspect the models package**

Run: `cd backend && uv run python -c "from pocketpaw_ee.cloud.models import ALL_DOCUMENTS; print(len(ALL_DOCUMENTS))"`
Expected: prints an integer. Open `ee/pocketpaw_ee/cloud/models/__init__.py` and an existing model (e.g. `models/pocket.py`) to copy the `Document` subclass style (`Settings.name`, indexes, `createdAt` field naming).

**Step 2: Write the failing test**

```python
# backend/tests/cloud/runs/test_chat_run_model.py
from pocketpaw_ee.cloud.models import ALL_DOCUMENTS
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc


def test_chat_run_doc_registered():
    assert ChatRunDoc in ALL_DOCUMENTS


def test_chat_run_doc_defaults():
    doc = ChatRunDoc(
        run_id="r1",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
    )
    assert doc.status == "queued"
    assert doc.partial_text == ""
    assert doc.assistant_message_id is None
```

**Step 3: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_chat_run_model.py -v`
Expected: FAIL — `ModuleNotFoundError: pocketpaw_ee.cloud.models.chat_run`.

**Step 4: Write minimal implementation**

```python
# backend/ee/pocketpaw_ee/cloud/models/chat_run.py
"""Beanie document for a chat agent run — one assistant turn.

A run outlives the HTTP request that created it. Status transitions:
``queued -> running -> completed`` or terminal ``interrupted``/``failed``/``cancelled``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

import pymongo
from beanie import Document
from pydantic import Field

RunStatus = Literal["queued", "running", "completed", "interrupted", "failed", "cancelled"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatRunDoc(Document):
    run_id: str
    workspace: str
    context_type: str          # "dm" | "group" | "pocket" | "session"
    scope_id: str
    session_key: str
    group: str | None = None
    user_id: str
    agent_id: str
    client_message_id: str
    user_message_id: str
    assistant_message_id: str | None = None
    status: RunStatus = "queued"
    partial_text: str = ""
    error: str | None = None
    createdAt: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    class Settings:
        name = "chat_runs"
        indexes = [
            [("run_id", pymongo.ASCENDING)],
            # Fast lookup of the newest non-terminal run for a scope.
            [
                ("workspace", pymongo.ASCENDING),
                ("context_type", pymongo.ASCENDING),
                ("scope_id", pymongo.ASCENDING),
                ("createdAt", pymongo.DESCENDING),
            ],
        ]
```

Then add to `ee/pocketpaw_ee/cloud/models/__init__.py`: import `ChatRunDoc` and append it to `ALL_DOCUMENTS` (match the existing import + list style in that file).

**Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_chat_run_model.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
git add ee/pocketpaw_ee/cloud/models/chat_run.py ee/pocketpaw_ee/cloud/models/__init__.py tests/cloud/runs/test_chat_run_model.py
git commit -m "feat(runs): add ChatRunDoc model

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Run domain value objects

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/__init__.py` (empty)
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/domain.py`
- Test: `backend/tests/cloud/runs/test_run_domain.py`

**Step 1: Write the failing test**

```python
# backend/tests/cloud/runs/test_run_domain.py
import pytest

from pocketpaw_ee.cloud.chat.runs.domain import RunSpec


def test_run_spec_roundtrips_json():
    spec = RunSpec(
        run_id="r1", workspace_id="w1", context_type="session", scope_id="s1",
        session_key="session:s1", group=None, user_id="u1", agent_id="a1",
        client_message_id="c1", user_message_id="m1", content="hello",
        history=[{"role": "user", "content": "hi"}], intent=None,
    )
    restored = RunSpec.model_validate(spec.model_dump())
    assert restored == spec


def test_run_spec_requires_tenancy():
    with pytest.raises(Exception):
        RunSpec(run_id="r1")  # missing workspace_id etc.
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_domain.py -v`
Expected: FAIL — module not found.

**Step 3: Write minimal implementation**

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/domain.py
"""Value objects for chat runs. RunSpec is the JSON-serializable payload
handed to a RunExecutor — it must survive an arq pickle round-trip, so it
holds only primitives."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RunSpec(BaseModel):
    """Everything execute_run() needs, decoupled from the HTTP request."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    workspace_id: str
    context_type: str
    scope_id: str
    session_key: str
    group: str | None
    user_id: str
    agent_id: str
    client_message_id: str
    user_message_id: str
    content: str
    history: list[dict[str, str]]
    intent: str | None
    attachments: list[dict[str, Any]] = []
    mentions: list[str] = []
    reply_to: str | None = None
```

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_domain.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/__init__.py ee/pocketpaw_ee/cloud/chat/runs/domain.py tests/cloud/runs/test_run_domain.py
git commit -m "feat(runs): add RunSpec domain value object

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `RunStreamTransport` Protocol + `RedisStreamTransport` impl

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/transport.py` (Protocol + `StreamEvent` + `get_stream_transport()` factory)
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/redis_stream.py` (the `RedisStreamTransport` class)
- Test: `backend/tests/cloud/runs/test_redis_stream.py`

This is the adapter seam — every other module talks to the transport through the Protocol, never to Redis directly. Dragonfly/Valkey just point `POCKETPAW_REDIS_URL` at the new server; non-Redis backends write a new class implementing the Protocol.

**Step 1: Write the failing test**

```python
# backend/tests/cloud/runs/test_redis_stream.py
import fakeredis.aioredis
import pytest

from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.chat.runs.transport import RunStreamTransport, StreamEvent


@pytest.fixture
def transport() -> RedisStreamTransport:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisStreamTransport(redis)


def test_redis_impl_satisfies_protocol(transport):
    assert isinstance(transport, RunStreamTransport)  # runtime_checkable


@pytest.mark.asyncio
async def test_append_then_read_replays_all(transport):
    await transport.append_event("r1", "chunk", {"content": "a"})
    await transport.append_event("r1", "chunk", {"content": "b"})
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [(e.event, e.data["content"]) for e in events] == [("chunk", "a"), ("chunk", "b")]


@pytest.mark.asyncio
async def test_read_after_cursor_skips_seen(transport):
    id1 = await transport.append_event("r1", "chunk", {"content": "a"})
    await transport.append_event("r1", "chunk", {"content": "b"})
    events = [e async for e in transport.read_events("r1", after=id1, block_ms=10)]
    assert [e.data["content"] for e in events] == ["b"]


@pytest.mark.asyncio
async def test_read_stops_on_terminal_event(transport):
    await transport.append_event("r1", "chunk", {"content": "a"})
    await transport.append_event("r1", "stream_end", {"assistant_message_id": "m1"})
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "stream_end"
    assert events[-1].is_terminal


@pytest.mark.asyncio
async def test_cancel_flag(transport):
    assert await transport.is_cancelled("r1") is False
    await transport.request_cancel("r1")
    assert await transport.is_cancelled("r1") is True
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_redis_stream.py -v`
Expected: FAIL — modules not found.

**Step 3: Define the Protocol + `StreamEvent`**

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/transport.py
"""Transport abstraction for chat-run events.

Every module that needs to write or read run events depends on this Protocol,
not on Redis directly. The default impl is RedisStreamTransport (which works
with Redis, Dragonfly, or Valkey — same wire protocol). A non-Redis backend
(NATS JetStream, Kafka, ...) implements the Protocol and is selected via
POCKETPAW_CLOUD_STREAM_TRANSPORT.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

TERMINAL_EVENTS = {"stream_end", "error", "interrupted"}


@dataclass(frozen=True)
class StreamEvent:
    entry_id: str            # opaque cursor — Redis entry id, JetStream seq, ...
    event: str
    data: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.event in TERMINAL_EVENTS


@runtime_checkable
class RunStreamTransport(Protocol):
    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str: ...

    def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]: ...

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None: ...
    async def request_cancel(self, run_id: str) -> None: ...
    async def is_cancelled(self, run_id: str) -> bool: ...
    async def stream_exists(self, run_id: str) -> bool: ...


_transport: RunStreamTransport | None = None


def get_stream_transport() -> RunStreamTransport:
    """Return the process-wide transport singleton, selecting the impl by env."""
    global _transport
    if _transport is None:
        backend = os.environ.get("POCKETPAW_CLOUD_STREAM_TRANSPORT", "redis").lower()
        if backend == "redis":
            from pocketpaw_ee.cloud._core.redis_client import get_redis
            from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

            _transport = RedisStreamTransport(get_redis())
        else:
            raise RuntimeError(
                f"unknown POCKETPAW_CLOUD_STREAM_TRANSPORT={backend!r}"
            )
    return _transport


def _reset_for_tests() -> None:
    global _transport
    _transport = None
```

**Step 4: Implement `RedisStreamTransport`**

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/redis_stream.py
"""Redis-Streams implementation of RunStreamTransport.

Key layout:
  run:{run_id}:events   XADD stream of SSE events (the resumable log)
  run:{run_id}:cancel   string flag; presence = cancellation requested

Each stream entry has fields {"event": <type>, "data": <json>}. The Redis
entry id ("<ms>-<seq>") is monotonic and is what a reconnecting client passes
back as the cursor.

Works with any Redis-protocol server: Redis, Dragonfly, Valkey.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis

from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent


def _events_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _cancel_key(run_id: str) -> str:
    return f"run:{run_id}:cancel"


class RedisStreamTransport:
    """RunStreamTransport backed by Redis Streams."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str:
        return await self._redis.xadd(
            _events_key(run_id),
            {"event": event, "data": json.dumps(data)},
        )

    async def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]:
        """Yield events after the cursor, then block for live ones. Stops on
        a terminal event. Returns when ``block`` times out with no entries —
        the caller loops and emits a heartbeat between calls."""
        cursor = after
        while True:
            resp = await self._redis.xread(
                {_events_key(run_id): cursor}, block=block_ms, count=64
            )
            if not resp:
                return
            _key, entries = resp[0]
            for entry_id, fields in entries:
                cursor = entry_id
                ev = StreamEvent(
                    entry_id=entry_id,
                    event=fields["event"],
                    data=json.loads(fields["data"]),
                )
                yield ev
                if ev.is_terminal:
                    return

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None:
        await self._redis.expire(_events_key(run_id), ttl_seconds)
        await self._redis.expire(_cancel_key(run_id), ttl_seconds)

    async def request_cancel(self, run_id: str) -> None:
        await self._redis.set(_cancel_key(run_id), "1", ex=3600)

    async def is_cancelled(self, run_id: str) -> bool:
        return bool(await self._redis.exists(_cancel_key(run_id)))

    async def stream_exists(self, run_id: str) -> bool:
        return bool(await self._redis.exists(_events_key(run_id)))
```

**Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_redis_stream.py -v`
Expected: PASS (5 tests).

**Step 6: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/transport.py ee/pocketpaw_ee/cloud/chat/runs/redis_stream.py tests/cloud/runs/test_redis_stream.py
git commit -m "feat(runs): add RunStreamTransport Protocol + Redis impl

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Run service — document CRUD

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/service.py`
- Test: `backend/tests/cloud/runs/test_run_service.py`

`service.py` is the only module importing `ChatRunDoc`. CRUD now; `execute_run` is added in Task 7.

**Step 1: Write the failing test**

This test needs MongoDB (use the cloud test DB fixture — copy how `tests/cloud/` files obtain a Beanie-initialised DB; look for a `conftest.py` fixture such as `cloud_db`).

```python
# backend/tests/cloud/runs/test_run_service.py
import pytest

from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

pytestmark = pytest.mark.asyncio


def _spec(run_id="r1", scope_id="s1"):
    return RunSpec(
        run_id=run_id, workspace_id="w1", context_type="session", scope_id=scope_id,
        session_key=f"session:{scope_id}", group=None, user_id="u1", agent_id="a1",
        client_message_id=f"c-{run_id}", user_message_id="m1", content="hi",
        history=[], intent=None,
    )


async def test_create_and_get(cloud_db):
    await run_service.create_run(_spec())
    doc = await run_service.get_run("r1")
    assert doc.status == "queued"


async def test_mark_running_then_completed(cloud_db):
    await run_service.create_run(_spec())
    await run_service.mark_running("r1")
    await run_service.mark_completed("r1", assistant_message_id="m2", partial_text="done")
    doc = await run_service.get_run("r1")
    assert doc.status == "completed"
    assert doc.assistant_message_id == "m2"
    assert doc.ended_at is not None


async def test_find_active_run_for_scope_returns_newest_nonterminal(cloud_db):
    await run_service.create_run(_spec(run_id="old"))
    await run_service.mark_completed("old", assistant_message_id=None, partial_text="")
    await run_service.create_run(_spec(run_id="live"))
    await run_service.mark_running("live")
    active = await run_service.find_active_run_for_scope(
        workspace_id="w1", context_type="session", scope_id="s1"
    )
    assert active is not None and active.run_id == "live"


async def test_create_run_is_idempotent_on_client_message_id(cloud_db):
    spec = _spec()
    first = await run_service.create_run(spec)
    second = await run_service.create_run(spec)  # same client_message_id
    assert first.run_id == second.run_id
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_service.py -v`
Expected: FAIL — module not found.

> If the `cloud_db` fixture name differs, grep `tests/cloud/conftest.py` for the Beanie-init fixture and use that name.

**Step 3: Write minimal implementation**

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/service.py
"""Chat-run service — the only module that touches ChatRunDoc."""

from __future__ import annotations

from datetime import datetime, timezone

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_run(spec: RunSpec) -> ChatRunDoc:
    """Create the run doc. Idempotent on (workspace, client_message_id) so a
    re-submitted message returns the existing run instead of duplicating."""
    existing = await ChatRunDoc.find_one(
        ChatRunDoc.workspace == spec.workspace_id,
        ChatRunDoc.client_message_id == spec.client_message_id,
    )
    if existing is not None:
        return existing
    doc = ChatRunDoc(
        run_id=spec.run_id,
        workspace=spec.workspace_id,
        context_type=spec.context_type,
        scope_id=spec.scope_id,
        session_key=spec.session_key,
        group=spec.group,
        user_id=spec.user_id,
        agent_id=spec.agent_id,
        client_message_id=spec.client_message_id,
        user_message_id=spec.user_message_id,
    )
    await doc.insert()
    return doc


async def get_run(run_id: str) -> ChatRunDoc:
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id)
    if doc is None:
        raise NotFound("chat_run", run_id)
    return doc


async def mark_running(run_id: str) -> None:
    doc = await get_run(run_id)
    doc.status = "running"
    doc.started_at = _utcnow()
    await doc.save()


async def mark_completed(run_id: str, *, assistant_message_id: str | None, partial_text: str) -> None:
    doc = await get_run(run_id)
    doc.status = "completed"
    doc.assistant_message_id = assistant_message_id
    doc.partial_text = partial_text
    doc.ended_at = _utcnow()
    await doc.save()


async def mark_terminal(
    run_id: str,
    *,
    status: str,
    partial_text: str = "",
    error: str | None = None,
    assistant_message_id: str | None = None,
) -> None:
    """Set a non-completed terminal status: interrupted | failed | cancelled."""
    doc = await get_run(run_id)
    doc.status = status  # type: ignore[assignment]
    doc.partial_text = partial_text or doc.partial_text
    doc.error = error
    doc.assistant_message_id = assistant_message_id or doc.assistant_message_id
    doc.ended_at = _utcnow()
    await doc.save()


async def find_active_run_for_scope(
    *, workspace_id: str, context_type: str, scope_id: str
) -> ChatRunDoc | None:
    """Newest non-terminal run for a scope — drives frontend auto-resume."""
    return await ChatRunDoc.find(
        ChatRunDoc.workspace == workspace_id,
        ChatRunDoc.context_type == context_type,
        ChatRunDoc.scope_id == scope_id,
        {"status": {"$in": ["queued", "running"]}},
    ).sort(-ChatRunDoc.createdAt).first_or_none()


async def find_stale_running(older_than: datetime) -> list[ChatRunDoc]:
    """Runs left queued/running before a cutoff — for the PR2 startup sweep."""
    return await ChatRunDoc.find(
        {"status": {"$in": ["queued", "running"]}},
        ChatRunDoc.createdAt < older_than,
    ).to_list()
```

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_service.py -v`
Expected: PASS (4 tests).

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/service.py tests/cloud/runs/test_run_service.py
git commit -m "feat(runs): add chat-run service CRUD

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `execute_run` — relocate the agent loop

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/run_core.py`
- Reference (do not break): `backend/ee/pocketpaw_ee/cloud/chat/agent_router.py:439-709` (`_run_agent_stream`)
- Test: `backend/tests/cloud/runs/test_run_core.py`

`execute_run(spec)` is the agent loop, lifted from `_run_agent_stream`, but instead of `yield ("chunk", ...)` it calls `redis_stream.append_event(...)`. It is the shared body both executors invoke.

**Step 1: Write the failing test** (fake agent + fakeredis — no MongoDB needed for the event-sequence assertions; mock `run_service` writes)

```python
# backend/tests/cloud/runs/test_run_core.py
import fakeredis.aioredis
import pytest

from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

pytestmark = pytest.mark.asyncio


def _spec():
    return RunSpec(
        run_id="r1", workspace_id="w1", context_type="session", scope_id="s1",
        session_key="session:s1", group=None, user_id="u1", agent_id="a1",
        client_message_id="c1", user_message_id="m1", content="hi",
        history=[], intent=None,
    )


async def test_execute_run_writes_chunks_then_stream_end(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_agent_events(spec):
        yield ("chunk", {"content": "Hello", "type": "text"})
        yield ("chunk", {"content": " world", "type": "text"})

    # See implementation notes — patch the seams run_core exposes:
    from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
    transport = RedisStreamTransport(redis)

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["chunk", "chunk", "stream_end"]


async def _noop(*a, **k):
    return None


async def _persist_stub(spec, full_text, attachments):
    return "assistant-msg-1"
```

> The exact monkeypatch seams depend on how you factor `run_core`. The contract that MUST hold: given a fake agent producing two chunks, the Redis stream ends with a `stream_end` event carrying a non-null `assistant_message_id`; a cancelled run ends with `stream_end` + `cancelled: true` and no assistant message; an exception ends with an `error` event.

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_core.py -v`
Expected: FAIL — module not found.

**Step 3: Write the implementation**

Create `run_core.py` by moving the body of `_run_agent_stream` (`agent_router.py:439-709`). It talks to the **transport** (a `RunStreamTransport`), never to Redis directly. Transformation rules:

1. Signature becomes `async def execute_run(spec: RunSpec) -> None`. Rebuild a `ScopeContext` from `spec` via `resolve_scope_context(...)` at the top. At the top also: `transport = get_stream_transport()`.
2. Replace every `yield (name, data)` with `await transport.append_event(spec.run_id, name, data)`.
3. The cancel check `if cancel_event.is_set()` becomes `if await transport.is_cancelled(spec.run_id)`.
4. Keep the side-channel `asyncio.Queue` + `attach_sse_event_sink` machinery exactly as-is — drained items are appended through the transport the same way.
5. At the start: `await run_service.mark_running(spec.run_id)`.
6. On normal end: persist the assistant message (reuse `agent_router._persist_assistant_message` logic — move that helper, or import it), then `await run_service.mark_completed(...)`, then `transport.append_event("stream_end", {...})`, then `transport.set_ttl(run_id, STREAM_TTL)`.
7. On `cancelled`: `run_service.mark_terminal(status="cancelled", partial_text=full_text)`, append `stream_end` with `cancelled: true`, persist the partial as an assistant message **if non-empty** with metadata `interrupted: true`, set TTL.
8. On exception: `run_service.mark_terminal(status="failed", partial_text=full_text, error=str(e))`, append `error` event, set TTL.
9. Keep `_broadcast_message_new` / `_broadcast_agent_typing` calls — they still belong here.
10. `STREAM_TTL = int(os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "900"))`.

Expose `_iter_agent_events`, `get_stream_transport`, and the persist helper as module-level names so tests can patch them. **Do not import `redis_stream` or `redis` here** — the whole point of the transport seam is that `run_core` is backend-agnostic.

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_core.py -v`
Expected: PASS.

**Step 5: Run the existing agent-router tests to confirm nothing else broke yet**

Run: `cd backend && uv run pytest tests/cloud/chat/ -v -k agent`
Expected: still PASS (agent_router.py is untouched so far — `_run_agent_stream` still exists; we delete it in Task 8).

**Step 6: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/run_core.py tests/cloud/runs/test_run_core.py
git commit -m "feat(runs): add execute_run agent loop writing to Redis Stream

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `RunExecutor` protocol + `InProcessExecutor`

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/executor.py`
- Test: `backend/tests/cloud/runs/test_executor.py`

**Step 1: Write the failing test**

```python
# backend/tests/cloud/runs/test_executor.py
import asyncio

import pytest

from pocketpaw_ee.cloud.chat.runs import executor as ex
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

pytestmark = pytest.mark.asyncio


def _spec():
    return RunSpec(
        run_id="r1", workspace_id="w1", context_type="session", scope_id="s1",
        session_key="session:s1", group=None, user_id="u1", agent_id="a1",
        client_message_id="c1", user_message_id="m1", content="hi",
        history=[], intent=None,
    )


async def test_in_process_executor_runs_execute_run(monkeypatch):
    seen = []

    async def fake_execute_run(spec):
        seen.append(spec.run_id)

    monkeypatch.setattr(ex, "execute_run", fake_execute_run)
    inproc = ex.InProcessExecutor()
    await inproc.submit(_spec())
    await inproc.drain()  # await all tracked tasks
    assert seen == ["r1"]


async def test_executor_selection_defaults_to_inprocess(monkeypatch):
    monkeypatch.delenv("POCKETPAW_CLOUD_RUN_EXECUTOR", raising=False)
    ex._reset_for_tests()
    assert isinstance(ex.get_executor(), ex.InProcessExecutor)
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_executor.py -v`
Expected: FAIL — module not found.

**Step 3: Write minimal implementation**

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/executor.py
"""RunExecutor — decides WHERE an agent run executes.

Tier 1: InProcessExecutor (asyncio task in the web process).
Tier 2: ArqExecutor (added in PR2) enqueues a job for a worker service.
Selected by POCKETPAW_CLOUD_RUN_EXECUTOR (inprocess | arq).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Protocol

from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.run_core import execute_run

logger = logging.getLogger(__name__)


class RunExecutor(Protocol):
    async def submit(self, spec: RunSpec) -> None: ...


class InProcessExecutor:
    """Runs execute_run() in a tracked asyncio task in the web process."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def submit(self, spec: RunSpec) -> None:
        task = asyncio.create_task(self._guarded(spec))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, spec: RunSpec) -> None:
        try:
            await execute_run(spec)
        except Exception:
            logger.exception("in-process run %s crashed", spec.run_id)

    async def drain(self) -> None:
        """Await all outstanding run tasks (used on shutdown and in tests)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


_executor: RunExecutor | None = None


def get_executor() -> RunExecutor:
    global _executor
    if _executor is None:
        mode = os.environ.get("POCKETPAW_CLOUD_RUN_EXECUTOR", "inprocess").lower()
        if mode == "arq":
            from pocketpaw_ee.cloud.chat.runs.arq_executor import ArqExecutor

            _executor = ArqExecutor()
        else:
            _executor = InProcessExecutor()
    return _executor


def _reset_for_tests() -> None:
    global _executor
    _executor = None
```

> `arq_executor` does not exist yet — the `arq` branch is only reached in PR2. Keep the import inside the branch so PR1 never imports it.

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_executor.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/executor.py tests/cloud/runs/test_executor.py
git commit -m "feat(runs): add RunExecutor protocol and InProcessExecutor

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Run router — GET stream + POST stop

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/dto.py`
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/router.py`
- Test: `backend/tests/cloud/runs/test_run_router.py`

**Step 1: Write the failing test** (integration — seed a fakeredis stream, hit the endpoint via FastAPI `TestClient`/`httpx.AsyncClient`; copy the app-fixture pattern from an existing `tests/cloud/chat/` router test)

```python
# backend/tests/cloud/runs/test_run_router.py
import fakeredis.aioredis
import pytest

pytestmark = pytest.mark.asyncio


async def test_stream_replays_buffered_events(cloud_app, monkeypatch, seed_run):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
    transport = RedisStreamTransport(redis)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.router.get_stream_transport", lambda: transport
    )
    await transport.append_event("r1", "chunk", {"content": "hi"})
    await transport.append_event("r1", "stream_end", {"assistant_message_id": "m2"})

    resp = await cloud_app.get("/api/v1/cloud/chat/runs/r1/stream?after=0")
    body = resp.text
    assert "event: chunk" in body
    assert "event: stream_end" in body
    assert "id: " in body  # each frame carries the transport entry id


async def test_stop_sets_cancel_flag(cloud_app, monkeypatch, seed_run):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
    transport = RedisStreamTransport(redis)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.router.get_stream_transport", lambda: transport
    )
    resp = await cloud_app.post("/api/v1/cloud/chat/runs/r1/stop")
    assert resp.status_code == 200
    assert await transport.is_cancelled("r1") is True
```

> `cloud_app` = an authenticated test client; `seed_run` = a fixture inserting a `ChatRunDoc(run_id="r1")` whose scope membership includes the test user. Build both in `tests/cloud/runs/conftest.py`, mirroring existing cloud router tests.

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_router.py -v`
Expected: FAIL — router module not found / route 404.

**Step 3: Write minimal implementation**

`dto.py`:

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/dto.py
from __future__ import annotations

from pydantic import BaseModel


class StopRunResponse(BaseModel):
    status: str = "ok"
```

`router.py`:

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/router.py
"""Run streaming + control endpoints.

GET  /cloud/chat/runs/{run_id}/stream?after=<entry_id>   resumable SSE
POST /cloud/chat/runs/{run_id}/stop                       request cancellation
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.dto import StopRunResponse
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


def _sse(entry_id: str, event: str, data: dict) -> bytes:
    return f"id: {entry_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _authorize(run_id: str, workspace_id: str) -> object:
    """Load the run and confirm it belongs to the caller's workspace."""
    doc = await run_service.get_run(run_id)  # raises NotFound
    if doc.workspace != workspace_id:
        from pocketpaw_ee.cloud._core.errors import NotFound

        raise NotFound("chat_run", run_id)
    return doc


@router.get("/cloud/chat/runs/{run_id}/stream")
async def get_run_stream(
    run_id: str,
    after: str = Query("0"),
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    doc = await _authorize(run_id, workspace_id)
    transport = get_stream_transport()

    async def gen() -> AsyncIterator[bytes]:
        cursor = after
        # If the stream has already expired, fall back to the Mongo run doc.
        if not await transport.stream_exists(run_id):
            yield _sse(
                "0-0",
                "stream_end",
                {
                    "assistant_message_id": doc.assistant_message_id,
                    "cancelled": doc.status in ("cancelled", "interrupted"),
                    "from_history": True,
                },
            )
            return
        while True:
            saw_terminal = False
            async for ev in transport.read_events(run_id, after=cursor, block_ms=15000):
                cursor = ev.entry_id
                yield _sse(ev.entry_id, ev.event, ev.data)
                if ev.is_terminal:
                    saw_terminal = True
            if saw_terminal:
                return
            # block timed out — heartbeat so proxies keep the connection open
            yield b": ping\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cloud/chat/runs/{run_id}/stop")
async def post_run_stop(
    run_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StopRunResponse:
    await _authorize(run_id, workspace_id)
    await get_stream_transport().request_cancel(run_id)
    return StopRunResponse()
```

**Step 4: Wire the router** — in `ee/pocketpaw_ee/cloud/__init__.py` `mount_cloud()`, next to the other chat routers (~line 147), add:

```python
from pocketpaw_ee.cloud.chat.runs.router import router as runs_router
...
app.include_router(runs_router, prefix="/api/v1")
```

**Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_run_router.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/dto.py ee/pocketpaw_ee/cloud/chat/runs/router.py ee/pocketpaw_ee/cloud/__init__.py tests/cloud/runs/test_run_router.py tests/cloud/runs/conftest.py
git commit -m "feat(runs): add resumable stream + stop endpoints

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Slim the POST `/agent` endpoint

**Files:**
- Modify: `backend/ee/pocketpaw_ee/cloud/chat/agent_router.py`
- Test: `backend/tests/cloud/chat/test_agent_router.py` (update existing; grep for the current test file name first)

The POST endpoint stops streaming and returns JSON `{run_id, user_message_id, session_id}`. The `_run_agent_stream`, `_persist_assistant_message`, broadcast helpers, titling, and ripple-extraction code moved into `run_core.py` in Task 6 — delete them here (or re-export the ones `run_core` imports). Keep `_persist_user_message`, `_ensure_scope_session`, scope resolution.

**Step 1: Update the test** — the endpoint now returns JSON, not an SSE body:

```python
async def test_post_agent_creates_run_and_returns_json(cloud_app, ...):
    resp = await cloud_app.post(
        "/api/v1/cloud/chat/session/s1/agent",
        json={"content": "hello", "client_message_id": "c1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"]
    assert body["user_message_id"]
    # a ChatRunDoc now exists with status queued/running
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    doc = await run_service.get_run(body["run_id"])
    assert doc.status in ("queued", "running", "completed")


async def test_post_agent_submits_to_executor(cloud_app, monkeypatch, ...):
    submitted = []

    class _FakeExecutor:
        async def submit(self, spec):
            submitted.append(spec.run_id)

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_router.get_executor", lambda: _FakeExecutor()
    )
    resp = await cloud_app.post(
        "/api/v1/cloud/chat/session/s1/agent",
        json={"content": "hi", "client_message_id": "c2"},
    )
    assert submitted == [resp.json()["run_id"]]
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/chat/test_agent_router.py -v`
Expected: FAIL — endpoint still returns `text/event-stream`.

**Step 3: Rewrite `post_agent_chat`**

```python
@router.post("/cloud/chat/{scope}/{scope_id}/agent")
async def post_agent_chat(
    scope: Scope,
    scope_id: str,
    body: CloudAgentChatRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    try:
        ctx = await resolve_scope_context(
            scope=scope, scope_id=scope_id, user_id=user_id, agent_id_hint=body.agent_id
        )
        ctx.intent = body.intent
    except InvalidScope:
        raise CloudError(400, "scope.invalid", "Invalid scope") from None

    # Cancel any in-flight run for this scope (cross-process via Redis).
    prior = await run_service.find_active_run_for_scope(
        workspace_id=workspace_id, context_type=scope, scope_id=scope_id
    )
    if prior is not None:
        await get_stream_transport().request_cancel(prior.run_id)

    history = await load_history_for_scope(ctx)
    user_message_id = await _persist_user_message(ctx, body)

    try:
        ctx.session_id = await _ensure_scope_session(ctx)
    except Exception:
        logger.exception("ensure session failed for scope %s", ctx.kind.value)
        ctx.session_id = None

    run_id = uuid.uuid4().hex
    spec = RunSpec(
        run_id=run_id,
        workspace_id=workspace_id,
        context_type=scope,
        scope_id=scope_id,
        session_key=session_key_for(ctx),
        group=scope_id if scope in ("dm", "group") else None,
        user_id=user_id,
        agent_id=ctx.target_agent_id,
        client_message_id=body.client_message_id,
        user_message_id=user_message_id,
        content=body.content,
        history=history,
        intent=body.intent,
        attachments=body.attachments or [],
        mentions=body.mentions or [],
        reply_to=body.reply_to,
    )
    run = await run_service.create_run(spec)          # idempotent
    await get_executor().submit(spec)

    return {
        "run_id": run.run_id,
        "user_message_id": user_message_id,
        "session_id": ctx.session_id,
        "client_message_id": body.client_message_id,
    }
```

Delete the old `/agent/stop` endpoint and the `_active_runs` dict (the stop endpoint is now `runs/router.py`). Delete `_run_agent_stream`, `_run_agent_stream`'s helpers that moved to `run_core.py`, `_sse`, `_new_run_id`. Add imports: `RunSpec`, `run_service`, `get_stream_transport`, `get_executor`.

**Step 4: Run the test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/chat/test_agent_router.py -v`
Expected: PASS.

**Step 5: Run the full chat test suite**

Run: `cd backend && uv run pytest tests/cloud/chat/ tests/cloud/runs/ -v`
Expected: PASS (pre-existing MongoDB-env failures aside — see `reference_backend_test_env`).

**Step 6: Lint + type check**

Run: `cd backend && uv run ruff check . && uv run ruff format . && uv run mypy ee/pocketpaw_ee/cloud/chat/runs ee/pocketpaw_ee/cloud/chat/agent_router.py`
Expected: clean.

**Step 7: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/agent_router.py tests/cloud/chat/test_agent_router.py
git commit -m "feat(runs): POST /agent now creates a run and returns JSON

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Expose `active_run` on history load

**Files:**
- Modify: the session-history endpoint/service the frontend calls on session open. Grep first: `cd backend && rg "getSessionHistory|session.*messages" ee/pocketpaw_ee/cloud/sessions` and check `sessions/router.py` / `sessions/service.py`.
- Test: `backend/tests/cloud/sessions/test_session_history.py` (extend existing)

**Step 1: Write the failing test**

```python
async def test_session_history_includes_active_run(cloud_app, seed_running_run, ...):
    resp = await cloud_app.get("/api/v1/.../session/s1/messages")  # real path
    body = resp.json()
    assert body["active_run"]["run_id"] == "live"
    assert body["active_run"]["status"] == "running"


async def test_session_history_active_run_null_when_idle(cloud_app, ...):
    resp = await cloud_app.get("/api/v1/.../session/s1/messages")
    assert resp.json()["active_run"] is None
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/sessions/test_session_history.py -v -k active_run`
Expected: FAIL — `active_run` key absent.

**Step 3: Implement** — in the history service function, after assembling messages, call `run_service.find_active_run_for_scope(...)` for the session's scope and add to the response dict:

```python
active = await run_service.find_active_run_for_scope(
    workspace_id=workspace_id, context_type="session", scope_id=session_scope_id
)
result["active_run"] = (
    {"run_id": active.run_id, "status": active.status} if active else None
)
```

Do the same for the group-messages endpoint (`chat/router.py` `get_messages` → `message_service.get_messages`) so `dm`/`group` scopes resume too.

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/sessions/test_session_history.py -v -k active_run`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/sessions/ ee/pocketpaw_ee/cloud/chat/message_service.py tests/cloud/sessions/test_session_history.py
git commit -m "feat(runs): expose active_run on chat history responses

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Lifecycle wiring — drain in-process runs on shutdown

**Files:**
- Modify: `backend/ee/pocketpaw_ee/cloud/__init__.py` (`mount_cloud`)
- Test: `backend/tests/cloud/runs/test_lifecycle.py`

**Step 1: Write the failing test**

```python
async def test_shutdown_drains_in_process_executor(monkeypatch):
    from pocketpaw_ee.cloud.chat.runs import executor as ex
    ex._reset_for_tests()
    monkeypatch.delenv("POCKETPAW_CLOUD_RUN_EXECUTOR", raising=False)
    inproc = ex.get_executor()
    assert isinstance(inproc, ex.InProcessExecutor)
    # a slow run still in flight
    import asyncio
    done = []

    async def slow(spec):
        await asyncio.sleep(0.05)
        done.append(spec.run_id)

    monkeypatch.setattr(ex, "execute_run", slow)
    await inproc.submit(_spec())
    await inproc.drain()
    assert done == ["r1"]
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_lifecycle.py -v`
Expected: FAIL until the test imports resolve / `drain` behaves.

**Step 3: Implement** — in `mount_cloud`, alongside `_stop_agent_pool` (~line 497), add a shutdown hook:

```python
@app.on_event("shutdown")
async def _drain_chat_runs() -> None:
    from pocketpaw_ee.cloud.chat.runs.executor import InProcessExecutor, get_executor

    ex = get_executor()
    if isinstance(ex, InProcessExecutor):
        await ex.drain()
```

> **Investigate:** `mount_cloud`'s comment at `__init__.py:430-435` claims `@app.on_event("startup")` handlers never fire under the host's `lifespan`, yet `_start_agent_pool` is registered that way and clearly must run. Confirm by adding a log line and starting the dev server (`uv run pocketpaw --dev`). If `on_event` truly does not fire, register the drain inside the host `lifespan` in `src/pocketpaw/dashboard.py` instead. Resolve this before relying on the hook.

**Step 4: Run test + smoke the server**

Run: `cd backend && uv run pytest tests/cloud/runs/test_lifecycle.py -v`
Expected: PASS.
Run: `cd backend && uv run pocketpaw --dev` → confirm it boots, then Ctrl-C.
Expected: clean startup/shutdown, no Redis errors at boot (Redis only touched on first chat).

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/__init__.py tests/cloud/runs/test_lifecycle.py
git commit -m "feat(runs): drain in-process runs on shutdown

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Frontend — per-scope run state

**Files:**
- Modify: `paw-enterprise/src/lib/stores/chat.svelte.ts`
- Reference: `paw-enterprise/src/lib/core/chat/store.svelte.ts` (group store already keys by `groupId` — copy the shape)

Run frontend commands from `paw-enterprise/`.

**Step 1: Read both stores** and identify everywhere `chatStore.streamingContent` / `chatStore.messages` / `isStreaming` are referenced (`rg "streamingContent|isStreaming" src`).

**Step 2: Refactor** — replace the single streaming fields with a per-scope map:

```ts
type RunState = {
  runId: string | null;
  status: "idle" | "streaming" | "interrupted" | "error";
  text: string;          // accumulated streaming text
  lastEventId: string;   // Redis entry id of the last frame consumed
};

class ChatStore {
  // keyed by scopeKey = `${scope}:${scopeId}`
  runs = $state<Record<string, RunState>>({});
  activeScopeKey = $state<string>("");

  current = $derived(this.runs[this.activeScopeKey] ?? EMPTY_RUN_STATE);
  // ...messages stay keyed per scope too, mirroring core/chat/store.svelte.ts
}
```

Update `stopGeneration`, `loadHistory`, and the streaming setters to operate on `runs[scopeKey]`.

**Step 3: Type check**

Run: `cd paw-enterprise && bun run check`
Expected: clean (fix call sites the refactor surfaces).

**Step 4: Commit**

```bash
git add paw-enterprise/src/lib/stores/chat.svelte.ts paw-enterprise/src/lib/components/chat/
git commit -m "feat(chat): per-scope run state in chat store

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Frontend — POST returns run_id, open GET stream

**Files:**
- Modify: `paw-enterprise/src/lib/core/chat/service.ts` (`streamAgentSSE` ~645-767, `agentChat` ~779-955)

**Step 1: Read `streamAgentSSE`** and note the SSE-frame parser — it is reused verbatim.

**Step 2: Split the flow into two calls:**

```ts
// 1. POST — fast, returns JSON
const res = await fetch(`${base}/cloud/chat/${scope}/${scopeId}/agent`, {
  method: "POST", credentials: "include", headers: authHeaders(),
  body: JSON.stringify(body),
});
const { run_id, user_message_id, session_id } = await res.json();
if (session_id) adoptSessionId(session_id);

// 2. GET — consume the resumable stream
await openRunStream(run_id, { after: "0", signal });
```

`openRunStream(runId, { after, signal })`:
- `fetch(`${base}/cloud/chat/runs/${runId}/stream?after=${after}`, { credentials, headers, signal })`
- Reuse the existing `ReadableStream` SSE parser. For each frame, capture the `id:` line into `runs[scopeKey].lastEventId`, and dispatch `event`/`data` to the existing handlers (`chunk`, `tool_start`, `ripple`, `stream_end`, …).
- On `stream_end`/`error`, finalize as today.

**Step 3: Type check + manual test**

Run: `cd paw-enterprise && bun run check`
Then run the desktop app against a local backend (`bun run tauri dev`, backend up) and send a message — confirm the response streams.

**Step 4: Commit**

```bash
git add paw-enterprise/src/lib/core/chat/service.ts
git commit -m "feat(chat): consume agent reply via resumable run stream

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: Frontend — session switch keeps the run alive

**Files:**
- Modify: `paw-enterprise/src/lib/core/sessions/service.ts` (`switchSession`)
- Modify: `paw-enterprise/src/lib/stores/chat.svelte.ts` (`stopGeneration`)

**Step 1:** In `switchSession`, replace any `abortController.abort()` / `stopGeneration()` call with **close the stream reader only** — cancel the `fetch` reader for the *outgoing* scope but do **not** call `/stop`. The run keeps executing server-side. `stopGeneration` (explicit Stop button) is the only path that calls `POST /cloud/chat/runs/{runId}/stop`.

**Step 2: Manual test** — send a message, switch to another session mid-stream, switch back: the run should still be progressing (verify in the other session it resumes — covered by Task 15). Send in two sessions and confirm both stream independently.

**Step 3: Commit**

```bash
git add paw-enterprise/src/lib/core/sessions/service.ts paw-enterprise/src/lib/stores/chat.svelte.ts
git commit -m "feat(chat): session switch no longer kills the agent run

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: Frontend — resume on mount

**Files:**
- Modify: `paw-enterprise/src/lib/core/sessions/service.ts` (`switchSession`) / `paw-enterprise/src/lib/components/chat/ChatPanel.svelte` (`onMount`)

**Step 1:** After `switchSession` loads history, read `active_run` from the response. If non-null and `status` ∈ `{queued, running}`, call `openRunStream(active_run.run_id, { after: "0" })` — `after=0` replays the whole buffered partial then continues live. Set `runs[scopeKey].status = "streaming"`.

**Step 2: Manual test — the core acceptance test:**
1. Send a message; while it streams, **refresh the page (Ctrl-R)**.
2. Expected: after reload, the chat reappears and the partial response continues streaming to completion.
3. Send a message; switch sessions; switch back mid-stream.
4. Expected: the response is there and still streaming / completed.

**Step 3: Commit**

```bash
git add paw-enterprise/src/lib/core/sessions/service.ts paw-enterprise/src/lib/components/chat/ChatPanel.svelte
git commit -m "feat(chat): resume an in-flight run after refresh / session switch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: Frontend — interrupted-run Retry affordance

**Files:**
- Modify: the message-rendering component (grep `rg "interrupted" src` and the message renderer in `src/lib/components/chat/`)

**Step 1:** When a rendered assistant message carries metadata `interrupted: true`, show a **Retry** button beneath it. Retry re-sends the originating user message (same content) via the normal send path.

**Step 2: Type check**

Run: `cd paw-enterprise && bun run check`
Expected: clean.

**Step 3: Commit**

```bash
git add paw-enterprise/src/lib/components/chat/
git commit -m "feat(chat): Retry affordance for interrupted runs

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 17: PR1 — full suite, docs, open PR

**Step 1: Backend suite**

Run: `cd backend && uv run pytest --ignore=tests/e2e -q`
Expected: no *new* failures vs. baseline (pre-existing MongoDB-env failures per `reference_backend_test_env`).

**Step 2: Lint/type both projects**

Run: `cd backend && uv run ruff check . && uv run mypy ee/pocketpaw_ee/cloud/chat/runs`
Run: `cd paw-enterprise && bun run check`
Expected: clean.

**Step 3: Document the env vars** — add to `backend/CLAUDE.md` (Key Conventions / config section): `POCKETPAW_REDIS_URL`, `POCKETPAW_CLOUD_RUN_EXECUTOR` (default `inprocess`), `POCKETPAW_CLOUD_STREAM_TRANSPORT` (default `redis` — Dragonfly/Valkey work via the same setting since they speak the Redis protocol), `POCKETPAW_CLOUD_RUN_STREAM_TTL` (default `900`).

**Step 4: Commit docs + open the PR**

```bash
git add backend/CLAUDE.md
git commit -m "docs: document resumable-run env vars

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push -u origin feat/resumable-chat-runs-tier1
gh pr create --title "feat: resumable chat runs (Tier 1)" --body "<summary of design + behaviour; reference docs/plans/2026-05-22-resumable-chat-runs-design.md; no AI-attribution footer>"
```

**Deploy note for PR1:** provision a Redis service in Coolify and set `POCKETPAW_REDIS_URL` on the backend service before this ships. `POCKETPAW_CLOUD_RUN_EXECUTOR` stays unset (`inprocess`).

---

# PR 2 — Tier 2 (arq worker, crash survival)

**Step 1: Branch (stacked on PR1)**

```bash
cd backend
git checkout -b feat/resumable-chat-runs-tier2
```

---

## Task 18: Add the `arq` dependency

**Files:**
- Modify: `backend/ee/pyproject.toml`

**Step 1:** Add `"arq>=0.26.0"` to the EE package's runtime dependencies (the `[project]` `dependencies` list, near `redis[hiredis]`).

Run: `cd backend && uv sync --dev --group ee`
Expected: `arq` installs.

**Step 2: Commit**

```bash
git add ee/pyproject.toml
git commit -m "build: add arq dependency for Tier 2 run worker

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 19: `ArqExecutor`

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/arq_executor.py`
- Test: `backend/tests/cloud/runs/test_arq_executor.py`

**Step 1: Write the failing test**

```python
# backend/tests/cloud/runs/test_arq_executor.py
import pytest

from pocketpaw_ee.cloud.chat.runs.arq_executor import ArqExecutor

pytestmark = pytest.mark.asyncio


async def test_arq_executor_enqueues_execute_run_job(monkeypatch):
    enqueued = []

    class _FakePool:
        async def enqueue_job(self, name, payload):
            enqueued.append((name, payload))

    async def _fake_pool():
        return _FakePool()

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.arq_executor._get_pool", _fake_pool)
    ex = ArqExecutor()
    await ex.submit(_spec())  # _spec() helper as in earlier tasks
    assert enqueued[0][0] == "execute_run_job"
    assert enqueued[0][1]["run_id"] == "r1"
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_arq_executor.py -v`
Expected: FAIL — module not found.

**Step 3: Write minimal implementation**

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/arq_executor.py
"""Tier 2 executor — enqueues the run as an arq job for the worker service."""

from __future__ import annotations

import os

from arq import create_pool
from arq.connections import RedisSettings

from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

_pool = None


async def _get_pool():
    global _pool
    if _pool is None:
        url = os.environ["POCKETPAW_REDIS_URL"]
        _pool = await create_pool(RedisSettings.from_dsn(url))
    return _pool


class ArqExecutor:
    async def submit(self, spec: RunSpec) -> None:
        pool = await _get_pool()
        # spec.model_dump() is plain primitives — safe across the arq boundary.
        await pool.enqueue_job("execute_run_job", spec.model_dump())
```

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_arq_executor.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/arq_executor.py tests/cloud/runs/test_arq_executor.py
git commit -m "feat(runs): add ArqExecutor

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 20: arq worker entry point + startup sweep

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/chat/runs/worker.py`
- Test: `backend/tests/cloud/runs/test_worker.py`

The worker process must initialise the cloud DB + realtime bus before running any job (it is a separate process from the web app).

**Step 1: Write the failing test** (test the job wrapper + the sweep, not the arq runtime)

```python
# backend/tests/cloud/runs/test_worker.py
import pytest

from pocketpaw_ee.cloud.chat.runs import worker

pytestmark = pytest.mark.asyncio


async def test_execute_run_job_calls_execute_run(monkeypatch):
    seen = []

    async def fake_execute_run(spec):
        seen.append(spec.run_id)

    monkeypatch.setattr(worker, "execute_run", fake_execute_run)
    await worker.execute_run_job({}, _spec().model_dump())
    assert seen == ["r1"]


async def test_sweep_marks_stale_runs_interrupted(cloud_db, monkeypatch):
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    await run_service.create_run(_spec())
    await run_service.mark_running("r1")
    await worker.sweep_interrupted_runs()
    doc = await run_service.get_run("r1")
    assert doc.status == "interrupted"
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cloud/runs/test_worker.py -v`
Expected: FAIL — module not found.

**Step 3: Write minimal implementation**

```python
# backend/ee/pocketpaw_ee/cloud/chat/runs/worker.py
"""arq worker entry point for Tier 2 run execution.

Run it as a separate process:  arq pocketpaw_ee.cloud.chat.runs.worker.WorkerSettings
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from arq.connections import RedisSettings

from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.run_core import execute_run
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport

logger = logging.getLogger(__name__)


async def execute_run_job(ctx: dict, spec_dict: dict) -> None:
    """arq job: rehydrate the RunSpec and run the agent."""
    await execute_run(RunSpec.model_validate(spec_dict))


async def sweep_interrupted_runs() -> None:
    """Mark runs left running before this worker started as interrupted.

    LLM token streams cannot resume mid-generation, so we do NOT re-run — the
    partial already streamed stays visible and the user retries manually.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=5)
    stale = await run_service.find_stale_running(cutoff)
    transport = get_stream_transport()
    for doc in stale:
        await run_service.mark_terminal(
            doc.run_id, status="interrupted", partial_text=doc.partial_text
        )
        # Persist the partial as an assistant message so it survives the
        # Redis TTL and renders with a Retry button.
        if doc.partial_text.strip():
            try:
                from pocketpaw_ee.cloud.chat import message_service

                await message_service.persist_assistant_message_for_scope(
                    kind=doc.context_type,
                    scope_id=doc.scope_id,
                    user_id=doc.user_id,
                    workspace_id=doc.workspace,
                    session_key=doc.session_key,
                    target_agent_id=doc.agent_id,
                    content=doc.partial_text,
                    attachments=[],
                    metadata={"interrupted": True},
                )
            except Exception:
                logger.warning("sweep: persist partial failed for %s", doc.run_id, exc_info=True)
        if await transport.stream_exists(doc.run_id):
            await transport.append_event(doc.run_id, "interrupted", {"run_id": doc.run_id})
        logger.info("sweep: marked run %s interrupted", doc.run_id)


async def _startup(ctx: dict) -> None:
    """Worker boot: init DB + realtime, then sweep stale runs."""
    from pocketpaw_ee.cloud import init_realtime
    from pocketpaw_ee.cloud.shared.db import init_cloud_db

    mongo_uri = os.environ.get("POCKETPAW_MONGO_URI", "mongodb://localhost:27017/paw-enterprise")
    await init_cloud_db(mongo_uri)
    init_realtime()
    await sweep_interrupted_runs()


async def _shutdown(ctx: dict) -> None:
    from pocketpaw_ee.cloud.shared.db import close_cloud_db

    await close_cloud_db()


class WorkerSettings:
    functions = [execute_run_job]
    on_startup = _startup
    on_shutdown = _shutdown
    max_tries = 1          # crash policy: no auto-retry
    redis_settings = RedisSettings.from_dsn(
        os.environ.get("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    )
```

> Confirm `persist_assistant_message_for_scope` accepts a `metadata`/`extra` kwarg; if not, extend it minimally (touch-time, follow the 4-file rule for `message_service`) to stamp `interrupted: true`.
> Confirm the correct Mongo URI env var by checking how `init_cloud_db` is called in `src/pocketpaw/dashboard.py` — match that variable name.

**Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_worker.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/chat/runs/worker.py tests/cloud/runs/test_worker.py
git commit -m "feat(runs): add arq worker entry point + interrupted-run sweep

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 21: Web-side startup sweep for in-process runs

**Files:**
- Modify: `backend/ee/pocketpaw_ee/cloud/__init__.py` (`mount_cloud`)
- Test: `backend/tests/cloud/runs/test_lifecycle.py` (extend)

When the executor is `inprocess`, a web-process restart orphans runs the same way a worker crash does. On web startup, run the same sweep.

**Step 1: Write the failing test**

```python
async def test_web_startup_sweeps_when_inprocess(cloud_db, monkeypatch):
    monkeypatch.delenv("POCKETPAW_CLOUD_RUN_EXECUTOR", raising=False)
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.worker import sweep_interrupted_runs
    await run_service.create_run(_spec())
    await run_service.mark_running("r1")
    await sweep_interrupted_runs()
    assert (await run_service.get_run("r1")).status == "interrupted"
```

**Step 2: Run test to verify it fails / passes**

Run: `cd backend && uv run pytest tests/cloud/runs/test_lifecycle.py -v -k startup_sweeps`
Expected: PASS already (sweep exists) — this test pins the behaviour; the wiring below makes it run on boot.

**Step 3: Implement** — in `mount_cloud`, add a startup hook next to `_start_agent_pool`:

```python
@app.on_event("startup")
async def _sweep_orphaned_runs() -> None:
    import os as _os
    if _os.environ.get("POCKETPAW_CLOUD_RUN_EXECUTOR", "inprocess").lower() == "arq":
        return  # the arq worker owns the sweep in Tier 2
    from pocketpaw_ee.cloud.chat.runs.worker import sweep_interrupted_runs
    try:
        await sweep_interrupted_runs()
    except Exception:
        logging.getLogger(__name__).warning("orphaned-run sweep failed", exc_info=True)
```

(Apply the same `lifespan`-vs-`on_event` resolution from Task 11.)

**Step 4: Run test + smoke**

Run: `cd backend && uv run pytest tests/cloud/runs/test_lifecycle.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/pocketpaw_ee/cloud/__init__.py tests/cloud/runs/test_lifecycle.py
git commit -m "feat(runs): sweep orphaned in-process runs on web startup

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 22: Deploy the worker + flip the flag

**Files:**
- Create: `backend/docs/internal/2026-05-resumable-runs-deploy.md`

**Step 1:** Write a short deploy doc covering:
- Coolify: add a second service from the same backend image, start command `arq pocketpaw_ee.cloud.chat.runs.worker.WorkerSettings`.
- Required env on the worker service: `POCKETPAW_REDIS_URL`, `POCKETPAW_MONGO_URI` (match the web service), `ANTHROPIC_API_KEY`, and any agent-backend env the web service has.
- On the **web** service set `POCKETPAW_CLOUD_RUN_EXECUTOR=arq`.
- Rollback: unset `POCKETPAW_CLOUD_RUN_EXECUTOR` (back to `inprocess`); stop the worker service.

**Step 2: Manual end-to-end verification (staging):**
1. Worker running, web flag = `arq`. Send a message → response streams (proves enqueue → worker → Redis → web stream).
2. Mid-stream, **restart the worker service**. The run should be marked `interrupted`; the partial stays with a Retry button; Retry produces a fresh complete response.
3. Refresh mid-stream → response resumes (Tier 1 behaviour still holds under Tier 2).

**Step 3: Commit + open PR**

```bash
git add backend/docs/internal/2026-05-resumable-runs-deploy.md
git commit -m "docs: Tier 2 worker deployment guide

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push -u origin feat/resumable-chat-runs-tier2
gh pr create --title "feat: resumable chat runs (Tier 2 — arq worker)" --base feat/resumable-chat-runs-tier1 --body "<summary; no AI-attribution footer>"
```

---

## Done — acceptance criteria

- [ ] Refresh mid-stream → the chat reappears and the response keeps streaming.
- [ ] Switch session mid-stream and back → the run survived and is visible.
- [ ] Two sessions stream concurrently without interfering.
- [ ] Explicit Stop cancels the run; refresh does not.
- [ ] Tab closed forever → the run still completes and appears in history later.
- [ ] (Tier 2) Worker restart mid-run → run marked `interrupted`, partial kept, Retry works.
- [ ] No new test failures vs. the pre-existing MongoDB-env baseline.
```
