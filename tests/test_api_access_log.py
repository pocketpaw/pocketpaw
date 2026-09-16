# Tests for the nginx-combined HTTP access-log middleware (feat/shield-access-log).
#
# Covers:
#   - real client IP precedence: CF-Connecting-IP → X-Forwarded-For leftmost →
#     socket peer (request.client.host).
#   - the emitted line matches nginx-combined shape (method, path, status, UA).
#   - default (access_log_path unset) → no file written, middleware NOT added by
#     create_api_app().
#
# The IP/format cases mount AccessLogMiddleware on a minimal FastAPI app (fast,
# focused) via FastAPI's TestClient. The wiring cases exercise create_api_app()
# with Settings.load patched, since the middleware is opt-in at build time.

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.access_log import AccessLogMiddleware, _resolve_client_ip

# nginx-combined:
#   <ip> - - [<time>] "<METHOD> <path> HTTP/<ver>" <status> <bytes> "<ref>" "<ua>"
_COMBINED_RE = re.compile(
    r"^(?P<ip>\S+) - - "
    r"\[(?P<ts>[^\]]+)\] "
    r'"(?P<method>\S+) (?P<path>\S+) HTTP/(?P<ver>[\d.]+)" '
    r"(?P<status>\d{3}) (?P<size>\d+) "
    r'"(?P<referer>[^"]*)" "(?P<ua>[^"]*)"$'
)


@pytest.fixture
def logged_app(tmp_path):
    """A minimal app wrapped in AccessLogMiddleware writing to a tmp file."""
    log_file = tmp_path / "nested" / "access.log"
    app = FastAPI()

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    app.add_middleware(AccessLogMiddleware, log_path=str(log_file))
    return app, log_file


def _read_last_line(path):
    lines = path.read_text().splitlines()
    assert lines, f"no lines written to {path}"
    return lines[-1]


# ---------------------------------------------------------------------------
# Real client IP precedence
# ---------------------------------------------------------------------------


def test_cf_connecting_ip_wins(logged_app):
    app, log_file = logged_app
    client = TestClient(app)
    resp = client.get(
        "/hello",
        headers={
            "CF-Connecting-IP": "203.0.113.9",
            "X-Forwarded-For": "198.51.100.7, 10.0.0.1",
            "User-Agent": "probe/1.0",
        },
    )
    assert resp.status_code == 200
    m = _COMBINED_RE.match(_read_last_line(log_file))
    assert m is not None
    assert m.group("ip") == "203.0.113.9"


def test_x_forwarded_for_leftmost_when_no_cf(logged_app):
    app, log_file = logged_app
    client = TestClient(app)
    resp = client.get(
        "/hello",
        headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"},
    )
    assert resp.status_code == 200
    m = _COMBINED_RE.match(_read_last_line(log_file))
    assert m is not None
    assert m.group("ip") == "198.51.100.7"


def test_peer_ip_when_no_proxy_headers(logged_app):
    app, log_file = logged_app
    # TestClient sets request.client.host to "testclient".
    client = TestClient(app)
    resp = client.get("/hello")
    assert resp.status_code == 200
    m = _COMBINED_RE.match(_read_last_line(log_file))
    assert m is not None
    assert m.group("ip") == "testclient"


def test_resolve_client_ip_precedence_unit():
    """Direct unit test of the precedence helper."""
    cf = (b"cf-connecting-ip", b"203.0.113.9")
    xff = (b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")
    assert _resolve_client_ip([cf, xff], "peer") == "203.0.113.9"
    assert _resolve_client_ip([xff], "peer") == "198.51.100.7"
    assert _resolve_client_ip([], "peer") == "peer"
    # Empty header values fall through to the next source.
    assert _resolve_client_ip([(b"cf-connecting-ip", b"  ")], "peer") == "peer"


# ---------------------------------------------------------------------------
# nginx-combined shape
# ---------------------------------------------------------------------------


def test_line_matches_nginx_combined_shape(logged_app):
    app, log_file = logged_app
    client = TestClient(app)
    resp = client.get("/hello", headers={"User-Agent": "curl/8.0"})
    assert resp.status_code == 200

    line = _read_last_line(log_file)
    m = _COMBINED_RE.match(line)
    assert m is not None, f"line did not match nginx-combined: {line!r}"
    assert m.group("method") == "GET"
    assert m.group("path") == "/hello"
    assert m.group("status") == "200"
    assert m.group("ua") == "curl/8.0"
    assert m.group("ver")  # HTTP version present


def test_query_string_included_in_path(logged_app):
    app, log_file = logged_app
    client = TestClient(app)
    resp = client.get("/hello?probe=../../etc/passwd")
    assert resp.status_code == 200
    m = _COMBINED_RE.match(_read_last_line(log_file))
    assert m is not None
    assert m.group("path").startswith("/hello?")
    assert "probe=" in m.group("path")


# ---------------------------------------------------------------------------
# Robustness — a write failure must never break the request
# ---------------------------------------------------------------------------


def test_write_failure_does_not_break_request(tmp_path, caplog):
    # Point the log at a path whose "parent" is an existing FILE, so mkdir and
    # open both fail. The request must still succeed.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    bad_path = blocker / "sub" / "access.log"

    app = FastAPI()

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    app.add_middleware(AccessLogMiddleware, log_path=str(bad_path))
    client = TestClient(app)
    resp = client.get("/hello")
    assert resp.status_code == 200  # request unaffected by the logging failure


# ---------------------------------------------------------------------------
# Opt-in wiring — middleware added only when access_log_path is set
# ---------------------------------------------------------------------------


def _has_access_log_middleware(app) -> bool:
    return any(mw.cls is AccessLogMiddleware for mw in app.user_middleware)


def _force_oss_backend(monkeypatch):
    """Pin the OSS ``file`` memory backend so create_api_app() doesn't try to
    build the ee-only mongodb backend from a developer's local config.json —
    keeps these wiring tests hermetic (CI has no mongodb config; a dev box may).
    """
    monkeypatch.setenv("POCKETPAW_MEMORY_BACKEND", "file")
    monkeypatch.setenv("POCKETPAW_IGNORE_CONFIG_JSON", "true")


def _patch_access_log_path(monkeypatch, value):
    """Make Settings.load() return the given access_log_path, preserving the
    real load path for everything else."""
    from pocketpaw.config import Settings

    real_load = Settings.load.__func__

    def _load(cls):
        s = real_load(cls)
        s.access_log_path = value
        return s

    monkeypatch.setattr(Settings, "load", classmethod(_load))


def test_middleware_absent_by_default(monkeypatch):
    """access_log_path unset → create_api_app() does NOT add the middleware."""
    _force_oss_backend(monkeypatch)
    _patch_access_log_path(monkeypatch, None)

    from pocketpaw.api.serve import create_api_app

    app = create_api_app()
    assert not _has_access_log_middleware(app)


def test_middleware_present_when_path_set(monkeypatch, tmp_path):
    """access_log_path set → create_api_app() adds the middleware."""
    _force_oss_backend(monkeypatch)
    _patch_access_log_path(monkeypatch, str(tmp_path / "access.log"))

    from pocketpaw.api.serve import create_api_app

    app = create_api_app()
    assert _has_access_log_middleware(app)
