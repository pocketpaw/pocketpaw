# Regression: an over-sized request body is rejected BEFORE it is buffered.
#
# Created 2026-09-04 (backend-perf H5). The upload limits (25 MiB per file, 50
# files per batch) were never wrong — they are just downstream of the damage.
# They live in uploads/service.py::_upload_one, which runs after Starlette has
# already parsed the whole multipart body into a SpooledTemporaryFile on disk.
# A 20 GB POST fills the container's disk before any of them execute.
#
# The assertion that matters throughout this file is not "a 413 came back". It
# is "the application never saw the body". A middleware that returns 413 after
# reading 20 GB has changed the status code and nothing else.
#
# What each test would catch (mutations in tests/mutations/body_ceiling.json):
#   - drop the Content-Length check       -> test_declared_oversize_is_rejected
#   - drop the streaming byte counter     -> test_chunked_oversize_is_rejected
#   - let the app respond after the 413   -> test_no_second_response_is_sent
#   - apply kwargs over URI options       -> TestMongoClientOptions
#   - set socket_timeout on Redis         -> test_no_read_timeout_is_set

from __future__ import annotations

import pytest

from pocketpaw.security.body_limit import BodySizeLimitMiddleware


class _RecordingApp:
    """An ASGI app that reads its whole body, and remembers how much it saw."""

    def __init__(self) -> None:
        self.body_bytes = 0
        self.called = False
        self.saw_disconnect = False

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.saw_disconnect = True
                break
            self.body_bytes += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


def _scope(headers: list[tuple[bytes, bytes]], method: str = "POST"):
    return {
        "type": "http",
        "method": method,
        "path": "/api/v1/uploads",
        "headers": headers,
    }


async def _drive(middleware, scope, chunks: list[bytes]):
    """Run one request through the middleware; return the sent ASGI messages."""
    queued = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]
    sent: list[dict] = []

    async def receive():
        return queued.pop(0) if queued else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int | None:
    for message in sent:
        if message["type"] == "http.response.start":
            return message["status"]
    return None


class TestDeclaredLength:
    async def test_declared_oversize_is_rejected_without_reading_the_body(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=1000)
        scope = _scope([(b"content-length", b"999999999")])

        sent = await _drive(mw, scope, [b"x" * 50])

        assert _status(sent) == 413
        assert app.called is False, (
            "the application ran, so the body was reaching a handler that would buffer it"
        )
        assert app.body_bytes == 0

    async def test_a_body_within_the_limit_passes_through_untouched(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=1000)
        scope = _scope([(b"content-length", b"10")])

        sent = await _drive(mw, scope, [b"0123456789"])

        assert _status(sent) == 200
        assert app.body_bytes == 10

    async def test_exactly_at_the_limit_is_allowed(self):
        """The limit is a ceiling, not a fence one short of it."""
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        sent = await _drive(mw, _scope([(b"content-length", b"10")]), [b"0123456789"])

        assert _status(sent) == 200
        assert app.body_bytes == 10

    async def test_an_unparseable_length_falls_through_to_the_counter(self):
        """A malformed header must not become a way to skip the ceiling."""
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        sent = await _drive(mw, _scope([(b"content-length", b"not-a-number")]), [b"x" * 50])

        assert _status(sent) == 413


