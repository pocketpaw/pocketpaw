# Tests for the R2 object and KV key methods of the Sites Cloudflare client
# (ee/pocketpaw_ee/sites/cloudflare_client.py), with httpx.MockTransport standing in
# for the Cloudflare API. They pin the exact wire shapes the Sites Data dashboard
# depends on:
#   * R2 object keys keep their slashes literal in the path (never %2F), other
#     characters are percent-encoded, and a key with a "." / ".." segment is refused
#     before any request (httpx would collapse it into another URL, even the bucket's).
#   * r2_list reads ``result`` + ``result_info`` (cursor, is_truncated, delimited).
#   * r2_get streams (the body is not read until iterated) and answers None on 404.
#   * r2_put sends the Content-Type we chose and a Content-Length (no chunked body),
#     and reads the string ``size`` of the upload response as an int.
#   * KV keys are percent-encoded in the path, including "/"; "." and ".." are
#     refused. kv_put is a raw body without metadata and a two-part multipart
#     (``value`` + ``metadata``) with it; ``expiration_ttl`` rides in the query.
#     A KV key with any "." / ".." segment is refused like an R2 key.
#   * deletes treat a 404 as done; any other failure raises. A bucket delete is one
#     call: teardown leaves emptying to the expire-all lifecycle rule.
#   * r2_get asks for ``identity`` and streams raw bytes, so Content-Length holds.
from __future__ import annotations

import gzip
import json
import re

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

_ACCT = "https://api.cloudflare.com/client/v4/accounts/acct_1"


def _client(handler) -> CloudflareClient:
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="",
        dispatch_namespace="",
        _transport=httpx.MockTransport(handler),
    )


class _Streaming(httpx.AsyncBaseTransport):
    """Like MockTransport, but hands the body back unread, as a real connection does
    (MockTransport reads it first, which ``aiter_raw`` refuses)."""

    def __init__(self, handler) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = self._handler(request)
        body = b"".join(resp.stream)  # the raw bytes, never decoded

        async def _chunks():
            for i in range(0, len(body), 5):
                yield body[i : i + 5]

        return httpx.Response(resp.status_code, headers=resp.headers, stream=_Body(_chunks()))


class _Body(httpx.AsyncByteStream):
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        async for chunk in self._chunks:
            yield chunk


def _stream_client(handler) -> CloudflareClient:
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="",
        dispatch_namespace="",
        _transport=_Streaming(handler),
    )


def _ok(result, **extra) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "result": result, **extra})


def _parts(body: bytes, content_type: str) -> dict[str, tuple[str, bytes]]:
    boundary = re.search(r"boundary=(.+)$", content_type).group(1).strip('"').encode()
    out: dict[str, tuple[str, bytes]] = {}
    for chunk in body.split(b"--" + boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, content = chunk.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        out[name] = (head.decode(), content)
    return out


# --------------------------------------------------------------------------- R2


@pytest.mark.asyncio
async def test_r2_list_reads_objects_folders_and_cursor():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode().split("?")[0]
        seen["params"] = dict(request.url.params)
        return _ok(
            [
                {
                    "key": "img/a.png",
                    "size": 12,
                    "etag": "e1",
                    "last_modified": "2026-10-28T10:00:00Z",
                    "http_metadata": {"contentType": "image/png"},
                }
            ],
            result_info={"cursor": "c2", "is_truncated": True, "delimited": ["img/sub/"]},
        )

    page = await _client(handler).r2_list("bkt", prefix="img/", cursor="c1", per_page=200)
    assert seen["path"] == "/client/v4/accounts/acct_1/r2/buckets/bkt/objects"
    assert seen["params"] == {"prefix": "img/", "delimiter": "/", "cursor": "c1", "per_page": "200"}
    assert page["folders"] == ["img/sub/"]
    assert page["cursor"] == "c2" and page["truncated"] is True
    assert page["objects"] == [
        {
            "key": "img/a.png",
            "size": 12,
            "etag": "e1",
            "last_modified": "2026-10-28T10:00:00Z",
            "content_type": "image/png",
        }
    ]


@pytest.mark.asyncio
async def test_r2_list_last_page_has_no_cursor():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "delimiter" not in request.url.params
        return _ok([], result_info={"cursor": "zzz", "is_truncated": False})

    page = await _client(handler).r2_list("bkt", delimiter=None, per_page=1)
    assert page == {"objects": [], "folders": [], "cursor": None, "truncated": False}


@pytest.mark.asyncio
async def test_r2_object_keys_keep_slashes_literal_and_encode_the_rest():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return _ok({})

    await _client(handler).r2_delete("bkt", "a b/c?d#e/%f.txt")
    assert seen == ["/client/v4/accounts/acct_1/r2/buckets/bkt/objects/a%20b/c%3Fd%23e/%25f.txt"]
    assert "%2F" not in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["../bkt2/x", "a/../../b", "a/./b", ".", "..", "", "a/.."])
