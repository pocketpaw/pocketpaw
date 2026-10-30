# Tests for API rate limiting and audit.
# Created: 2026-02-20


from pocketpaw.security.rate_limiter import (
    RateLimiter,
    RateLimitInfo,
    get_api_key_limiter,
    login_limiter,
)


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


class TestLoginLimiter:
    """The login_limiter caps brute-force attempts on /auth/login etc."""

    def test_login_limiter_blocks_after_5_attempts_per_ip_email(self):
        """6 hits on the same (ip, email) key — 6th returns denied with Retry-After."""
        # Use a fresh limiter so global state from earlier tests doesn't pollute.
        limiter = RateLimiter(rate=5.0 / 900.0, capacity=5)
        key = "login:1.2.3.4:bob@x.c"
        for _ in range(5):
            info = limiter.check(key)
            assert info.allowed is True
        info = limiter.check(key)
        assert info.allowed is False
        assert "Retry-After" in info.headers()

    def test_login_limiter_separate_emails_isolated(self):
        """Rotating the email field behind one IP gets its own bucket."""
        limiter = RateLimiter(rate=5.0 / 900.0, capacity=5)
        ip = "1.2.3.4"
        # Exhaust bob's bucket.
        for _ in range(5):
            limiter.check(f"login:{ip}:bob@x.c")
        assert limiter.check(f"login:{ip}:bob@x.c").allowed is False
        # alice still has her full bucket — keying lets the (ip, email) tuple
        # distinguish a normal user from a brute-force rotating emails.
        assert limiter.check(f"login:{ip}:alice@x.c").allowed is True

    def test_login_limiter_module_singleton_configured(self):
        """The exported login_limiter is 5 / 15 min."""
        assert login_limiter.capacity == 5
        assert abs(login_limiter.rate - 5.0 / 900.0) < 1e-9


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

        lines = (tmp_path / "test_audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
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

        lines = (tmp_path / "test_audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
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

        lines = (tmp_path / "test_audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
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
