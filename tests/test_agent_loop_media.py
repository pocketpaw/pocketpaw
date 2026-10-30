"""Tests for the media prompt helpers in pocketpaw.agents.loop."""

from __future__ import annotations

from pocketpaw.agents.loop import _format_bytes


class TestFormatBytes:
    def test_under_1kb_shows_bytes(self) -> None:
        assert _format_bytes(0) == "0 B"
        assert _format_bytes(1) == "1 B"
        assert _format_bytes(1023) == "1023 B"

    def test_kb_range(self) -> None:
        assert _format_bytes(1024) == "1.0 KB"
        assert _format_bytes(414255) == "404.5 KB"
        assert _format_bytes(1024 * 1024 - 1) == "1024.0 KB"

    def test_mb_range(self) -> None:
        assert _format_bytes(1024 * 1024) == "1.0 MB"
        assert _format_bytes(5_242_880) == "5.0 MB"

    def test_gb_range(self) -> None:
        assert _format_bytes(1024 * 1024 * 1024) == "1.0 GB"
        assert _format_bytes(2 * 1024 * 1024 * 1024) == "2.0 GB"
