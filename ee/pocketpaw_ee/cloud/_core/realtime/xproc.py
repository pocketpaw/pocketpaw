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

        for _key, entries in resp:
            for entry_id, fields in entries:
                try:
                    envelope = json.loads(fields["envelope"])
                    await _dispatch(envelope)
                except Exception:
                    logger.exception("xproc dispatch failed for entry %s", entry_id)
                finally:
                    # Always ack — a bad envelope must not stall the stream.
                    # Worst case the side-effect is missed once and the user
                    # observes a one-off glitch; better than blocking forever.
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
