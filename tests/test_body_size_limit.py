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


def _scope(
    headers: list[tuple[bytes, bytes]],
    method: str = "POST",
    path: str = "/api/v1/uploads",
):
    return {
        "type": "http",
        "method": method,
        "path": path,
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


class TestMultipartRelayPartCeiling:
    """The one route with its own ceiling (feat/uploads-multipart-endpoints).

    ``PUT /api/v1/uploads/multipart/{id}/parts/{n}`` carries one bare storage
    part, so the global ceiling — derived from a 50-file multipart batch — is
    the wrong shape for it. The branch is matched on SHAPE, and a near-miss
    falls back to the global ceiling rather than to no ceiling: the opposite of
    the rotted allowlists the module header argues against, where a miss
    silently removes a bound.

    Mutations: tests/mutations/uploads_multipart_api.json.
    """

    async def test_a_part_gets_its_own_ceiling_rather_than_the_global_one(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10**9, relay_part_bytes=100)
        scope = _scope(
            [(b"content-length", b"5000")],
            method="PUT",
            path="/api/v1/uploads/multipart/mpu_abc/parts/3",
        )

        sent = await _drive(mw, scope, [b"x" * 50])

        assert _status(sent) == 413
        assert app.called is False, (
            "the relay route was measured against the global ceiling, so a 5 GB "
            "part would reach the handler"
        )

    async def test_a_legitimate_part_reaches_the_app(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10, relay_part_bytes=10_000)
        scope = _scope(
            [(b"content-length", b"50")],
            method="PUT",
            path="/api/v1/uploads/multipart/mpu_abc/parts/3",
        )

        sent = await _drive(mw, scope, [b"x" * 50])

        assert _status(sent) == 200
        assert app.body_bytes == 50, "the exemption is not applied to the relay route"

    async def test_anything_that_is_not_the_relay_route_gets_the_global_ceiling(self):
        """Fail-closed: a POST to that shape, a renamed path, or the simple
        upload route must all keep the global bound."""
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10, relay_part_bytes=10_000)
        cases = [
            ("POST", "/api/v1/uploads/multipart/mpu_abc/parts/3"),
            ("PUT", "/api/v1/uploads/multipart/mpu_abc/chunks/3"),
            ("PUT", "/api/v1/uploads/multipart/mpu_abc"),
            ("PUT", "/api/v1/uploads"),
        ]
        for method, path in cases:
            scope = _scope([(b"content-length", b"50")], method=method, path=path)
            assert _status(await _drive(mw, scope, [b"x" * 50])) == 413, f"{method} {path}"

    def test_the_global_ceiling_is_not_raised_to_cover_large_uploads(self):
        """``max_file_bytes`` must stay at 25 MiB. Raising it to 5 GiB to make
        large uploads work would lift the GLOBAL ceiling to ~250 GB, which is
        precisely the bug this module exists to prevent."""
        from pocketpaw.security.body_limit import _configured_ceiling
        from pocketpaw.uploads.config import UploadSettings

        assert UploadSettings().max_file_bytes == 25 * 1024 * 1024
        assert _configured_ceiling() < 2 * 1024 * 1024 * 1024


