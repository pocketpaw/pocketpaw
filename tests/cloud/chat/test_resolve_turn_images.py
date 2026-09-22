# tests/cloud/chat/test_resolve_turn_images.py — from an S3-backed upload row
# to bytes a model can look at.
#
# Created: 2026-09-15 (fix/chat-image-persistent-client).
#
# The seam nothing covered. `_fit_for_model` and `materialize_to_local_path` each
# got direct tests, but `resolve_turn_images` -- the function that strings them
# together and is the ONLY producer of `image_attachments` in the whole product
# -- had none. It is also the function whose every failure is invisible:
#
#   "Never raises -- a broken upload must not cost the user their turn."
#
# So a bug anywhere inside it returns an empty tuple and the turn proceeds. The
# user-visible result is a model that names the file and cannot see it, because
# `_build_attachments_block` resolves the record SEPARATELY and still emits its
# note. That is worth spelling out: the filename and size in that note come from
# the Mongo record, NOT from reading the blob. Seeing them proves the row was
# found. It proves nothing about whether the pixels were ever loaded.
#
# Reported as "from s3 it is able to get the image but not able to read the
# image", which is exactly that distinction.

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from pocketpaw_ee.cloud.chat import agent_service

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


def _jpeg(w: int = 640, h: int = 400) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 110, 210)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@dataclass
class _Ctx:
    workspace_id: str | None = "ws-1"


def _rec(filename: str, mime: str, size: int) -> FileRecord:
    return FileRecord(
        id="f1",
        storage_key="ws-1/f1",
        filename=filename,
        mime=mime,
        size=size,
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC),
    )


class _Resolver:
    """Stands in for EEUploadResolver: hands back (record, local path) the way
    the S3 branch does after streaming the blob into a temp file."""

    def __init__(self, rec: FileRecord | None, path: Path | None) -> None:
        self._rec = rec
        self._path = path
        self.asked: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def open_local_for_url(self, url: str, workspace: str):
        self.asked.append((url, workspace))
        if self._rec is None or self._path is None:
            yield None
        else:
            yield self._rec, self._path


def _wire(monkeypatch, resolver) -> None:
    import pocketpaw_ee.cloud.uploads.resolver as res_mod

    monkeypatch.setattr(res_mod, "default_resolver", lambda: resolver)


async def _run(
    monkeypatch,
    tmp_path: Path,
    *,
    filename="screenshot-compare.jpg",
    mime="image/jpeg",
    data=None,
    ctx=None,
    attachments=None,
):
    raw = _jpeg() if data is None else data
    p = tmp_path / filename
    p.write_bytes(raw)
    resolver = _Resolver(_rec(filename, mime, len(raw)), p)
    _wire(monkeypatch, resolver)
    return (
        await agent_service.resolve_turn_images(
            ctx or _Ctx(),
            attachments if attachments is not None else [{"url": "/api/v1/uploads/f1"}],
        ),
        resolver,
        raw,
    )


class TestTheHappyPath:
    async def test_an_s3_jpeg_becomes_an_image_the_model_can_see(
        self, monkeypatch, tmp_path
    ) -> None:
        out, _resolver, raw = await _run(monkeypatch, tmp_path)
        assert len(out) == 1, "the attached image must survive as an ImageAttachment"
        img = out[0]
        assert img.media_type == "image/jpeg"
        assert img.filename == "screenshot-compare.jpg"
        assert img.data == raw, "the bytes handed to the model must be the stored bytes"

    async def test_it_asks_the_resolver_with_this_workspace(self, monkeypatch, tmp_path) -> None:
        # Tenant scoping is enforced inside the resolver, so the workspace has
        # to actually reach it.
        _out, resolver, _raw = await _run(monkeypatch, tmp_path)
        assert resolver.asked == [("/api/v1/uploads/f1", "ws-1")]

    async def test_a_png_rides_untouched(self, monkeypatch, tmp_path) -> None:
        buf = io.BytesIO()
        Image.new("RGB", (800, 500), (9, 9, 9)).save(buf, format="PNG")
        raw = buf.getvalue()
        out, _r, _ = await _run(
            monkeypatch, tmp_path, filename="shot.png", mime="image/png", data=raw
        )
        assert len(out) == 1
        assert out[0].media_type == "image/png"
        assert out[0].data == raw


