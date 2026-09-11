"""Tests for http_utils.py — is_request_secure().

`is_request_secure` makes a security-relevant decision (is the original client
protocol HTTPS?) by inspecting the request scheme and two proxy-supplied
headers:

1. `X-Forwarded-Proto` — the de-facto standard, may carry a comma-separated
   chain where the *first* hop is the original client protocol.
2. RFC 7239 `Forwarded` — e.g. `Forwarded: for=1.2.3.4;proto=https`, where the
   proto token may be quoted and the casing is not significant.

These tests pin down direct-scheme handling, first-hop selection, casing /
whitespace / quoting tolerance, and the malformed-header fallbacks.
"""

from __future__ import annotations

from starlette.requests import Request

from pocketpaw.http_utils import is_request_secure


def make_request(scheme: str = "http", headers: dict[str, str] | None = None) -> Request:
    """Build a minimal ASGI `Request` with a given URL scheme and headers."""
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "scheme": scheme,
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "server": ("testserver", 80),
        "headers": raw_headers,
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Direct request scheme
# ---------------------------------------------------------------------------


class TestDirectScheme:
    def test_https_scheme_is_secure(self):
        assert is_request_secure(make_request(scheme="https")) is True

    def test_http_scheme_without_forwarding_is_not_secure(self):
        assert is_request_secure(make_request(scheme="http")) is False

    def test_https_scheme_short_circuits_conflicting_forwarded_headers(self):
        # A direct HTTPS connection wins even if a header claims otherwise.
        request = make_request(
            scheme="https",
            headers={"x-forwarded-proto": "http", "forwarded": "proto=http"},
        )
        assert is_request_secure(request) is True


# ---------------------------------------------------------------------------
# X-Forwarded-Proto header
# ---------------------------------------------------------------------------


class TestXForwardedProto:
    def test_https_value_is_secure(self):
        request = make_request(headers={"x-forwarded-proto": "https"})
        assert is_request_secure(request) is True

    def test_http_value_is_not_secure(self):
        request = make_request(headers={"x-forwarded-proto": "http"})
        assert is_request_secure(request) is False

    def test_first_hop_https_in_chain_is_secure(self):
        # The original client protocol is the first entry in the chain.
        request = make_request(headers={"x-forwarded-proto": "https,http"})
        assert is_request_secure(request) is True

    def test_first_hop_http_in_chain_is_not_secure(self):
        request = make_request(headers={"x-forwarded-proto": "http,https"})
        assert is_request_secure(request) is False

    def test_surrounding_whitespace_is_tolerated(self):
        request = make_request(headers={"x-forwarded-proto": "  https , http "})
        assert is_request_secure(request) is True

    def test_value_is_case_insensitive(self):
        request = make_request(headers={"x-forwarded-proto": "HTTPS"})
        assert is_request_secure(request) is True

    def test_empty_value_is_not_secure(self):
        request = make_request(headers={"x-forwarded-proto": ""})
        assert is_request_secure(request) is False

    def test_falls_through_to_forwarded_header_when_proto_is_http(self):
        # X-Forwarded-Proto=http does not short-circuit; the RFC 7239 header is
        # still consulted and can establish HTTPS.
        request = make_request(
            headers={"x-forwarded-proto": "http", "forwarded": "proto=https"},
        )
        assert is_request_secure(request) is True


# ---------------------------------------------------------------------------
# RFC 7239 Forwarded header
# ---------------------------------------------------------------------------


class TestForwardedHeader:
    def test_proto_https_is_secure(self):
        request = make_request(headers={"forwarded": "proto=https"})
        assert is_request_secure(request) is True

    def test_proto_http_is_not_secure(self):
        request = make_request(headers={"forwarded": "proto=http"})
        assert is_request_secure(request) is False

    def test_proto_alongside_other_params_is_secure(self):
        request = make_request(headers={"forwarded": "for=1.2.3.4;proto=https"})
        assert is_request_secure(request) is True

    def test_quoted_proto_value_is_secure(self):
        request = make_request(headers={"forwarded": 'proto="https"'})
        assert is_request_secure(request) is True

    def test_first_entry_is_used_in_chain(self):
        request = make_request(
            headers={"forwarded": "for=1.1.1.1;proto=https, for=2.2.2.2;proto=http"},
        )
        assert is_request_secure(request) is True

    def test_first_entry_http_in_chain_is_not_secure(self):
        request = make_request(
            headers={"forwarded": "for=1.1.1.1;proto=http, for=2.2.2.2;proto=https"},
        )
        assert is_request_secure(request) is False

    def test_keys_and_values_are_case_insensitive(self):
        request = make_request(headers={"forwarded": "PROTO=HTTPS"})
        assert is_request_secure(request) is True

    def test_whitespace_around_params_is_tolerated(self):
        request = make_request(headers={"forwarded": "for=1.2.3.4; proto = https"})
        assert is_request_secure(request) is True

    def test_missing_proto_param_is_not_secure(self):
        request = make_request(headers={"forwarded": "for=1.2.3.4;host=example.com"})
        assert is_request_secure(request) is False

    def test_empty_value_is_not_secure(self):
        request = make_request(headers={"forwarded": ""})
        assert is_request_secure(request) is False

    def test_malformed_value_without_assignment_is_not_secure(self):
        request = make_request(headers={"forwarded": "garbage"})
        assert is_request_secure(request) is False
