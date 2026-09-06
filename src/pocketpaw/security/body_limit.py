# src/pocketpaw/security/body_limit.py
# Created: 2026-09-04 (fix/pool-and-body-ceilings, backend-perf H5) — a hard
# ceiling on request bodies, enforced BEFORE the body is read.
#
# The gap this closes: `max_file_bytes` (25 MiB) and `max_files_per_batch` (50)
# are real limits, but they live in `uploads/service.py::_upload_one`, which
# runs AFTER Starlette has already parsed the whole multipart body into a
# SpooledTemporaryFile on disk. A 20 GB POST to /api/v1/uploads therefore fills
# the container's disk before any of those checks execute. The limit was never
# wrong; it was just downstream of the damage.
#
# Two things this deliberately does NOT do, so nobody mistakes it for a
# complete fix:
#   1. It does not make concurrent uploads safe. A legitimate 50-file batch is
#      still ~1.25 GB spooled to disk, and ten at once is still ~12.5 GB. That
#      needs streaming straight to object storage plus a concurrency cap, which
#      is a larger change (audit H5, "M for S3 multipart").
#   2. It is not a per-route policy. See the note on the ceiling below.

"""Reject over-sized request bodies before they are buffered."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

#: Slack over the app's own maximum legitimate payload, for multipart framing:
#: per-part headers, boundaries, and the trailing epilogue. 16 MiB is far more
#: than any real client adds and costs nothing when the ceiling is only ever
#: consulted to reject the absurd.
_MULTIPART_OVERHEAD_BYTES = 16 * 1024 * 1024


def _configured_ceiling() -> int:
    """The maximum body this process will accept, in bytes.

    DERIVED from the upload settings rather than invented, and deliberately a
    single global number rather than a per-path table.

    Why not per-path: seventeen modules in this repo accept an ``UploadFile``,
    across both packages. A path allowlist would have to name each of them, and
    a route missing from that list breaks silently the first time somebody
    uploads to it. This session has already found two hand-maintained lists
    that had rotted exactly that way — the v1 route-auth audit list, and the
    rate-limiter sweep list that structurally could not name the cloud
    limiters. A derived global ceiling has nothing to forget.

    What it is for, stated plainly: stopping a body that no route in this
    application could ever legitimately accept, before it is written to disk.
    The per-route limits downstream stay exactly as they are and remain the
    thing that decides whether a 30 MiB file is allowed.

    ``POCKETPAW_MAX_REQUEST_BYTES`` overrides. A malformed or non-positive
    value falls back to the derived default rather than disabling the ceiling,
    because a typo must not silently remove a guard.
    """
    raw = os.environ.get("POCKETPAW_MAX_REQUEST_BYTES", "").strip()
    if raw:
        try:
            val = int(raw)
        except ValueError:
            logger.warning(
                "POCKETPAW_MAX_REQUEST_BYTES=%r is not an int; using the derived default", raw
            )
        else:
            if val > 0:
                return val
            logger.warning(
                "POCKETPAW_MAX_REQUEST_BYTES=%d is not positive; using the derived default", val
            )

    # Imported lazily: this module is installed on the app at construction
    # time, and the upload config pulls in the wider settings graph.
    from pocketpaw.uploads.config import UploadSettings

    settings = UploadSettings()
    return settings.max_file_bytes * settings.max_files_per_batch + _MULTIPART_OVERHEAD_BYTES


class BodySizeLimitMiddleware:
    """Pure-ASGI ceiling on request body size.

    Pure ASGI, not ``BaseHTTPMiddleware``, on purpose. Four
    ``BaseHTTPMiddleware`` layers already wrap every cloud request, each one
    costing an anyio task group and a memory object stream per request (audit
    M8). This one adds a couple of dict lookups instead, and it has to sit
    outermost to be worth anything — a ceiling that runs after the body is
    parsed is the bug it exists to fix.
    """

    def __init__(self, app, max_bytes: int | None = None) -> None:
        self.app = app
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        # Resolved on first use, not in __init__: the app is constructed at
        # import time in dashboard.py, before the environment is necessarily
        # loaded, so reading the override eagerly would miss it.
        if self._max_bytes is None:
            self._max_bytes = _configured_ceiling()
        return self._max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            # WebSocket and lifespan have no request body to bound.
            await self.app(scope, receive, send)
            return

        limit = self.max_bytes

        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            # The cheap path, and the one that matters: reject on the client's
            # own declaration, before a single byte of body is read.
            logger.warning(
                "rejecting over-sized request: declared %d bytes > limit %d (%s %s)",
                declared,
                limit,
                scope.get("method", "?"),
                scope.get("path", "?"),
            )
            await _send_too_large(send, limit)
            return

        # No Content-Length means chunked transfer-encoding, which is exactly
        # how a declaration check alone is bypassed. Count as the body arrives.
        state = {"seen": 0, "responded": False}

        async def _receive() -> Message:
            message = await receive()
            if message.get("type") != "http.request":
                return message
            state["seen"] += len(message.get("body", b""))
            if state["seen"] > limit:
                if not state["responded"]:
                    state["responded"] = True
                    logger.warning(
                        "rejecting over-sized request: streamed past limit %d (%s %s)",
                        limit,
                        scope.get("method", "?"),
                        scope.get("path", "?"),
                    )
                    await _send_too_large(send, limit)
                # Tell the app the client is gone. Returning a disconnect
                # rather than raising keeps this independent of how any given
                # body parser handles exceptions — several of them catch
                # broadly and would turn a raise into a 500, which would report
                # the wrong thing to the client and to the logs.
                return {"type": "http.disconnect"}
            return message

        async def _send(message: Message) -> None:
            # Once we have answered, swallow whatever the app tries to send.
            # The app sees a disconnect and may still attempt a response;
            # letting it through would be a second set of response-start
            # headers on one request.
            if state["responded"]:
                return
            await send(message)

        await self.app(scope, _receive, _send)


def _declared_length(scope: Scope) -> int | None:
    """Parse ``Content-Length``, or None when absent or unparseable.

    An unparseable value is treated as absent rather than as a rejection: the
    streaming counter below still bounds it, and the server itself will reject
    a malformed framing header on its own terms.
    """
    for key, value in scope.get("headers", []):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_too_large(send: Send, limit: int) -> None:
    body = (
        b'{"detail":"Request body too large. Limit is '
        + str(limit).encode()
        + b' bytes.","error":{"code":"request.body_too_large"}}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # Nothing about this decision depends on the body, so a client
                # retrying the identical request gets the identical answer.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["BodySizeLimitMiddleware"]