async def test_r2_keys_with_dot_segments_never_reach_the_wire(key):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError(f"request sent: {request.url}")

    cf = _client(handler)
    for call in (cf.r2_delete("bkt", key), cf.r2_get("bkt", key)):
        with pytest.raises(ValidationError):
            await call


@pytest.mark.asyncio
async def test_r2_get_streams_and_closes():
    sent = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["n"] += 1
        assert request.method == "GET"
        assert request.url.raw_path.decode().endswith("/r2/buckets/bkt/objects/docs/a.pdf")
        return httpx.Response(
            200,
            content=b"%PDF-1.7 hello",
            headers={"content-type": "application/pdf", "etag": '"e9"'},
        )

    obj = await _stream_client(handler).r2_get("bkt", "docs/a.pdf")
    assert obj is not None and obj.content_type == "application/pdf"
    assert obj.size == len(b"%PDF-1.7 hello")
    body = b"".join([c async for c in obj.iter_bytes()])
    assert body == b"%PDF-1.7 hello" and sent["n"] == 1


@pytest.mark.asyncio
async def test_r2_get_asks_for_identity_and_streams_raw_bytes():
    """Content-Length must stay true: no transparent decompression of a stored
    gzip body, and we ask R2 not to encode in transit."""
    stored = gzip.compress(b"hello " * 50)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept-encoding")
        return httpx.Response(
            200,
            content=stored,
            headers={"content-encoding": "gzip", "content-length": str(len(stored))},
        )

    obj = await _stream_client(handler).r2_get("bkt", "a.gz")
    assert seen["accept"] == "identity"
    body = b"".join([c async for c in obj.iter_bytes()])
    assert body == stored and obj.size == len(stored)


@pytest.mark.asyncio
async def test_r2_get_missing_is_none_and_errors_raise():
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False, "errors": [{"code": 10007}]})

    assert await _client(missing).r2_get("bkt", "nope") is None

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"code": 10000}]})

    with pytest.raises(ValidationError):
        await _client(boom).r2_get("bkt", "x")


@pytest.mark.asyncio
async def test_r2_put_sends_content_type_and_length_and_reads_string_size():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.raw_path.decode()
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read()
        return _ok({"key": "up/a.png", "size": "4", "etag": "e2", "uploaded": "2026-10-28T10:00Z"})

    async def chunks():
        yield b"ab"
        yield b"cd"

    out = await _client(handler).r2_put(
        "bkt", "up/a.png", chunks(), content_type="image/png", size=4
    )
    assert seen["method"] == "PUT"
    assert seen["path"].endswith("/r2/buckets/bkt/objects/up/a.png")
    assert seen["headers"]["content-type"] == "image/png"
    assert seen["headers"]["content-length"] == "4"
    assert "transfer-encoding" not in seen["headers"]
    assert seen["body"] == b"abcd"
    assert out["size"] == 4 and out["key"] == "up/a.png" and out["etag"] == "e2"


@pytest.mark.asyncio
async def test_r2_delete_404_is_done_and_other_errors_raise():
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False, "errors": []})

    await _client(gone).r2_delete("bkt", "x")

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "errors": [{"code": 1}]})

    with pytest.raises(ValidationError):
        await _client(boom).r2_delete("bkt", "x")


