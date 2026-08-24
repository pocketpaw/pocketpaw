# tests/ee/sites/test_artifact_store_s3.py — SP-4, the SHARED native-artifact store.
# Created 2026-08-24 (feat/sites-s3-artifact-store). New file.
#
# Under test: ``pocketpaw_ee.sites.artifact_store_s3`` — the blob-storage-backed
# ``_store`` seam that lets an artifact built on one replica be served by another and
# survive a redeploy. The filesystem store it stands beside is per-container, so a view
# routed elsewhere pays a full ``bun install`` + build.
#
# The adapter is FAKED (a dict, not boto3): these prove the store's own contract, which
# is entirely about what it does when the backend behaves and when it does not. The real
# ``S3StorageAdapter`` is covered by the uploads tests.
#
# THE ASYNC-FROM-SYNC LEG IS NOT INCIDENTAL. ``get_native_artifact`` and the pre-warm
# call ``store.read(...)`` / ``store.write(...)`` with no ``await``, from inside a
# running event loop, while ``StorageAdapter`` is async throughout. A bridge that only
# works when no loop is running would pass a naive unit test and then raise
# "asyncio.run() cannot be called from a running event loop" on the first real preview —
# so ``TestFromInsideARunningLoop`` exercises both halves in an async test, which is the
# production call shape.
#
# What these prove:
#   * round-trip — a write then a read returns the same (body_html, css);
#   * the key is ``(pocket_id, content_hash)``, the filesystem store's pair, unchanged;
#   * all three fallback arms degrade to a rebuild and never raise: a cold MISS, a
#     CORRUPT object, and a FAILED WRITE (plus a hung backend on either side);
#   * THE CAPTURE KEY NEVER REACHES BLOB STORAGE — a rendered payload carrying a
#     ``site_key_...`` value is refused outright rather than stored, from either field;
#   * an ordinary artifact is not caught by that guard (it is a real cache, not a
#     permanently-closed one);
#   * selection is env-driven and DEFAULTS to the filesystem store, so OSS installs and
#     local dev are unchanged, and a box that cannot build an adapter falls back rather
#     than failing a preview.

from __future__ import annotations

import asyncio
import json
import secrets

import pytest
from pocketpaw_ee.sites import artifact_store_s3 as mod
from pocketpaw_ee.sites import service as sites_service

BODY = "<section data-uid='Hero:0'><h1>Hello</h1></section>"
CSS = ".hero{color:red}"