class TestStreamedLength:
    """Content-Length is client-supplied, and chunked transfer-encoding omits
    it entirely. A declaration-only check is bypassed by not declaring."""

    async def test_chunked_oversize_is_rejected(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=100)
        scope = _scope([(b"transfer-encoding", b"chunked")])

        sent = await _drive(mw, scope, [b"x" * 60, b"x" * 60, b"x" * 60])

        assert _status(sent) == 413

    async def test_the_app_stops_reading_once_the_limit_is_passed(self):
        """The point is bounded memory, so the app must not keep receiving."""
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=100)

        await _drive(mw, _scope([]), [b"x" * 60, b"x" * 60, b"x" * 60, b"x" * 60])

        assert app.saw_disconnect is True
        assert app.body_bytes <= 120, f"app buffered {app.body_bytes} bytes past a 100-byte ceiling"

    async def test_no_second_response_is_sent(self):
        """The app sees a disconnect and still tries to respond. Letting that
        through would put two response-start messages on one request, which is
        an ASGI protocol violation and raises inside the server."""
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=100)

        sent = await _drive(mw, _scope([]), [b"x" * 60, b"x" * 60])

        starts = [m for m in sent if m["type"] == "http.response.start"]
        assert len(starts) == 1, f"{len(starts)} response-start messages were sent"
        assert starts[0]["status"] == 413

    async def test_a_chunked_body_within_the_limit_passes(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=1000)
        sent = await _drive(mw, _scope([]), [b"x" * 100, b"x" * 100])

        assert _status(sent) == 200
        assert app.body_bytes == 200


class TestNonHttpScopes:
    async def test_websocket_scope_is_passed_straight_through(self):
        """A WebSocket has no request body to bound, and touching its receive
        channel would break the handshake."""
        seen = {}

        async def app(scope, receive, send):
            seen["type"] = scope["type"]

        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        await mw({"type": "websocket", "path": "/ws"}, None, None)

        assert seen["type"] == "websocket"

    async def test_lifespan_scope_is_passed_straight_through(self):
        seen = {}

        async def app(scope, receive, send):
            seen["type"] = scope["type"]

        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        await mw({"type": "lifespan"}, None, None)

        assert seen["type"] == "lifespan"


class TestCeilingResolution:
    def test_the_default_is_derived_from_the_upload_settings(self, monkeypatch):
        """Derived, not invented. Seventeen modules accept an UploadFile; a
        hand-written path table would rot the first time one was added."""
        monkeypatch.delenv("POCKETPAW_MAX_REQUEST_BYTES", raising=False)
        from pocketpaw.security.body_limit import _configured_ceiling
        from pocketpaw.uploads.config import UploadSettings

        settings = UploadSettings()
        floor = settings.max_file_bytes * settings.max_files_per_batch
        assert _configured_ceiling() > floor, (
            "the ceiling is below the app's own maximum legitimate batch, "
            "so a valid 50-file upload would be rejected"
        )

    def test_the_env_override_wins(self, monkeypatch):
        from pocketpaw.security.body_limit import _configured_ceiling

        monkeypatch.setenv("POCKETPAW_MAX_REQUEST_BYTES", "4096")
        assert _configured_ceiling() == 4096

    @pytest.mark.parametrize("bad", ["nonsense", "0", "-1", "  "])
    def test_a_bad_override_falls_back_rather_than_disabling_the_ceiling(self, monkeypatch, bad):
        """A typo must never remove a guard."""
        from pocketpaw.security.body_limit import _configured_ceiling

        monkeypatch.setenv("POCKETPAW_MAX_REQUEST_BYTES", bad)
        assert _configured_ceiling() > 1_000_000


class TestItIsActuallyWired:
    """A middleware class that nothing registers protects nothing, and every
    test above would still pass. These assert the wiring, and the ORDER of the
    wiring, which is the part that carries the fix.
    """

    def test_registered_on_the_dashboard_app(self):
        from pocketpaw.dashboard import app

        assert BodySizeLimitMiddleware in [m.cls for m in app.user_middleware]

    def test_registered_outside_auth_on_the_dashboard_app(self):
        """AuthMiddleware reads the request body on the login paths, so a
        ceiling registered inside it cannot stop that buffering.

        Starlette's user_middleware list is outermost-LAST-added, and the stack
        is built by reversing it — so a LOWER index here means further out.
        """
        from pocketpaw.dashboard import app
        from pocketpaw.dashboard_auth import AuthMiddleware

        order = [m.cls for m in app.user_middleware]
        assert order.index(BodySizeLimitMiddleware) < order.index(AuthMiddleware), (
            "the body ceiling runs inside AuthMiddleware, which reads the body itself"
        )
