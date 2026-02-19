"""Tests for update_check module.

Changes:
  - 2026-02-16: Initial tests for PyPI version check with caching.
"""

import json
import time
from unittest.mock import patch

from pocketpaw.update_check import (
    CACHE_FILENAME,
    CACHE_TTL,
    _parse_version,
    check_for_updates,
    print_update_notice,
)


class TestParseVersion:
    def test_simple(self):
        assert _parse_version("0.4.1") == (0, 4, 1)

    def test_major(self):
        assert _parse_version("1.0.0") == (1, 0, 0)

    def test_two_digit(self):
        assert _parse_version("0.12.3") == (0, 12, 3)


class TestCheckForUpdates:
    def test_returns_no_update_when_current(self, tmp_path):
        """When PyPI returns same version, update_available is False."""
        pypi_response = json.dumps({"info": {"version": "0.4.1"}}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = lambda s, *a: None
            mock_urlopen.return_value.read.return_value = pypi_response

            result = check_for_updates("0.4.1", tmp_path)

        assert result is not None
        assert result["current"] == "0.4.1"
        assert result["latest"] == "0.4.1"
        assert result["update_available"] is False

    def test_returns_update_when_behind(self, tmp_path):
        """When PyPI has newer version, update_available is True."""
        pypi_response = json.dumps({"info": {"version": "0.5.0"}}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = lambda s, *a: None
            mock_urlopen.return_value.read.return_value = pypi_response

            result = check_for_updates("0.4.1", tmp_path)

        assert result is not None
        assert result["update_available"] is True
        assert result["latest"] == "0.5.0"

    def test_writes_cache_file(self, tmp_path):
        """After a successful check, cache file should exist."""
        pypi_response = json.dumps({"info": {"version": "0.4.1"}}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = lambda s, *a: None
            mock_urlopen.return_value.read.return_value = pypi_response

            check_for_updates("0.4.1", tmp_path)

        cache_file = tmp_path / CACHE_FILENAME
        assert cache_file.exists()
        cache = json.loads(cache_file.read_text())
        assert "ts" in cache
        assert cache["latest"] == "0.4.1"

    def test_uses_fresh_cache(self, tmp_path):
        """When cache is fresh, doesn't hit PyPI."""
        cache_file = tmp_path / CACHE_FILENAME
        cache_file.write_text(json.dumps({"ts": time.time(), "latest": "0.5.0"}))

        # No mock needed — if it tries to hit PyPI it would fail
        result = check_for_updates("0.4.1", tmp_path)

        assert result is not None
        assert result["update_available"] is True
        assert result["latest"] == "0.5.0"

    def test_ignores_stale_cache(self, tmp_path):
        """When cache is older than TTL, re-fetches from PyPI."""
        cache_file = tmp_path / CACHE_FILENAME
        stale_ts = time.time() - CACHE_TTL - 100
        cache_file.write_text(json.dumps({"ts": stale_ts, "latest": "0.3.0"}))

        pypi_response = json.dumps({"info": {"version": "0.4.1"}}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = lambda s, *a: None
            mock_urlopen.return_value.read.return_value = pypi_response

            result = check_for_updates("0.4.1", tmp_path)

        assert result is not None
        assert result["latest"] == "0.4.1"  # Updated from stale 0.3.0

    def test_returns_none_on_network_error(self, tmp_path):
        """Network errors return None, never raise."""
        with patch("urllib.request.urlopen", side_effect=Exception("no network")):
            result = check_for_updates("0.4.1", tmp_path)

        assert result is None

    def test_handles_corrupted_cache(self, tmp_path):
        """Corrupted cache file doesn't crash, re-fetches."""
        cache_file = tmp_path / CACHE_FILENAME
        cache_file.write_text("not json{{{")

        pypi_response = json.dumps({"info": {"version": "0.4.1"}}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = lambda s, *a: None
            mock_urlopen.return_value.read.return_value = pypi_response

            result = check_for_updates("0.4.1", tmp_path)

        assert result is not None
        assert result["current"] == "0.4.1"


class TestPrintUpdateNotice:
    def test_prints_notice(self, capsys):
        print_update_notice({"current": "0.4.0", "latest": "0.4.1"})
        captured = capsys.readouterr()
        assert "0.4.0" in captured.out
        assert "0.4.1" in captured.out
        assert "pip install --upgrade pocketpaw" in captured.out