class _FakeAdapter:
    """The two ``StorageAdapter`` methods the artifact store actually uses, in memory.

    ``open`` is an async GENERATOR, like the real S3 adapter's — so a missing key raises
    on the first ``__anext__``, not at the call. Getting that wrong in the fake would
    hide a bridge that never iterates the stream.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[tuple[str, str]] = []
        self.put_error: Exception | None = None
        self.open_error: Exception | None = None
        self.delay_sec: float = 0.0

    async def put(self, key: str, stream, mime: str):  # noqa: ANN001 — duck-typed seam
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.put_error is not None:
            raise self.put_error
        buf = b""
        async for chunk in stream:
            buf += chunk
        self.objects[key] = buf
        self.puts.append((key, mime))

    async def open(self, key: str):
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.open_error is not None:
            raise self.open_error
        if key not in self.objects:
            from pocketpaw.uploads.errors import NotFound

            raise NotFound(f"missing: {key}")
        yield self.objects[key]


@pytest.fixture(autouse=True)
def _clean_selection(monkeypatch):
    """Neither env knob may be inherited from the developer's shell, and the process
    adapter cache must not leak between cases that flip them."""
    monkeypatch.delenv("PAW_SITES_ARTIFACT_STORE", raising=False)
    monkeypatch.delenv("POCKETPAW_UPLOAD_ADAPTER", raising=False)
    monkeypatch.delenv("PAW_SITES_ARTIFACT_S3_TIMEOUT_SEC", raising=False)
    mod.reset_shared_adapter()
    yield
    mod.reset_shared_adapter()


@pytest.fixture
def store_and_adapter():
    adapter = _FakeAdapter()
    return mod.S3ArtifactStore(adapter), adapter


class TestTheRoundTrip:
    def test_a_write_then_a_read_returns_the_same_artifact(self, store_and_adapter):
        store, adapter = store_and_adapter

        store.write("pocketA", "hash1", BODY, CSS)
        assert store.read("pocketA", "hash1") == (BODY, CSS)
        assert adapter.puts == [("site-artifacts/pocketA/hash1.json", "application/json")]

    def test_the_key_is_the_pocket_id_and_content_hash_pair(self, store_and_adapter):
        """The filesystem store writes ``<pocket_id>/<hash>.json``. Changing the pair, or
        its order, would make the two stores address different artifacts and silently
        invalidate every cached render on a backend switch."""
        store, adapter = store_and_adapter

        store.write("pocketA", "hash1", BODY, CSS)

        assert "site-artifacts/pocketA/hash1.json" in adapter.objects
        assert mod.artifact_key("pocketA", "hash1") == "site-artifacts/pocketA/hash1.json"

    def test_the_stored_payload_is_the_filesystem_stores_shape(self, store_and_adapter):
        """``{body_html, css, stored_at}`` — the same JSON ``_FilesystemArtifactStore``
        writes, so the two are interchangeable for anything that reads an artifact."""
        store, adapter = store_and_adapter

        store.write("pocketA", "hash1", BODY, CSS)

        data = json.loads(adapter.objects["site-artifacts/pocketA/hash1.json"])
        assert data["body_html"] == BODY
        assert data["css"] == CSS
        assert data["stored_at"]

    def test_two_pockets_do_not_collide_on_a_shared_hash(self, store_and_adapter):
        """Identical source in two pockets hashes identically; the pocket id is what
        keeps one tenant's artifact out of another's read."""
        store, _ = store_and_adapter

        store.write("pocketA", "same", "<b>A</b>", ".a{}")
        store.write("pocketB", "same", "<b>B</b>", ".b{}")

        assert store.read("pocketA", "same") == ("<b>A</b>", ".a{}")
        assert store.read("pocketB", "same") == ("<b>B</b>", ".b{}")


class TestEveryFailureDegradesToARebuild:
    """Best-effort on both sides, matching the filesystem store: a read returns None so
    the caller rebuilds, and a write is swallowed so the render still returns."""

    def test_a_cold_key_is_a_miss_not_an_error(self, store_and_adapter):
        store, _ = store_and_adapter
        assert store.read("pocketA", "never-written") is None

    def test_a_corrupt_object_is_a_miss(self, store_and_adapter):
        store, adapter = store_and_adapter
        adapter.objects["site-artifacts/pocketA/hash1.json"] = b"{ not json"

        assert store.read("pocketA", "hash1") is None

    def test_an_object_missing_a_field_is_a_miss(self, store_and_adapter):
        store, adapter = store_and_adapter
        adapter.objects["site-artifacts/pocketA/hash1.json"] = json.dumps(
            {"body_html": BODY}
        ).encode()

        assert store.read("pocketA", "hash1") is None

    def test_a_non_string_field_is_a_miss(self, store_and_adapter):
        """A JSON object of the right shape but the wrong types would otherwise be handed
        to a caller that unpacks it into two strings."""
        store, adapter = store_and_adapter
        adapter.objects["site-artifacts/pocketA/hash1.json"] = json.dumps(
            {"body_html": BODY, "css": {"not": "a string"}}
        ).encode()

        assert store.read("pocketA", "hash1") is None

    def test_an_unreachable_backend_reads_as_a_miss(self, store_and_adapter):
        store, adapter = store_and_adapter
        adapter.open_error = RuntimeError("bucket unreachable")

        assert store.read("pocketA", "hash1") is None

    def test_a_failed_write_is_swallowed(self, store_and_adapter):
        """The render has already succeeded by the time write() is called. Raising here
        would turn a healthy preview into a 500 because a cache was full."""
        store, adapter = store_and_adapter
        adapter.put_error = RuntimeError("access denied")

        store.write("pocketA", "hash1", BODY, CSS)  # must not raise

        assert adapter.objects == {}

    def test_a_hung_backend_does_not_block_forever(self, store_and_adapter):
        """The seam is sync and runs on the request's event loop, so an unbounded wait
        wedges the loop rather than slowing one preview. Both sides carry the deadline."""
        store, adapter = store_and_adapter
        adapter.delay_sec = 5.0
        store._timeout = 0.05

        assert store.read("pocketA", "hash1") is None
        store.write("pocketA", "hash1", BODY, CSS)  # must not raise
        assert adapter.objects == {}


class TestTheCaptureKeyNeverReachesBlobStorage:
    """The per-site capture key's exposure was only ever acceptable because it lives in a
    container that is then destroyed (see ``build_job`` / ``daytona_runner`` headers).
    Blob storage is durable, so the write path refuses a payload carrying one."""

    def _real_shaped_key(self) -> str:
        # The exact mint used by service._build_native_artifact and Site.rotate.
        return f"site_key_{secrets.token_urlsafe(24)}"

    def test_a_body_carrying_a_capture_key_is_never_stored(self, store_and_adapter):
        store, adapter = store_and_adapter
        key = self._real_shaped_key()
        body = (
            '<form action="/capture/form">'
            f'<input type="hidden" name="paw_key" value="{key}">'
            "</form>"
        )

        store.write("pocketA", "hash1", body, CSS)

        assert adapter.puts == [], "the artifact was uploaded instead of being refused"
        # The invariant, asserted over the bytes rather than over "nothing was written":
        # it still holds a future implementation that stores a scrubbed copy to the same
        # standard, which "objects == {}" would not.
        assert not any(key.encode() in blob for blob in adapter.objects.values()), (
            "a per-site capture key reached durable blob storage"
        )

    def test_a_stylesheet_carrying_a_capture_key_is_never_stored(self, store_and_adapter):
        """Both fields are scanned. Guarding only the field the key is expected in is how
        the next substitution site gets missed."""
        store, adapter = store_and_adapter
        css = f'.x{{content:"{self._real_shaped_key()}"}}'

        store.write("pocketA", "hash1", BODY, css)

        assert adapter.objects == {}

    def test_a_refusal_reads_back_as_a_miss_so_the_caller_rebuilds(self, store_and_adapter):
        """Refusing is not an error path — it collapses into the store's existing
        best-effort contract, which the caller already handles by rebuilding."""
        store, _ = store_and_adapter

        store.write("pocketA", "hash1", f"<p>{self._real_shaped_key()}</p>", CSS)

        assert store.read("pocketA", "hash1") is None

    def test_an_ordinary_artifact_is_not_caught_by_the_guard(self, store_and_adapter):
        """A guard that refuses everything is indistinguishable from a broken cache. The
        near-miss strings here are the ones a real page can plausibly contain."""
        store, adapter = store_and_adapter
        body = (
            "<p>Set your site_key_ in the dashboard</p>"
            "<code>site_key_short</code>"
            "<a href='/docs/site-keys'>site keys</a>"
        )

        store.write("pocketA", "hash1", body, CSS)

        assert store.read("pocketA", "hash1") == (body, CSS)

    def test_the_detector_recognises_a_real_key_anywhere_in_the_payload(self):
        key = self._real_shaped_key()
        assert mod.carries_capture_key(f"prefix {key} suffix")
        assert mod.carries_capture_key("clean", f"<i>{key}</i>")
        assert not mod.carries_capture_key(BODY, CSS)
        assert not mod.carries_capture_key("site_key_", "site_key_tooshort")


class TestFromInsideARunningLoop:
    """The production call shape: a SYNC store call made from inside a running event
    loop. ``asyncio.run`` raises there, so a bridge that skips the worker thread passes
    every test above and fails on the first real preview."""

    async def test_read_and_write_work_with_a_loop_already_running(self, store_and_adapter):
        store, _ = store_and_adapter
        assert asyncio.get_running_loop() is not None

        store.write("pocketA", "hash1", BODY, CSS)

        assert store.read("pocketA", "hash1") == (BODY, CSS)

    async def test_the_calling_loop_still_works_afterwards(self, store_and_adapter):
        """A bridge that closed or stole the caller's loop would only show up here."""
        store, _ = store_and_adapter

        store.write("pocketA", "hash1", BODY, CSS)
        await asyncio.sleep(0)

        assert store.read("pocketA", "hash1") == (BODY, CSS)


