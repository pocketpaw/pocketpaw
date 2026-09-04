"""Cross-process bridge for the realtime bus and WebSocket fan-out.

Tier 2 of resumable chat runs splits the agent loop off into a separate
worker process. The worker's ``InProcessBus`` has no subscribers — every
listener lives in the web process — and the worker's ``WsManager`` has no
client connections. ``xproc`` ships envelopes through a Redis Stream so the
web process re-delivers them locally as if the emit had happened there.

Tier 1 (in-process executor) is unaffected: ``set_role`` defaults to ``web``,
``publish_*`` short-circuits, and the consumer runs but reads only events
emitted from arq workers — typically zero, since Tier 1 doesn't run any.

The stream + consumer group survives both worker and web restarts: arq
workers keep XADD-ing; the web's consumer group XACKs each delivered entry,
so a fresh web process resumes from the last unacked cursor.

Changes: 2026-09-04 (fix/concurrent-dispatch, backend-perf H2) — the consumer
dispatches a batch by ORDERING LANE instead of strictly one envelope at a
time. Every worker-originated realtime frame in the deployment flows through
this one loop, so a single slow dispatch stalled delivery for every tenant on
the box, not just the one that caused it — one workspace with a back-pressured
socket made agent replies appear frozen for every other customer.

The lane split is what makes that safe. These envelopes carry streamed agent
output, so the chunks of one reply must arrive in the order they were
produced; a plain ``gather`` over the batch would remove the stall and
scramble the answer instead. Envelopes sharing a lane still run in stream
order, and ``_ordering_lane`` falls back to the event type when it cannot
identify a scope, so an unfamiliar envelope stays serial rather than being
parallelised on a guess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Literal

from redis import exceptions as redis_exceptions

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.events import Event, rebuild_event
from pocketpaw_ee.cloud._core.redis_client import get_redis

logger = logging.getLogger(__name__)

XPROC_STREAM = "cloud:xproc:events"
XPROC_GROUP = "cloud-web"
XPROC_BLOCK_MS = 15000
XPROC_BATCH = 64
# Cap the stream so a stalled consumer can't grow Redis unbounded; ~10k
# entries is roughly an hour of busy traffic and survives short outages.
XPROC_MAXLEN = 10000

_ROLE: Literal["web", "worker"] = "web"


def set_role(role: Literal["web", "worker"]) -> None:
    """Pin the current process role. Call once at startup: ``worker`` from
    the arq worker's ``_startup`` hook, ``web`` (default) everywhere else."""
    global _ROLE
    _ROLE = role


def is_worker() -> bool:
    return _ROLE == "worker"


# --- publish (worker side) --------------------------------------------------


async def publish_bus_envelope(event: Event) -> None:
    """Worker → web: ship a bus event for ``bus.publish`` on the web side."""
    if not is_worker():
        return
    envelope = {
        "kind": "bus",
        "type": event.type,
        "data": event.data,
        "ts": event.ts.isoformat(),
    }
    try:
        await _xadd(envelope)
    except Exception:
        # Best-effort delivery, like the local bus. A dropped envelope means
        # one missed downstream side-effect — bad, but not worse than today's
        # Tier 2 behavior where the same event is silently dropped.
        logger.exception("xproc.publish_bus_envelope failed for %s", event.type)


async def publish_ws_envelope(
    *,
    scope_id: str,
    recipients: list[str],
    ws_type: str,
    ws_data: dict,
) -> None:
    """Worker → web: ship a WS broadcast for ``manager.broadcast_to_group``."""
    if not is_worker():
        return
    envelope = {
        "kind": "ws",
        "scope_id": scope_id,
        "recipients": list(recipients),
        "type": ws_type,
        "data": ws_data,
    }
    try:
        await _xadd(envelope)
    except Exception:
        logger.exception("xproc.publish_ws_envelope failed for %s", ws_type)


async def _xadd(envelope: dict) -> None:
    redis = get_redis()
    await redis.xadd(
        XPROC_STREAM,
        {"envelope": json.dumps(envelope)},
        maxlen=XPROC_MAXLEN,
        approximate=True,
    )


# --- consume (web side) -----------------------------------------------------


