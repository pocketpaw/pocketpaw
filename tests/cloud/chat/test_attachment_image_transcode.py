# tests/cloud/chat/test_attachment_image_transcode.py — the formats a model can
# be SHOWN, and the ones that have to be converted first.
#
# Created 2026-09-15 (feat/chat-image-wiring). New file.
#
# The gate in ``resolve_turn_images`` is set membership, and a format missing
# from BOTH sets does not fail loudly — it falls through to the extraction path,
# which is the wrong tool for pixels. An image-only file has no text to give, so
# the model is handed whatever the extractor could scrape off the container and
# answers by describing THAT. Reported live on an AVIF: "I can see the file
# container metadata (AVIF, single still frame, roughly 246x56 based on the ispe
# box), but the actual pixel content is AV1-compressed and I can't decode it."
#
# That is the failure this file pins. It is not a crash and no test goes red on
# its own when a format is forgotten, so the membership itself is the assertion.

from __future__ import annotations

import io

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    _MODEL_IMAGE_MIMES,
    _TRANSCODE_TO_PNG_MIMES,
    _to_png,
)

# What the Claude Messages API accepts as an image block. A format outside this
# set is a rejected request, not a degraded answer, so it MUST be converted.
API_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _avif_bytes() -> bytes:
    """A real one-frame AVIF, encoded here so the fixture cannot drift."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (246, 56), (10, 120, 200)).save(buf, format="AVIF")
    return buf.getvalue()


class TestTheTwoSetsAgreeWithTheAPI:
    def test_every_direct_mime_is_one_the_api_actually_takes(self) -> None:
        # Shipping a mime here that the API rejects turns a working turn into a
        # 400 — worse than the OCR it replaced, because the turn dies.
        assert _MODEL_IMAGE_MIMES <= API_IMAGE_MIMES

    def test_no_mime_is_in_both_sets(self) -> None:
        # A format in both would be converted AND accepted directly; whichever
        # branch runs first silently wins.
        assert not (_MODEL_IMAGE_MIMES & _TRANSCODE_TO_PNG_MIMES)

    def test_nothing_the_api_takes_is_needlessly_transcoded(self) -> None:
        # Re-encoding a png to a png costs a decode per turn and buys nothing.
        assert not (_TRANSCODE_TO_PNG_MIMES & API_IMAGE_MIMES)

    @pytest.mark.parametrize("mime", ["image/avif", "image/heic", "image/heif"])
    def test_the_formats_phones_and_browsers_hand_us_are_covered(self, mime: str) -> None:
        # AVIF is what Chrome's "Copy image" gives you; HEIC is what an iPhone
        # shoots. Neither is on the API's list, so neither may be left out of
        # both sets — that is the fall-through this file exists for.
        assert mime in _MODEL_IMAGE_MIMES | _TRANSCODE_TO_PNG_MIMES


class TestAvifBecomesSomethingTheModelCanSee:
    def test_an_avif_transcodes_to_a_real_png(self) -> None:
        from PIL import Image

        out = _to_png(_avif_bytes(), mime="image/avif")
        assert out[:8] == b"\x89PNG\r\n\x1a\n", "the model is handed PNG bytes, not AVIF"
        with Image.open(io.BytesIO(out)) as img:
            # The pixels survive the round trip — the point of converting rather
            # than refusing is that the model sees the PICTURE, not a container.
            assert img.format == "PNG"
            assert img.size == (246, 56)

    def test_an_avif_is_not_handed_over_untouched(self) -> None:
        raw = _avif_bytes()
        assert _to_png(raw, mime="image/avif") != raw
