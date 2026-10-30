# tests/cloud/studio/test_reference_mime.py — the mime a reference is labelled with.
#
# Created 2026-09-05 (fix/studio-audio-mime-in-references).
#
# A generated music track wired into a Video node reached fal as
# "data:image/png;base64,<mp3 bytes>", because `_MIME_BY_EXT` held image
# extensions only and everything else fell through to an "image/png" default.
# fal put it in audio_urls, tried to decode a PNG as sound, and rejected a
# perfectly good 10-second track as 0.04 seconds long.
#
# The bytes were never wrong — only the label — which is exactly why this needs a
# test that reads the data URI HEADER. Every assertion that only checked "an
# audio url was forwarded" passed throughout.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import service


class TestMimeForFilename:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("track.mp3", "audio/mpeg"),
            ("bed.wav", "audio/wav"),
            ("voice.m4a", "audio/mp4"),
            ("amb.ogg", "audio/ogg"),
        ],
    )
    def test_audio_extensions_resolve_to_audio(self, name: str, expected: str) -> None:
        """Mutation that must break this: drop the audio rows from _MIME_BY_EXT."""
        assert service._mime_for_filename(name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("clip.mp4", "video/mp4"),
            ("take.mov", "video/quicktime"),
            ("web.webm", "video/webm"),
        ],
    )
    def test_video_extensions_resolve_to_video(self, name: str, expected: str) -> None:
        assert service._mime_for_filename(name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("still.png", "image/png"), ("shot.jpg", "image/jpeg"), ("art.webp", "image/webp")],
    )
    def test_images_are_unchanged(self, name: str, expected: str) -> None:
        """The image paths are the majority caller and must not move."""
        assert service._mime_for_filename(name) == expected

    def test_no_media_extension_is_ever_labelled_as_an_image(self) -> None:
        """The specific defect: an extension the table does not know silently
        became a PNG, whatever it actually was."""
        for name in ("track.mp3", "clip.mp4", "bed.wav"):
            assert not service._mime_for_filename(name).startswith("image/")

    def test_an_unknown_extension_falls_back_to_the_callers_expectation(self) -> None:
        """A caller resolving audio should not get an image default for a file
        whose extension it cannot read."""
        assert service._mime_for_filename("mystery", "audio/mpeg") == "audio/mpeg"
        assert service._mime_for_filename("mystery.xyz", "video/mp4") == "video/mp4"
        # Unchanged for callers that pass nothing.
        assert service._mime_for_filename("mystery") == "image/png"


class TestResolveSourceDataUrl:
    async def test_a_stored_mp3_is_encoded_as_audio_not_as_a_png(self, monkeypatch) -> None:
        """The end-to-end shape of the bug: the header on the data URI is what
        fal reads, and it said image/png over MP3 bytes.

        Mutation that must break this: hardcode the image default again.
        """
        data_url, mime = await _resolve_stored(monkeypatch, "track.mp3", b"ID3fake-mp3-bytes")
        assert mime == "audio/mpeg"
        assert data_url.startswith("data:audio/mpeg;base64,")
        assert "image/png" not in data_url

    async def test_a_stored_png_still_encodes_as_an_image(self, monkeypatch) -> None:
        data_url, mime = await _resolve_stored(monkeypatch, "still.png", b"\x89PNG-fake")
        assert mime == "image/png"
        assert data_url.startswith("data:image/png;base64,")

    async def test_an_extensionless_audio_reference_honours_the_default(self, monkeypatch) -> None:
        data_url, mime = await _resolve_stored(
            monkeypatch, "noext", b"bytes", default_mime="audio/mpeg"
        )
        assert mime == "audio/mpeg"
        assert data_url.startswith("data:audio/mpeg;base64,")


async def _resolve_stored(
    monkeypatch, name: str, data: bytes, default_mime: str = "image/png"
) -> tuple[str, str]:
    """Resolve a media-path reference with the storage adapter stubbed out."""

    class _Adapter:
        async def exists(self, key):  # noqa: ANN001, ANN202
            return True

        async def open(self, key):  # noqa: ANN001, ANN202
            yield data

    monkeypatch.setattr(service.media_storage, "get_adapter", lambda: _Adapter())
    monkeypatch.setattr(service.media_storage, "media_key", lambda n: n)
    return await service._resolve_source_data_url(
        f"/api/v1/media/{name}", default_mime=default_mime
    )