class TestTheSilentDrops:
    """Each of these returns () and lets the turn proceed. The model then names
    a file it cannot see, which is the symptom that sent us looking."""

    async def test_a_non_image_mime_is_not_offered_as_an_image(self, monkeypatch, tmp_path) -> None:
        out, _r, _ = await _run(
            monkeypatch, tmp_path, filename="report.pdf", mime="application/pdf", data=b"%PDF-1.7\n"
        )
        assert out == (), "a pdf belongs to the extraction path, not the image path"

    async def test_no_workspace_means_no_images(self, monkeypatch, tmp_path) -> None:
        out, _r, _ = await _run(monkeypatch, tmp_path, ctx=_Ctx(workspace_id=None))
        assert out == ()

    async def test_no_attachments_means_no_images(self, monkeypatch, tmp_path) -> None:
        out, _r, _ = await _run(monkeypatch, tmp_path, attachments=[])
        assert out == ()

    async def test_an_unresolvable_url_is_skipped(self, monkeypatch, tmp_path) -> None:
        resolver = _Resolver(None, None)
        _wire(monkeypatch, resolver)
        out = await agent_service.resolve_turn_images(_Ctx(), [{"url": "/api/v1/uploads/gone"}])
        assert out == ()

    async def test_an_attachment_with_no_url_is_skipped(self, monkeypatch, tmp_path) -> None:
        out, _r, _ = await _run(monkeypatch, tmp_path, attachments=[{"name": "no url here"}])
        assert out == ()

    async def test_bytes_that_are_not_really_an_image_are_skipped(
        self, monkeypatch, tmp_path
    ) -> None:
        # A row claiming image/jpeg whose blob is not decodable. Forwarding it
        # would trade a missing picture for a provider-side 400 on the turn.
        out, _r, _ = await _run(monkeypatch, tmp_path, data=b"this is not a jpeg")
        assert out == ()

    async def test_one_bad_attachment_does_not_lose_the_good_one(
        self, monkeypatch, tmp_path
    ) -> None:
        raw = _jpeg()
        good = tmp_path / "good.jpg"
        good.write_bytes(raw)

        class _Mixed:
            @contextlib.asynccontextmanager
            async def open_local_for_url(self, url: str, workspace: str):
                if url.endswith("bad"):
                    yield None
                else:
                    yield _rec("good.jpg", "image/jpeg", len(raw)), good

        _wire(monkeypatch, _Mixed())
        out = await agent_service.resolve_turn_images(
            _Ctx(),
            [{"url": "/api/v1/uploads/bad"}, {"url": "/api/v1/uploads/good"}],
        )
        assert len(out) == 1, "per-file isolation: the readable one still rides"
        assert out[0].filename == "good.jpg"


class TestTheCaps:
    async def test_at_most_five_images_ride_one_turn(self, monkeypatch, tmp_path) -> None:
        raw = _jpeg(80, 80)
        p = tmp_path / "small.jpg"
        p.write_bytes(raw)
        resolver = _Resolver(_rec("small.jpg", "image/jpeg", len(raw)), p)
        _wire(monkeypatch, resolver)
        out = await agent_service.resolve_turn_images(
            _Ctx(), [{"url": f"/api/v1/uploads/f{i}"} for i in range(9)]
        )
        assert len(out) == agent_service._ATTACHMENT_MAX_FILES

    async def test_a_row_claiming_an_absurd_size_is_never_read(self, monkeypatch, tmp_path) -> None:
        raw = _jpeg(80, 80)
        p = tmp_path / "small.jpg"
        p.write_bytes(raw)
        huge = agent_service._MODEL_IMAGE_MAX_SOURCE_BYTES + 1
        resolver = _Resolver(_rec("small.jpg", "image/jpeg", huge), p)
        _wire(monkeypatch, resolver)
        out = await agent_service.resolve_turn_images(_Ctx(), [{"url": "/api/v1/uploads/f1"}])
        assert out == ()