class TestSiteDataCsvPreviewCeiling:
    """``POST /api/v1/sites/by-pocket/{id}/data/tables/{table}/import:preview`` takes
    one CSV of at most 5 MB, so it gets a ~6 MiB ceiling of its own instead of the
    ~1.27 GB global one: without it a huge multipart body is spooled to disk before
    the route (or even auth) runs. Matched on shape; a miss keeps the global bound.

    Mutations: tests/mutations/site_data_import_restore.json.
    """

    _PATH = "/api/v1/sites/by-pocket/pk_1/data/tables/orders/import:preview"

    async def test_an_oversized_csv_upload_is_refused_before_the_app(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10**9)
        declared = str(7 * 1024 * 1024).encode()
        sent = await _drive(mw, _scope([(b"content-length", declared)], path=self._PATH), [b"x"])
        assert _status(sent) == 413 and app.called is False

    async def test_a_chunked_oversized_csv_upload_is_refused(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10**9)
        chunks = [b"x" * (1024 * 1024)] * 7
        sent = await _drive(mw, _scope([], path=self._PATH), chunks)
        assert _status(sent) == 413

    async def test_a_five_mb_csv_upload_reaches_the_app(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        body = b"x" * (5 * 1024 * 1024 + 4096)  # the 5 MB file plus multipart framing
        declared = str(len(body)).encode()
        sent = await _drive(mw, _scope([(b"content-length", declared)], path=self._PATH), [body])
        assert _status(sent) == 200 and app.body_bytes == len(body)

    async def test_other_routes_keep_the_global_ceiling(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        for method, path in [
            ("GET", self._PATH),
            ("POST", "/api/v1/sites/by-pocket/pk_1/data/tables/orders/import"),
            ("POST", "/api/v1/sites/by-pocket/pk_1/data/tables/a/b/import:preview"),
        ]:
            scope = _scope([(b"content-length", b"50")], method=method, path=path)
            assert _status(await _drive(mw, scope, [b"x" * 50])) == 413, f"{method} {path}"


class TestSiteDataFileUploadCeiling:
    """``PUT /api/v1/sites/by-pocket/{id}/data/files/{binding}/object`` takes one raw
    file of at most 50 MiB, so it gets a 51 MiB ceiling instead of the global one.
    Matched on shape (method and path); a miss keeps the global bound.

    Mutations: tests/mutations/site_data_files_kv.json.
    """

    _PATH = "/api/v1/sites/by-pocket/pk_1/data/files/SITE_FILES/object"

    async def test_an_oversized_upload_is_refused_before_the_app(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10**10)
        declared = str(52 * 1024 * 1024).encode()
        scope = _scope([(b"content-length", declared)], method="PUT", path=self._PATH)
        sent = await _drive(mw, scope, [b"x"])
        assert _status(sent) == 413 and app.called is False

    async def test_a_chunked_oversized_upload_is_refused(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10**10)
        chunks = [b"x" * (4 * 1024 * 1024)] * 13
        sent = await _drive(mw, _scope([], method="PUT", path=self._PATH), chunks)
        assert _status(sent) == 413

    async def test_a_fifty_mib_upload_reaches_the_app(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        size = 50 * 1024 * 1024
        scope = _scope([(b"content-length", str(size).encode())], method="PUT", path=self._PATH)
        sent = await _drive(mw, scope, [b"x" * size])
        assert _status(sent) == 200 and app.body_bytes == size

    async def test_other_routes_keep_the_global_ceiling(self):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=10)
        for method, path in [
            ("POST", self._PATH),
            ("PUT", "/api/v1/sites/by-pocket/pk_1/data/files/a/b/object"),
            ("PUT", "/api/v1/sites/by-pocket/pk_1/data/files/SITE_FILES/object:move"),
        ]:
            scope = _scope([(b"content-length", b"50")], method=method, path=path)
            assert _status(await _drive(mw, scope, [b"x" * 50])) == 413, f"{method} {path}"


class TestSiteDataKvAndMoveCeilings:
    """The Sites Data KV write (``PUT …/data/kv/{binding}/keys/{key}``, a 1 MB value
    as JSON) gets 3 MiB, and a file move (``POST …/data/files/{binding}/object:move``,
    two keys) gets 64 KiB, instead of the global ceiling.

    Mutations: tests/mutations/site_data_files_kv.json.
    """

    _KV = "/api/v1/sites/by-pocket/pk_1/data/kv/CACHE/keys/a/b"
    _MOVE = "/api/v1/sites/by-pocket/pk_1/data/files/SITE_FILES/object:move"

    async def _status_for(self, method, path, size, *, max_bytes=10**10):
        app = _RecordingApp()
        mw = BodySizeLimitMiddleware(app, max_bytes=max_bytes)
        scope = _scope([(b"content-length", str(size).encode())], method=method, path=path)
        return _status(await _drive(mw, scope, [b"x" * size])), app

    async def test_an_oversized_kv_write_is_refused(self):
        status, app = await self._status_for("PUT", self._KV, 3 * 1024 * 1024 + 1)
        assert status == 413 and app.called is False

    async def test_a_three_mib_kv_write_reaches_the_app(self):
        status, _ = await self._status_for("PUT", self._KV, 3 * 1024 * 1024, max_bytes=10)
        assert status == 200

    async def test_an_oversized_move_is_refused(self):
        status, app = await self._status_for("POST", self._MOVE, 64 * 1024 + 1)
        assert status == 413 and app.called is False

    async def test_a_small_move_reaches_the_app(self):
        status, _ = await self._status_for("POST", self._MOVE, 2048, max_bytes=10)
        assert status == 200

    async def test_other_methods_keep_the_global_ceiling(self):
        for method, path in [("GET", self._KV), ("PUT", self._MOVE), ("DELETE", self._KV)]:
            status, _ = await self._status_for(method, path, 50, max_bytes=10)
            assert status == 413, f"{method} {path}"