@pytest.mark.asyncio
async def test_delete_r2_bucket_never_lists_or_deletes_objects():
    """Teardown keeps relying on the expire-all lifecycle rule (a day of grace on the
    draft-discard path): a bucket delete is one call, nothing else."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(409, json={"success": False, "errors": [{"code": 10008}]})

    assert await _client(handler).delete_r2_bucket("bkt") is False
    assert calls == [("DELETE", "/client/v4/accounts/acct_1/r2/buckets/bkt")]


# --------------------------------------------------------------------------- KV


@pytest.mark.asyncio
async def test_kv_list_keys():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode().split("?")[0]
        seen["params"] = dict(request.url.params)
        return _ok(
            [{"name": "a", "expiration": 1767225600, "metadata": {"x": 1}}, {"name": "b"}],
            result_info={"cursor": "", "count": 2},
        )

    page = await _client(handler).kv_list_keys("ns1", prefix="a", cursor="c", limit=200)
    assert seen["path"] == "/client/v4/accounts/acct_1/storage/kv/namespaces/ns1/keys"
    assert seen["params"] == {"prefix": "a", "cursor": "c", "limit": "200"}
    assert page == {
        "keys": [
            {"name": "a", "expiration": 1767225600, "metadata": {"x": 1}},
            {"name": "b", "expiration": None, "metadata": None},
        ],
        "cursor": None,
    }


@pytest.mark.asyncio
async def test_kv_keys_are_percent_encoded_including_slashes():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        if "/metadata/" in seen[-1]:
            return _ok({"m": 1})
        if request.method == "GET":
            return httpx.Response(200, content=b"v")
        return _ok({})

    cf = _client(handler)
    await cf.kv_get("ns1", "user/1 ?#%")
    await cf.kv_metadata("ns1", "user/1 ?#%")
    await cf.kv_delete("ns1", "user/1 ?#%")
    base = "/client/v4/accounts/acct_1/storage/kv/namespaces/ns1"
    assert seen == [
        f"{base}/values/user%2F1%20%3F%23%25",
        f"{base}/metadata/user%2F1%20%3F%23%25",
        f"{base}/values/user%2F1%20%3F%23%25",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [".", "..", "", "a/..", "../x", "a/./b", "./a"])
async def test_kv_dot_keys_never_reach_the_wire(key):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError(f"request sent: {request.url}")

    cf = _client(handler)
    for call in (cf.kv_delete("ns1", key), cf.kv_get("ns1", key), cf.kv_put("ns1", key, b"x")):
        with pytest.raises(ValidationError):
            await call


@pytest.mark.asyncio
async def test_kv_get_reports_size_utf8_and_caps_what_it_keeps():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50)

    full = await _client(handler).kv_get("ns1", "k")
    assert full.data == b"x" * 50 and full.size == 50 and full.truncated is False
    assert full.text == "x" * 50
    capped = await _client(handler).kv_get("ns1", "k", max_bytes=10)
    assert capped.data == b"x" * 10 and capped.size == 50 and capped.truncated is True
    assert capped.text is None

    def binary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\x00")

    raw = await _client(binary).kv_get("ns1", "k")
    assert raw.text is None and raw.data == b"\xff\xfe\x00"

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False, "errors": [{"code": 10009}]})

    assert await _client(missing).kv_get("ns1", "k") is None
    assert await _client(missing).kv_metadata("ns1", "k") is None


@pytest.mark.asyncio
async def test_kv_put_raw_without_metadata():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode().split("?")[0]
        seen["params"] = dict(request.url.params)
        seen["type"] = request.headers.get("content-type", "")
        seen["body"] = request.read()
        return _ok({})

    await _client(handler).kv_put("ns1", "a/b", b"hello", expiration_ttl=120)
    assert seen["path"].endswith("/storage/kv/namespaces/ns1/values/a%2Fb")
    assert seen["params"] == {"expiration_ttl": "120"}
    assert seen["body"] == b"hello"
    assert not seen["type"].startswith("multipart/")


@pytest.mark.asyncio
async def test_kv_put_with_metadata_is_multipart():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.read()
        return _ok({})

    await _client(handler).kv_put("ns1", "k", b"{}", metadata={"plan": "pro"})
    assert seen["params"] == {}
    assert seen["type"].startswith("multipart/form-data")
    parts = _parts(seen["body"], seen["type"])
    assert set(parts) == {"value", "metadata"}
    assert parts["value"][1] == b"{}"
    assert json.loads(parts["metadata"][1]) == {"plan": "pro"}


@pytest.mark.asyncio
async def test_kv_delete_404_is_done():
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False, "errors": []})

    await _client(gone).kv_delete("ns1", "k")
