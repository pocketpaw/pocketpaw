"""Tests for SSRF protection in UrlExtractTool local extraction - issue #616."""

import socket
from unittest.mock import patch

import pytest

from pocketpaw.tools.builtin.url_extract import _is_safe_url


class TestIsSafeUrl:
    """Tests for the _is_safe_url SSRF validation function."""

    @pytest.mark.asyncio
    async def test_valid_https_url(self):
        """Valid public HTTPS URL should be allowed."""
        with patch("socket.gethostbyname", return_value="93.184.216.34"):
            safe, reason = await _is_safe_url("https://example.com")
        assert safe is True

    @pytest.mark.asyncio
    async def test_valid_http_url(self):
        """Valid public HTTP URL should be allowed."""
        with patch("socket.gethostbyname", return_value="93.184.216.34"):
            safe, reason = await _is_safe_url("http://example.com")
        assert safe is True

    @pytest.mark.asyncio
    async def test_blocks_loopback(self):
        """Loopback address should be blocked."""
        with patch("socket.gethostbyname", return_value="127.0.0.1"):
            safe, reason = await _is_safe_url("http://localhost")
        assert safe is False
        assert "loopback" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_private_192(self):
        """Private RFC1918 address 192.168.x.x should be blocked."""
        with patch("socket.gethostbyname", return_value="192.168.1.1"):
            safe, reason = await _is_safe_url("http://192.168.1.1")
        assert safe is False
        assert "private" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_private_10(self):
        """Private RFC1918 address 10.x.x.x should be blocked."""
        with patch("socket.gethostbyname", return_value="10.0.0.1"):
            safe, reason = await _is_safe_url("http://10.0.0.1")
        assert safe is False
        assert "private" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_link_local(self):
        """Link-local address 169.254.x.x should be blocked."""
        with patch("socket.gethostbyname", return_value="169.254.169.254"):
            safe, reason = await _is_safe_url("http://169.254.169.254/latest/meta-data/")
        assert safe is False
        assert "private" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_ftp_scheme(self):
        """FTP scheme should be blocked."""
        safe, reason = await _is_safe_url("ftp://example.com/file.txt")
        assert safe is False
        assert "scheme" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_file_scheme(self):
        """File scheme should be blocked."""
        safe, reason = await _is_safe_url("file:///etc/passwd")
        assert safe is False
        assert "scheme" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_empty_hostname(self):
        """URL with no hostname should be blocked."""
        safe, reason = await _is_safe_url("http://")
        assert safe is False

    @pytest.mark.asyncio
    async def test_blocks_unresolvable_hostname(self):
        """Unresolvable hostname should be blocked."""
        with patch("socket.gethostbyname", side_effect=socket.gaierror):
            safe, reason = await _is_safe_url("http://nonexistent.invalid")
        assert safe is False
        assert "resolve" in reason.lower()
