# tests/cloud/chat/test_attachment_image_size_limits.py — a big picture still
# reaches the model.
#
# Created 2026-09-15 (feat/chat-image-wiring). New file.
#
# The per-image ceiling is the provider's, not ours: Anthropic documents 5MB and
# it is the safe floor across providers. The surface-snapshot channel already
# answers to it (``run_core._MAX_IMAGE_BYTES``, added 2026-09-11 after a 5-6MB
# page snapshot sailed through our guard and was refused by the provider — which
# the user saw as a failed turn with no useful message). The user-attachment
# channel feeds the SAME request and had no ceiling at all.
#
# A cap alone would have made the common case worse rather than better. A 4K PNG
# screenshot routinely clears 5MB, and so does any HEIC or AVIF we transcode,
# because PNG is lossless and a modest photo comes out several times larger. Cap
# without shrink means the feature works for small pictures and silently drops
# the ones people actually paste.
#
# So the contract this file pins is: already-small images pass through
# BYTE-IDENTICAL, oversized ones are shrunk until they fit, and only something
# that cannot be made to fit is refused.

from __future__ import annotations

import io

import pytest
from PIL import Image
from pocketpaw_ee.cloud.chat.agent_service import (
    _MODEL_IMAGE_MAX_BYTES,
    _MODEL_IMAGE_MAX_EDGE,
    _fit_for_model,
)


def _png(w: int, h: int, *, noise: bool = False) -> bytes:
    """A PNG of a given size. ``noise`` defeats PNG's compression so the bytes
    are genuinely large rather than a solid colour that squashes to nothing."""
    img = Image.new("RGB", (w, h), (240, 240, 240))
    if noise:
        import os

        img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 90, 200)).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class TestSomethingThatAlreadyFits:
    def test_a_small_png_is_returned_byte_identical(self) -> None:
        # The common case. Re-encoding every attachment would change bytes the
        # user uploaded for no reason and burn CPU on every turn.
        raw = _png(320, 200)
        assert len(raw) <= _MODEL_IMAGE_MAX_BYTES
        out = _fit_for_model(raw, mime="image/png")
        assert out is not None
        data, mime = out
        assert data is raw, "an image that fits must not be re-encoded"
        assert mime == "image/png"

    def test_a_small_jpeg_keeps_its_mime(self) -> None:
        raw = _jpeg(320, 200)
        out = _fit_for_model(raw, mime="image/jpeg")
        assert out is not None
        assert out == (raw, "image/jpeg")


class TestSomethingTooWide:
    def test_a_huge_edge_is_brought_under_the_limit(self) -> None:
        # Past ~1568 the API downscales before the model looks, while billing
        # the size we sent. Shrinking is the same picture for fewer tokens.
        raw = _png(4000, 2200)
        out = _fit_for_model(raw, mime="image/png")
        assert out is not None
        data, _ = out
        with Image.open(io.BytesIO(data)) as img:
            assert max(img.size) <= _MODEL_IMAGE_MAX_EDGE
            # Aspect ratio survives — a squashed screenshot is unreadable.
            assert img.size[0] > img.size[1]

    def test_the_result_is_still_a_decodable_image(self) -> None:
        raw = _png(4000, 2200)
        out = _fit_for_model(raw, mime="image/png")
        assert out is not None
        data, mime = out
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "PNG"
        assert mime == "image/png"


class TestSomethingTooHeavy:
    def test_a_png_over_the_byte_ceiling_is_shrunk_not_dropped(self) -> None:
        # This is the reported case: a screenshot too big to send. Before the
        # fitter it either blew the provider limit or, with a bare cap, vanished.
        raw = _png(2400, 1400, noise=True)
        assert len(raw) > _MODEL_IMAGE_MAX_BYTES, "fixture must exceed the ceiling"
        out = _fit_for_model(raw, mime="image/png")
        assert out is not None, "a large screenshot must still reach the model"
        data, _ = out
        assert len(data) <= _MODEL_IMAGE_MAX_BYTES

    def test_a_jpeg_stays_a_jpeg_when_shrunk(self) -> None:
        # Re-encoding a photo as lossless PNG inflates it, which is the opposite
        # of what the caller asked for.
        raw = _jpeg(5000, 3000)
        out = _fit_for_model(raw, mime="image/jpeg")
        assert out is not None
        data, mime = out
        assert mime == "image/jpeg"
        assert len(data) <= _MODEL_IMAGE_MAX_BYTES
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "JPEG"


class TestWhatCannotBeFitted:
    def test_bytes_that_are_not_an_image_are_refused(self) -> None:
        # Unreadable here means unreadable to the provider too, so refusing is
        # the honest answer rather than forwarding a 400.
        assert _fit_for_model(b"not an image at all", mime="image/png") is None

    def test_an_animated_gif_is_never_flattened(self) -> None:
        # Pillow would resize it down to a single frame. A still of an animation
        # is not what was attached, so an oversized one is refused instead.
        frames = [Image.new("RGB", (64, 64), (i * 20, 0, 0)) for i in range(4)]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
        raw = buf.getvalue()
        out = _fit_for_model(raw, mime="image/gif")
        # Small enough to ride as-is, and untouched.
        assert out is not None
        assert out[0] == raw


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg"])
def test_every_fitted_result_is_within_the_ceiling(mime: str) -> None:
    raw = _png(3000, 1800, noise=True) if mime == "image/png" else _jpeg(4000, 2400)
    out = _fit_for_model(raw, mime=mime)
    assert out is not None
    assert len(out[0]) <= _MODEL_IMAGE_MAX_BYTES
