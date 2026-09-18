# tests/ee/sites/test_safe_fetch.py — SF-7: the single-URL SSRF-hardened fetch.
#
# Created 2026-09-18 (feat/sites-single-url-fetch). Covers ``fetch_single_url``,
# the primitive extracted out of url_crawler so a caller that needs exactly ONE
# fetch of a customer-controlled URL reuses the hardened path instead of
# hand-rolling a second, weaker one. All network is mocked (httpx.MockTransport
# + a dict-backed resolver): nothing here opens a socket.
#
# The guards these tests exist to pin, each of which a naive reimplementation
# gets wrong:
#   * a target that RESOLVES private is rejected with ZERO requests issued (not
#     rejected after the connection, which is too late);
#   * a MIXED public+private DNS answer is rejected OUTRIGHT, never filtered down
#     to the public record — filtering hands a DNS-controlling attacker a public
#     record to use as a passkey;
#   * a redirect whose target resolves private dies AT THE HOP, so the first
#     public hop cannot be used as a launder;
#   * an oversized response aborts MID-STREAM — the assertion is on how many
#     chunks the transport was asked for, because "raises eventually" is also
#     what a fully-buffering implementation does.
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.safe_fetch import FetchError, fetch_single_url

_PUBLIC = {
    "example.com": ["93.184.216.34"],
    "other.example": ["93.184.216.35"],
}


