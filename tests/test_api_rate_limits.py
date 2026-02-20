# Tests for API rate limiting and audit.
# Created: 2026-02-20


from pocketpaw.security.rate_limiter import RateLimiter, RateLimitInfo, get_api_key_limiter


class TestRateLimiter:
    """Tests for the enhanced RateLimiter."""

    def test_check_returns_info(self):
        limiter = RateLimiter(rate=10.0, capacity=5)
        info = limiter.check("test-client")
        assert isinstance(info, RateLimitInfo)
        assert info.allowed is True
        assert info.limit == 5
        assert info.remaining >= 0

    def test_check_denied(self):
        limiter = RateLimiter(rate=0.1, capacity=2)
        # Exhaust bucket
        limiter.check("client")
        limiter.check("client")
        info = limiter.check("client")
        assert info.allowed is False
        assert info.remaining == 0

    def test_headers_on_allowed(self):
        limiter = RateLimiter(rate=10.0, capacity=10)
        info = limiter.check("client")
        headers = info.headers()
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers
        assert "X-RateLimit-Reset" in headers
        assert headers["X-RateLimit-Limit"] == "10"
        # Should not have Retry-After when allowed
        assert "Retry-After" not in headers

    def test_headers_on_denied(self):
        limiter = RateLimiter(rate=0.1, capacity=1)
        limiter.check("client")
        info = limiter.check("client")
        headers = info.headers()
        assert info.allowed is False
        assert "Retry-After" in headers
        assert int(headers["Retry-After"]) > 0

    def test_allow_still_works(self):
        """Backward compat: allow() returns bool."""
        limiter = RateLimiter(rate=10.0, capacity=5)
        assert limiter.allow("client") is True

    def test_api_key_limiter_exists(self):
        """get_api_key_limiter() returns a config-aware limiter."""
        limiter = get_api_key_limiter()
        assert limiter.capacity == 60
        assert limiter.rate == 60 / 60.0  # capacity / 60s


class TestRateLimitInfo:
    """Tests for RateLimitInfo."""

    def test_headers_format(self):
        info = RateLimitInfo(allowed=True, limit=60, remaining=59, reset_after=1.5)
        h = info.headers()
        assert h["X-RateLimit-Limit"] == "60"
        assert h["X-RateLimit-Remaining"] == "59"
        assert h["X-RateLimit-Reset"] == "2"  # ceil(1.5)

    def test_headers_denied_format(self):
        info = RateLimitInfo(allowed=False, limit=60, remaining=0, reset_after=3.7)
        h = info.headers()
        assert h["Retry-After"] == "4"  # ceil(3.7)


class TestAuditAPIEvents:
    """Tests for API audit logging."""

    def test_log_api_event(self, tmp_path):
        from pocketpaw.security.audit import AuditLogger

        audit = AuditLogger(log_path=tmp_path / "test_audit.jsonl")
        event_id = audit.log_api_event(
            action="api_key_created",
            target="key:abc123",
            key_name="my-key",
            scopes=["chat"],
        )
        assert event_id is not None

        # Verify written to file
        import json

        lines = (tmp_path / "test_audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "api_key_created"
        assert entry["target"] == "key:abc123"
        assert entry["context"]["key_name"] == "my-key"

    def test_log_api_event_oauth(self, tmp_path):
        from pocketpaw.security.audit import AuditLogger

        audit = AuditLogger(log_path=tmp_path / "test_audit.jsonl")
        event_id = audit.log_api_event(
            action="oauth_token",
            target="client:pocketpaw-desktop",
            scope="chat sessions",
        )
        assert event_id is not None

        import json

        lines = (tmp_path / "test_audit.jsonl").read_text().strip().split("\n")
        entry = json.loads(lines[0])
        assert entry["action"] == "oauth_token"
        assert entry["context"]["scope"] == "chat sessions"

    def test_log_api_event_revoke(self, tmp_path):
        from pocketpaw.security.audit import AuditLogger

        audit = AuditLogger(log_path=tmp_path / "test_audit.jsonl")
        audit.log_api_event(
            action="api_key_revoked",
            target="key:def456",
            key_name="old-key",
        )

        import json

        lines = (tmp_path / "test_audit.jsonl").read_text().strip().split("\n")
        entry = json.loads(lines[0])
        assert entry["action"] == "api_key_revoked"


class TestOpenAPIConfig:
    """Tests for OpenAPI configuration."""

    def test_openapi_endpoint_exists(self):
        from pocketpaw.dashboard import app

        assert app.openapi_url == "/api/v1/openapi.json"
        assert app.docs_url == "/api/v1/docs"
        assert app.redoc_url == "/api/v1/redoc"

    def test_openapi_metadata(self):
        from pocketpaw.dashboard import app

        assert app.title == "PocketPaw API"
        assert "1.0.0" in app.version


class TestConfigRateLimit:
    """Tests for api_rate_limit_per_key config field."""

    def test_field_exists(self):
        from pocketpaw.config import Settings

        assert "api_rate_limit_per_key" in Settings.model_fields

    def test_default_value(self):
        from pocketpaw.config import Settings

        s = Settings()
        assert s.api_rate_limit_per_key == 60