async def run_consumer(
    *,
    consumer_name: str | None = None,
    block_ms: int = XPROC_BLOCK_MS,
) -> None:
    """Long-running task: read envelopes and dispatch to local bus + manager.

    Idempotent re consumer-group creation (BUSYGROUP is swallowed); resilient
    to transient Redis/dispatch errors (logged, brief backoff, loop continues).
    Cancellation propagates out so the lifecycle hook can stop it cleanly.
    """
    redis = get_redis()
    try:
        await redis.xgroup_create(XPROC_STREAM, XPROC_GROUP, id="$", mkstream=True)
    except redis_exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    name = consumer_name or f"web-{uuid.uuid4().hex[:8]}"
    logger.info("xproc consumer %s starting on %s", name, XPROC_STREAM)

    # Exponential backoff so a Redis outage doesn't spam tracebacks.
    backoff_seconds = 1.0
    while True:
        try:
            resp = await redis.xreadgroup(
                XPROC_GROUP,
                name,
                {XPROC_STREAM: ">"},
                count=XPROC_BATCH,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("xproc consumer xreadgroup failed; backing off %.1fs", backoff_seconds)
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2.0, 10.0)
            continue

        backoff_seconds = 1.0
        if not resp:
            continue

        # Dispatch the batch by ORDERING LANE: envelopes that share a lane run
        # one after another, different lanes run at the same time.
        #
        # The old loop awaited every envelope in turn, so one slow dispatch
        # held up every envelope behind it — and because this is the single
        # bridge for all worker-originated realtime traffic, "behind it" means
        # every other tenant on the box. One workspace with a wedged socket
        # made agent replies look frozen for every customer.
        #
        # A plain gather over the batch would fix that and break something
        # worse. These envelopes carry streamed agent output: the chunks of one
        # reply must arrive in the order they were produced, or the answer
        # renders scrambled. Concurrency is therefore only ever ACROSS lanes.
        lanes: dict[tuple[str, str], list[tuple[str, dict | None]]] = {}
        for _key, entries in resp:
            for entry_id, fields in entries:
                try:
                    envelope = json.loads(fields["envelope"])
                except Exception:
                    logger.exception("xproc: unparseable envelope in entry %s", entry_id)
                    # Still needs an ack, in its own lane so it cannot delay
                    # anything real.
                    lanes.setdefault(("bad", entry_id), []).append((entry_id, None))
                    continue
                lanes.setdefault(_ordering_lane(envelope), []).append((entry_id, envelope))

        await asyncio.gather(*(_run_lane(redis, entries) for entries in lanes.values()))


#: Fields, in priority order, that identify "the conversation this envelope
#: belongs to" on a bus envelope. A bus envelope carries no explicit scope —
#: the audience is resolved later from ``data`` — so the lane is read from the
#: same identifiers the audience resolver uses.
_BUS_SCOPE_FIELDS = ("group_id", "scope_id", "session_id", "session_key", "run_id", "pocket_id")


def _ordering_lane(envelope: dict) -> tuple[str, str]:
    """The lane an envelope must stay ordered within.

    Two envelopes sharing a lane are dispatched in stream order. Envelopes in
    different lanes may overtake each other, which is the entire point.

    Conservative by construction: when no scope can be identified, the lane
    falls back to the event TYPE rather than to something unique. That keeps
    same-type envelopes serial — the old behaviour — instead of silently
    parallelising a stream whose ordering requirements we could not read.
    Getting this wrong does not raise; it scrambles a user's reply.
    """
    kind = envelope.get("kind")
    if kind == "ws":
        return ("ws", str(envelope.get("scope_id", "")))
    if kind == "bus":
        data = envelope.get("data") or {}
        if isinstance(data, dict):
            for field in _BUS_SCOPE_FIELDS:
                value = data.get(field)
                if value:
                    return ("bus", str(value))
        return ("bus-type", str(envelope.get("type", "")))
    # Unknown kinds are forward-compatible no-ops in _dispatch, but keep them
    # in one lane so a newer worker's stream can't fan out unbounded.
    return ("unknown", str(kind))


async def _run_lane(redis, entries: list[tuple[str, dict | None]]) -> None:
    """Dispatch one lane's envelopes in order, acking each as it finishes."""
    for entry_id, envelope in entries:
        try:
            if envelope is not None:
                await _dispatch(envelope)
        except Exception:
            logger.exception("xproc dispatch failed for entry %s", entry_id)
        finally:
            # Always ack — a bad envelope must not stall the stream. Worst
            # case the side-effect is missed once and the user observes a
            # one-off glitch; better than blocking forever.
            try:
                await redis.xack(XPROC_STREAM, XPROC_GROUP, entry_id)
            except Exception:
                logger.debug("xproc xack failed for %s", entry_id, exc_info=True)


async def _dispatch(envelope: dict) -> None:
    kind = envelope.get("kind")
    if kind == "bus":
        event = rebuild_event(envelope)
        await get_bus().publish(event)
    elif kind == "ws":
        # Lazy import: the chat WS module pulls FastAPI types we don't want
        # to load when the consumer isn't actually dispatching WS frames.
        from pocketpaw_ee.cloud.chat.schemas import WsOutbound
        from pocketpaw_ee.cloud.chat.ws import manager

        await manager.broadcast_to_group(
            envelope["scope_id"],
            envelope.get("recipients", []),
            WsOutbound(type=envelope["type"], data=envelope.get("data", {})),
        )
    else:
        # Forward-compatible: skip envelopes from a newer worker rather than
        # crashing the consumer.
        logger.warning("xproc consumer: unknown envelope kind %r", kind)


def _reset_for_tests() -> None:
    global _ROLE
    _ROLE = "web"