class TestSelectingTheStore:
    def test_the_default_is_the_filesystem_store(self):
        """Unset env keeps OSS installs and local dev byte-for-byte on the prior store."""
        assert mod.shared_artifact_store() is None
        assert isinstance(
            sites_service._default_artifact_store(), sites_service._FilesystemArtifactStore
        )

    def test_an_unrecognised_mode_is_the_filesystem_store(self, monkeypatch):
        monkeypatch.setenv("PAW_SITES_ARTIFACT_STORE", "gcs")
        assert mod.shared_artifact_store() is None
        assert isinstance(
            sites_service._default_artifact_store(), sites_service._FilesystemArtifactStore
        )

    def test_s3_mode_selects_the_shared_store(self, monkeypatch):
        adapter = _FakeAdapter()
        monkeypatch.setenv("PAW_SITES_ARTIFACT_STORE", "s3")
        monkeypatch.setattr(mod, "_shared_adapter", lambda: adapter)

        selected = sites_service._default_artifact_store()

        assert isinstance(selected, mod.S3ArtifactStore)
        selected.write("pocketA", "hash1", BODY, CSS)
        assert "site-artifacts/pocketA/hash1.json" in adapter.objects

    def test_mode_matching_ignores_case_and_padding(self, monkeypatch):
        monkeypatch.setenv("PAW_SITES_ARTIFACT_STORE", "  S3 ")
        monkeypatch.setattr(mod, "_shared_adapter", lambda: _FakeAdapter())

        assert isinstance(mod.shared_artifact_store(), mod.S3ArtifactStore)

    def test_an_unbuildable_adapter_falls_back_instead_of_failing_a_preview(self, monkeypatch):
        """``build_adapter`` raises when POCKETPAW_UPLOAD_ADAPTER=s3 and no bucket is
        configured. A preview must degrade to the local store, not 500."""
        monkeypatch.setenv("PAW_SITES_ARTIFACT_STORE", "s3")
        monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
        monkeypatch.delenv("S3_PRIVATE_BUCKET", raising=False)
        monkeypatch.delenv("S3_BUCKET", raising=False)

        assert mod.shared_artifact_store() is None
        assert isinstance(
            sites_service._default_artifact_store(), sites_service._FilesystemArtifactStore
        )

    def test_a_failed_adapter_build_is_not_retried_on_every_preview(self, monkeypatch):
        calls: list[int] = []

        def _boom(_root):
            calls.append(1)
            raise RuntimeError("no bucket")

        monkeypatch.setenv("PAW_SITES_ARTIFACT_STORE", "s3")
        monkeypatch.setattr("pocketpaw.uploads.factory.build_adapter", _boom)

        assert mod.shared_artifact_store() is None
        assert mod.shared_artifact_store() is None

        assert len(calls) == 1, "a box with no S3 config must not rebuild (and log) per view"
