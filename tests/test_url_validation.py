"""Tests for URL validation and SSRF prevention (issue #703)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pocketpaw.security.url_validation import is_url_safe, validate_url

# ---------------------------------------------------------------------------
# validate_url — scheme checks
# ---------------------------------------------------------------------------


class TestSchemeValidation:
    """Reject non-http(s) schemes."""

    def test_http_allowed(self) -> None:
        assert validate_url("http://example.com") == "http://example.com"

    def test_https_allowed(self) -> None:
        assert validate_url("https://example.com") == "https://example.com"

    def test_ftp_rejected(self) -> None:
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_url("ftp://example.com")

    def test_file_rejected(self) -> None:
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_url("file:///etc/passwd")

    def test_data_rejected(self) -> None:
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_url("data:text/html,<h1>hi</h1>")

    def test_javascript_rejected(self) -> None:
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_url("javascript:alert(1)")

    def test_no_hostname_rejected(self) -> None:
        with pytest.raises(ValueError, match="no hostname"):
            validate_url("http://")


# ---------------------------------------------------------------------------
# validate_url — private IP blocking
# ---------------------------------------------------------------------------


class TestPrivateIPBlocking:
    """Block URLs targeting private/reserved IP ranges."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1/admin",
            "http://10.255.255.255/",
            "http://172.16.0.1/",
            "http://172.31.255.255/",
            "http://192.168.1.1/",
            "http://192.168.0.100:8080/api",
        ],
    )
    def test_private_ranges_blocked(self, url: str) -> None:
        with pytest.raises(ValueError, match="private/reserved"):
            validate_url(url)

    def test_link_local_blocked(self) -> None:
        """Block 169.254.x.x — AWS/GCP metadata endpoint."""
        with pytest.raises(ValueError, match="private/reserved"):
            validate_url("http://169.254.169.254/latest/meta-data/")

    def test_localhost_blocked_by_default(self) -> None:
        with pytest.raises(ValueError, match="private/reserved"):
            validate_url("http://127.0.0.1:8080/")

    def test_localhost_allowed_with_flag(self) -> None:
        result = validate_url("http://127.0.0.1:4096/", allow_localhost=True)
        assert result == "http://127.0.0.1:4096/"

    def test_ipv6_loopback_blocked_by_default(self) -> None:
        with pytest.raises(ValueError, match="private/reserved"):
            validate_url("http://[::1]:8080/")

    def test_ipv6_loopback_allowed_with_flag(self) -> None:
        result = validate_url("http://[::1]:8080/", allow_localhost=True)
        assert result == "http://[::1]:8080/"

    def test_zero_address_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/reserved"):
            validate_url("http://0.0.0.0/")

    def test_public_ip_allowed(self) -> None:
        assert validate_url("http://8.8.8.8/") == "http://8.8.8.8/"

    def test_allow_private_flag_skips_all_checks(self) -> None:
        result = validate_url("http://10.0.0.1/", allow_private=True)
        assert result == "http://10.0.0.1/"


# ---------------------------------------------------------------------------
# validate_url — hostname (non-IP) URLs
# ---------------------------------------------------------------------------


class TestHostnameURLs:
    """Non-IP hostnames pass through (DNS resolution is not checked)."""

    def test_public_hostname_allowed(self) -> None:
        assert validate_url("https://api.openai.com/v1") == "https://api.openai.com/v1"

    def test_hostname_with_port_allowed(self) -> None:
        assert validate_url("http://myserver:8080/") == "http://myserver:8080/"


# ---------------------------------------------------------------------------
# validate_url — empty / None handling
# ---------------------------------------------------------------------------


class TestEmptyValues:
    """Empty strings and None should pass through (optional fields)."""

    def test_empty_string_passthrough(self) -> None:
        assert validate_url("") == ""

    def test_whitespace_passthrough(self) -> None:
        assert validate_url("   ") == "   "


# ---------------------------------------------------------------------------
# is_url_safe — non-raising helper
# ---------------------------------------------------------------------------


class TestIsUrlSafe:
    def test_safe_url(self) -> None:
        assert is_url_safe("https://example.com") is True

    def test_unsafe_url(self) -> None:
        assert is_url_safe("http://169.254.169.254/") is False

    def test_bad_scheme(self) -> None:
        assert is_url_safe("ftp://example.com") is False

    def test_localhost_unsafe_by_default(self) -> None:
        assert is_url_safe("http://127.0.0.1/") is False

    def test_localhost_safe_with_flag(self) -> None:
        assert is_url_safe("http://127.0.0.1/", allow_localhost=True) is True


# ---------------------------------------------------------------------------
# Config field validation integration
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """Settings rejects unsafe URLs on config fields."""

    def test_config_rejects_file_scheme(self) -> None:
        from pocketpaw.config import Settings

        with pytest.raises(ValidationError, match="scheme must be http or https"):
            Settings(opencode_base_url="file:///etc/passwd")

    def test_config_rejects_metadata_ip(self) -> None:
        from pocketpaw.config import Settings

        with pytest.raises(ValidationError, match="private/reserved"):
            Settings(opencode_base_url="http://169.254.169.254/latest/meta-data/")

    def test_config_accepts_localhost_defaults(self) -> None:
        """Default localhost URLs must still be accepted."""
        from pocketpaw.config import Settings

        s = Settings()
        assert s.opencode_base_url == "http://localhost:4096"
        assert s.litellm_api_base == "http://localhost:4000"
        assert s.ollama_host == "http://localhost:11434"

    def test_config_accepts_public_url(self) -> None:
        from pocketpaw.config import Settings

        s = Settings(opencode_base_url="https://my-opencode.example.com:4096")
        assert s.opencode_base_url == "https://my-opencode.example.com:4096"

    def test_config_accepts_empty_optional_url(self) -> None:
        from pocketpaw.config import Settings

        s = Settings(openai_compatible_base_url="")
        assert s.openai_compatible_base_url == ""

    def test_config_rejects_private_ip_on_signal_url(self) -> None:
        from pocketpaw.config import Settings

        with pytest.raises(ValidationError, match="private/reserved"):
            Settings(signal_api_url="http://10.0.0.5:9200/")

    def test_config_rejects_ftp_on_ollama_host(self) -> None:
        from pocketpaw.config import Settings

        with pytest.raises(ValidationError, match="scheme must be http or https"):
            Settings(ollama_host="ftp://localhost:11434")

    def test_config_allows_localhost_ip_on_litellm(self) -> None:
        """allow_localhost=True so 127.0.0.1 is OK for config fields."""
        from pocketpaw.config import Settings

        s = Settings(litellm_api_base="http://127.0.0.1:4000")
        assert s.litellm_api_base == "http://127.0.0.1:4000"
