# SSRF URL-validation tests for Settings URL fields.
# Added: 2026-04-16 for security sprint cluster E (#703).

from __future__ import annotations

import pytest


def _reload_settings():
    """Force a fresh Settings() read so env changes take effect.

    The Settings singleton caches the first-loaded instance; we want each
    test to see its own environment variables.
    """
    from pocketpaw import config as cfg

    cfg._settings = None  # invalidate singleton if present
    return cfg


class TestExternalUrlValidator:
    def test_internal_url_rejected_when_not_allowed(self, monkeypatch):
        from pocketpaw.security.url_validators import validate_external_url

        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        with pytest.raises(ValueError):
            validate_external_url("http://169.254.169.254/")  # EC2 metadata
        with pytest.raises(ValueError):
            validate_external_url("http://127.0.0.1:8080/")
        with pytest.raises(ValueError):
            validate_external_url("http://10.0.0.5/")
        with pytest.raises(ValueError):
            validate_external_url("http://192.168.1.1/")

    def test_public_url_accepted(self, monkeypatch):
        from pocketpaw.security.url_validators import validate_external_url

        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        # Public URLs pass
        assert validate_external_url("https://api.openai.com") == "https://api.openai.com"
        assert validate_external_url("http://example.com") == "http://example.com"

    def test_non_http_scheme_always_rejected(self, monkeypatch):
        from pocketpaw.security.url_validators import validate_external_url

        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "true")
        with pytest.raises(ValueError, match="scheme"):
            validate_external_url("file:///etc/passwd")
        with pytest.raises(ValueError, match="scheme"):
            validate_external_url("ftp://example.com/")
        with pytest.raises(ValueError, match="scheme"):
            validate_external_url("gopher://x")

    def test_internal_url_accepted_when_flag_set(self, monkeypatch):
        from pocketpaw.security.url_validators import validate_external_url

        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "true")
        # Localhost & RFC1918 pass when explicitly allowed (dev default)
        assert validate_external_url("http://localhost:4096") == "http://localhost:4096"
        assert validate_external_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
        assert validate_external_url("http://192.168.1.100") == "http://192.168.1.100"

    def test_empty_string_accepted(self, monkeypatch):
        """Empty string means "not configured" — Settings defaults use this."""
        from pocketpaw.security.url_validators import validate_external_url

        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        assert validate_external_url("") == ""

    def test_malformed_url_rejected(self, monkeypatch):
        from pocketpaw.security.url_validators import validate_external_url

        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "true")
        with pytest.raises(ValueError):
            validate_external_url("not-a-url")
        with pytest.raises(ValueError):
            validate_external_url("http://")


class TestSettingsAppliesValidator:
    """Integration: a Settings load with a malicious URL env var fails fast."""

    def test_opencode_base_url_rejects_metadata_service(self, monkeypatch):
        _reload_settings()
        from pocketpaw.config import Settings

        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setenv("POCKETPAW_OPENCODE_BASE_URL", "http://169.254.169.254/")
        with pytest.raises(Exception):  # pydantic wraps ValueError in ValidationError
            Settings()

    def test_signal_api_url_rejects_file_scheme(self, monkeypatch):
        _reload_settings()
        from pocketpaw.config import Settings

        monkeypatch.setenv("POCKETPAW_SIGNAL_API_URL", "file:///etc/passwd")
        with pytest.raises(Exception):
            Settings()