def _resolver(table: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError(f"no DNS for {host}")
        return table[host]

    return resolve


def _transport(routes: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request]):
    """MockTransport routing on (Host header, path). The pinned request carries
    the validated IP in the URL, so the ORIGINAL host is only in the header."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes.get(
            (request.headers.get("host", ""), request.url.path),
            httpx.Response(404, content=b"not found"),
        )

    return httpx.MockTransport(handler)


async def _fetch(routes, *, url="https://example.com/", table=None, seen=None, **kw):
    seen = seen if seen is not None else []
    return await fetch_single_url(
        url,
        transport=_transport(routes, seen),
        resolver=_resolver(_PUBLIC if table is None else table),
        **kw,
    )


# --------------------------------------------------------------------------- #
# The happy path + the pin
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetches_one_url_and_pins_the_connection_to_the_resolved_ip():
    """The body comes back, and the socket-level request went to the RESOLVED IP
    while the Host header carried the real name. That is the TOCTOU closure: a
    re-resolution between check and fetch cannot happen because httpx is never
    handed the hostname."""
    seen: list[httpx.Request] = []
    routes = {
        ("example.com", "/.well-known/paw-verify"): httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"token-abc"
        )
    }
    result = await _fetch(routes, url="https://example.com/.well-known/paw-verify", seen=seen)
    assert result.status == 200
    assert result.body == b"token-abc"
    assert result.content_type == "text/plain"
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.com"
    assert seen[0].extensions.get("sni_hostname") == "example.com"


@pytest.mark.asyncio
async def test_non_2xx_is_returned_not_raised():
    """A 404 from the customer's server is the CALLER's decision (an absent
    verification token is not an SSRF event), so the status comes back."""
    result = await _fetch({})
    assert result.status == 404


# --------------------------------------------------------------------------- #
# SSRF floors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/.well-known/paw-verify",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "http://example.com:8080/",
    ],
)
@pytest.mark.asyncio
async def test_rejects_forbidden_url_shapes_before_any_request(url):
    """A literal private/metadata address, a non-http scheme, credentials in the
    URL and a non-standard port all die at validation — before DNS, before a
    socket. ``seen`` staying empty is the point."""
    seen: list[httpx.Request] = []
    with pytest.raises(ValidationError) as exc:
        await _fetch({}, url=url, seen=seen)
    assert exc.value.code in ("sites.import_url_invalid", "sites.import_url_forbidden")
    assert seen == []


@pytest.mark.asyncio
async def test_rejects_a_host_that_resolves_to_a_private_address():
    """The URL shape is fine and the host is a real hostname — the rejection has
    to come from the DNS answer, and it has to come BEFORE the request."""
    seen: list[httpx.Request] = []
    with pytest.raises(ValidationError) as exc:
        await _fetch(
            {},
            url="https://rebind.example/",
            table={"rebind.example": ["10.1.2.3"]},
            seen=seen,
        )
    assert exc.value.code == "sites.import_url_forbidden"
    assert "private address" in str(exc.value)
    assert seen == []


@pytest.mark.asyncio
async def test_rejects_a_mixed_public_and_private_dns_answer_outright():
    """ALL resolved addresses must pass. A host answering with one public and one
    private record is rejected WHOLE — an implementation that filtered down to
    the public record would fetch happily here, which is why this asserts zero
    requests rather than just "raises"."""
    seen: list[httpx.Request] = []
    with pytest.raises(ValidationError) as exc:
        await _fetch(
            {},
            url="https://mixed.example/",
            table={"mixed.example": ["93.184.216.34", "192.168.0.7"]},
            seen=seen,
        )
    assert exc.value.code == "sites.import_url_forbidden"
    assert seen == []


@pytest.mark.asyncio
async def test_rejects_a_mixed_answer_whose_private_record_is_last():
    """Order must not matter: the loop has to check every record, not stop at the
    first public one."""
    with pytest.raises(ValidationError):
        await _fetch(
            {},
            url="https://mixed.example/",
            table={"mixed.example": ["93.184.216.34", "8.8.8.8", "::1"]},
        )


@pytest.mark.asyncio
async def test_dns_failure_fails_closed_as_a_fetch_error():
    with pytest.raises(FetchError) as exc:
        await _fetch({}, url="https://nowhere.example/", table={})
    assert exc.value.code == "sites.import_crawl_dns_failed"


# --------------------------------------------------------------------------- #
# Redirects — every hop is revalidated
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rejects_a_redirect_whose_target_resolves_private():
    """The first hop is a legitimate public host; the Location points at a host
    that resolves to RFC1918. The hop must be revalidated, so the second request
    is never issued — ``seen`` holds exactly the one public hop."""
    seen: list[httpx.Request] = []
    routes = {
        ("example.com", "/"): httpx.Response(
            302, headers={"location": "http://internal.example/admin"}
        )
    }
    with pytest.raises(ValidationError) as exc:
        await _fetch(
            routes,
            table={**_PUBLIC, "internal.example": ["172.16.4.4"]},
            seen=seen,
        )
    assert exc.value.code == "sites.import_url_forbidden"
    assert len(seen) == 1
    assert seen[0].headers["host"] == "example.com"


@pytest.mark.asyncio
async def test_rejects_a_redirect_to_a_non_http_scheme():
    routes = {("example.com", "/"): httpx.Response(302, headers={"location": "file:///etc/passwd"})}
    with pytest.raises(ValidationError) as exc:
        await _fetch(routes)
    assert exc.value.code == "sites.import_url_invalid"


@pytest.mark.asyncio
async def test_follows_a_public_redirect_and_reports_the_final_url():
    routes = {
        ("example.com", "/"): httpx.Response(
            301, headers={"location": "https://other.example/final"}
        ),
        ("other.example", "/final"): httpx.Response(200, content=b"ok"),
    }
    result = await _fetch(routes)
    assert result.url == "https://other.example/final"
    assert result.body == b"ok"


@pytest.mark.asyncio
async def test_allowed_host_pins_the_hop_chain_to_one_host():
    """``allowed_host`` is the orthogonal SCOPE guard: the redirect target is
    perfectly public, and it is still refused for leaving the host."""
    routes = {
        ("example.com", "/"): httpx.Response(
            302, headers={"location": "https://other.example/elsewhere"}
        )
    }
    with pytest.raises(FetchError) as exc:
        await _fetch(routes, allowed_host="example.com")
    assert exc.value.code == "sites.import_crawl_offsite_redirect"


@pytest.mark.asyncio
async def test_a_redirect_loop_stops_at_the_hop_cap():
    routes = {
        ("example.com", "/"): httpx.Response(302, headers={"location": "https://example.com/"})
    }
    with pytest.raises(FetchError) as exc:
        await _fetch(routes)
    assert "too many redirects" in str(exc.value)


# --------------------------------------------------------------------------- #
# The size cap is enforced on the STREAM
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_oversized_response_aborts_mid_stream_without_buffering_it():
    """The cap is 1KB and the body is 100KB in 1KB chunks. The assertion is on
    CHUNKS PULLED, not just on the raise: an implementation that read the whole
    response and then compared lengths would also raise here, while having held
    100KB of hostile body in memory. Two chunks is the fewest that can cross a
    one-chunk cap."""
    pulled = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal pulled
        for _ in range(100):
            pulled += 1
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    with pytest.raises(FetchError) as exc:
        await fetch_single_url(
            "https://example.com/big",
            max_bytes=1024,
            transport=httpx.MockTransport(handler),
            resolver=_resolver(_PUBLIC),
        )
    assert exc.value.code == "sites.import_crawl_response_too_large"
    assert pulled == 2, f"read {pulled} chunks - the stream was not aborted at the cap"


@pytest.mark.asyncio
async def test_declared_content_length_over_the_cap_is_refused_before_the_body():
    """A believable content-length above the cap is refused without reading the
    body at all."""
    pulled = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal pulled
        pulled += 1
        yield b"x" * 4096

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "4096"}, content=body())

    with pytest.raises(FetchError) as exc:
        await fetch_single_url(
            "https://example.com/big",
            max_bytes=1024,
            transport=httpx.MockTransport(handler),
            resolver=_resolver(_PUBLIC),
        )
    assert exc.value.code == "sites.import_crawl_response_too_large"
    assert pulled == 0


@pytest.mark.asyncio
async def test_a_response_at_the_cap_is_allowed_through():
    """The cap is a ceiling, not an off-by-one: exactly max_bytes must pass."""
    routes = {("example.com", "/"): httpx.Response(200, content=b"y" * 1024)}
    result = await _fetch(routes, max_bytes=1024)
    assert len(result.body) == 1024


# --------------------------------------------------------------------------- #
# The crawler still reaches the same machinery
# --------------------------------------------------------------------------- #


def test_url_crawler_reexports_the_moved_names_as_the_same_objects():
    """import_service and the existing crawler suite import these from
    url_crawler. The extraction keeps them as the SAME class objects, so
    ``except CrawlError`` around a safe_fetch call still catches."""
    from pocketpaw_ee.sites import safe_fetch, url_crawler

    assert url_crawler.CrawlError is safe_fetch.FetchError
    assert url_crawler.CrawlBudgetExceeded is safe_fetch.FetchBudgetExceeded
    assert url_crawler.validate_seed_url is safe_fetch.validate_fetch_url
    assert url_crawler.SafeFetcher is safe_fetch.SafeFetcher
