# tests/cloud/test_paw_bar_concierge_sources.py — uploads and single links as
# concierge knowledge (CR-9).
#
# Created: 2026-09-28 (feat/concierge-knowledge-sources). An owner adds a file
# (PDF, DOCX, Markdown, text) or one web page through
# GET/POST/DELETE /paw-bar/admin/site/{id}/knowledge/sources (+ …/{source_id}/refetch
# for links). Each source is extracted and compiled into the site pocket KB,
# ``pocket:<pocket_id>``, in the background, and the row carries its status. These
# tests pin:
#
#   * each file type reaches the kb as text, in the site pocket scope;
#   * the byte cap, the zip-bomb ceiling, and the per-plan count cap;
#   * the MIME is sniffed from the bytes: a disguised file is refused whatever its
#     extension or the client's Content-Type say;
#   * links go through the SSRF-safe fetcher: loopback, private ranges, metadata
#     and non-http schemes are refused at add time, and a redirect to a private
#     address is caught by the per-hop re-check during the fetch;
#   * the status lifecycle: processing, ready, failed{reason}, too_large,
#     unsupported, blocked, and a stuck row read as failed/interrupted;
#   * every ingest, engine or parser failure logs its cause with the source id
#     (a missing parser at error: a deployment fault, not the owner's file);
#   * removal and ``delete_sources`` un-index, without touching an article another
#     source or the page sync still holds; a row removed mid-ingest leaves no orphan;
#   * tenancy (404) and the role gate (403), nothing written;
#   * a v2 visitor turn quotes an uploaded document as a retrieved item.
#
# Guarded by tests/mutations/concierge_knowledge_sources.json (SSRF, MIME sniff,
# caps, tenant scoping).

from __future__ import annotations

import io
import logging
import socket
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.cloud.test_paw_bar_concierge_v2 import (
    _chat,
    _seed_kb,
    _site,
    _widget,
    concierge_client,  # noqa: F401 — fixture, requested by name below
    model,  # noqa: F401 — fixture, requested by name below
)

_BASE = "/paw-bar/admin/site/{sid}/knowledge/sources"
_FACT = "Brew & Co ships whole beans to Canada for a flat $9."
_PAGE_FACT = "Returns are accepted on unopened bags within 30 days."


def _build_app(role: str = "admin") -> FastAPI:
    from pocketpaw_ee.paw_bar.knowledge_routes import router

    from tests.cloud.conftest import override_workspace_role

    app = FastAPI()
    app.include_router(router)
    override_workspace_role(app, role=role, workspace_id="ws-1")
    return app


@pytest_asyncio.fixture
async def owner(mongo_db):
    transport = ASGITransport(app=_build_app("admin"))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest_asyncio.fixture
async def member(mongo_db):
    transport = ASGITransport(app=_build_app("member"))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
def v2(request):
    return request.getfixturevalue("concierge_client"), request.getfixturevalue("model")


@pytest.fixture
def caps(monkeypatch):
    """Pin the source caps the routes read."""
    from pocketpaw_ee.paw_bar import knowledge_routes

    from pocketpaw.config import get_settings

    def _pin(
        free: int = 5,
        site: int = 20,
        staff: int = 50,
        max_bytes: int = 1024 * 1024,
        max_chars: int = 100_000,
    ) -> None:
        pinned = get_settings().model_copy(
            update={
                "pawbar_concierge_source_max_count_free": free,
                "pawbar_concierge_source_max_count_site": site,
                "pawbar_concierge_source_max_count_staff": staff,
                "pawbar_concierge_source_max_bytes": max_bytes,
                "pawbar_concierge_source_max_chars": max_chars,
            }
        )
        monkeypatch.setattr(knowledge_routes, "_settings", lambda: pinned)

    _pin()
    return _pin


class _Jobs:
    """Captures the background ingests the routes schedule, to run them on demand."""

    def __init__(self) -> None:
        self.pending: list[Any] = []

    def schedule(self, coro: Any) -> None:
        self.pending.append(coro)

    async def run(self) -> None:
        while self.pending:
            await self.pending.pop(0)


@pytest.fixture
def jobs(monkeypatch) -> _Jobs:
    from pocketpaw_ee.paw_bar import knowledge_routes

    j = _Jobs()
    monkeypatch.setattr(knowledge_routes, "_schedule", j.schedule)
    yield j
    for coro in j.pending:
        coro.close()


class _Kb:
    """The kb-go write boundary: what was ingested where, and what was deleted."""

    def __init__(self) -> None:
        self.ingested: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str]] = []
        self.next_ids: list[str] = []
        self.fail: BaseException | None = None

    async def ingest(self, scope: str, text: str, source: str = "manual") -> dict:
        if self.fail is not None:
            raise self.fail
        self.ingested.append((scope, text, source))
        article = self.next_ids.pop(0) if self.next_ids else f"art-{len(self.ingested)}"
        return {"article": article, "compiled_with": "test"}

    async def remove(self, scope: str, article_id: str) -> bool:
        self.removed.append((scope, article_id))
        return True


@pytest.fixture
def kb(monkeypatch) -> _Kb:
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    fake = _Kb()
    monkeypatch.setattr(KnowledgeService, "ingest_text_to_scope", staticmethod(fake.ingest))
    monkeypatch.setattr(KnowledgeService, "remove_article", staticmethod(fake.remove))
    return fake


class _Web:
    """Fake DNS plus a fake origin behind the REAL safe_fetch path, so the SSRF
    checks (public-IP validation on every hop) run for real, with no network."""

    def __init__(self) -> None:
        self.dns: dict[str, str] = {"public.example": "93.184.216.34"}
        self.pages: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[str] = []

    def serve(self, url: str, body: bytes | str, status: int = 200, **headers: str) -> None:
        if isinstance(body, str):
            body = body.encode()
        hdrs = {"content-type": "text/html; charset=utf-8", **headers}
        self.pages[url] = (status, hdrs, body)

    async def getaddrinfo(self, host: str, port: int, **_kw: Any):
        ip = self.dns.get(host)
        if ip is None:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode()}"
        self.requests.append(url)
        status, headers, body = self.pages.get(url, (404, {"content-type": "text/html"}, b""))
        return httpx.Response(status, headers=headers, content=body)


@pytest.fixture
def web(monkeypatch) -> _Web:
    from pocketpaw.security import safe_fetch

    fake = _Web()
    monkeypatch.setattr(safe_fetch, "_get_running_loop", lambda: fake)
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(fake.handler)
    )
    return fake


# --------------------------------------------------------------------------- #
# Fixture documents
# --------------------------------------------------------------------------- #


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF whose text pypdf can extract."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def _docx(text: str) -> bytes:
    docx = pytest.importorskip(
        "docx", reason="python-docx ships in ee[extraction], which CI's `--group ee` skips"
    )
    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_PDF_FACT = _pdf("Brew and Co ships whole beans to Canada for a flat 9 dollars.")


async def _reload(site: Any):
    from pocketpaw_ee.cloud.models.site import Site

    return await Site.get(site.id)


async def _upload(
    client, sid: str, name: str, data: bytes, content_type: str = "application/octet-stream"
):
    return await client.post(_BASE.format(sid=sid), files={"file": (name, data, content_type)})


async def _link(client, sid: str, url: str):
    return await client.post(_BASE.format(sid=sid), data={"url": url})


async def _list(client, sid: str) -> dict:
    res = await client.get(_BASE.format(sid=sid))
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# 1. Each file type is extracted into the site pocket
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "make", "mime", "needle"),
    [
        ("shipping.txt", lambda: _FACT.encode(), "text/plain", _FACT),
        ("shipping.md", lambda: f"# Shipping\n\n{_FACT}\n".encode(), "text/markdown", _FACT),
        ("shipping.pdf", lambda: _PDF_FACT, "application/pdf", "ships whole beans to Canada"),
        (
            "shipping.docx",
            lambda: _docx(_FACT),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _FACT,
        ),
    ],
)
async def test_each_file_type_is_indexed_into_the_site_pocket(
    owner, caps, jobs, kb, name, make, mime, needle
):
    site = await _site()
    sid = str(site.id)
    data = make()

    res = await _upload(owner, sid, name, data)

    assert res.status_code == 202, res.text
    row = res.json()
    assert row["kind"] == "file"
    assert row["name"] == name
    assert row["status"] == "processing"
    assert row["mime"] == mime
    assert row["size_bytes"] == len(data)
    assert "storage_key" not in row and "url" in row and row["url"] is None

    await jobs.run()

    [(scope, text, source)] = kb.ingested
    assert scope == "pocket:pocket-1"
    assert needle in text
    assert name in source
    [row] = (await _list(owner, sid))["sources"]
    assert row["status"] == "ready"
    assert row["reason"] == ""
    assert row["article_ids"] == ["art-1"]
    assert row["chars"] == len(text)
    assert row["indexed_at"]


# --------------------------------------------------------------------------- #
# 2. Caps: bytes, inflated bytes, extracted chars, count per plan
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_upload_over_the_byte_cap_is_413_and_writes_nothing(owner, caps, jobs, kb):
    caps(max_bytes=2048)
    site = await _site()

    res = await _upload(owner, str(site.id), "big.txt", b"a" * 2049)

    assert res.status_code == 413
    assert res.json()["detail"] == "too_large"
    assert (await _reload(site)).concierge_sources == []
    assert jobs.pending == [] and kb.ingested == []


@pytest.mark.asyncio
async def test_an_upload_exactly_at_the_byte_cap_is_accepted(owner, caps, jobs, kb):
    caps(max_bytes=2048)
    site = await _site()

    res = await _upload(owner, str(site.id), "edge.txt", b"a" * 2048)

    assert res.status_code == 202, res.text


@pytest.mark.asyncio
async def test_a_docx_that_inflates_past_the_ceiling_is_refused_as_too_large(owner, caps, jobs, kb):
    caps(max_bytes=64 * 1024)
    bomb = _zip({"word/document.xml": b"<w:document/>", "word/media/pad.bin": b"\0" * 2_000_000})
    assert len(bomb) < 64 * 1024
    site = await _site()

    res = await _upload(owner, str(site.id), "bomb.docx", bomb)

    assert res.status_code == 413
    assert res.json()["detail"] == "too_large"
    assert (await _reload(site)).concierge_sources == []


@pytest.mark.asyncio
async def test_extracted_text_past_the_char_cap_is_cut_and_marked(owner, caps, jobs, kb):
    caps(max_chars=1_000)
    site = await _site()

    await _upload(owner, str(site.id), "long.txt", b"word " * 1_000)
    await jobs.run()

    [(_, text, _)] = kb.ingested
    assert len(text) == 1_000
    [row] = (await _list(owner, str(site.id)))["sources"]
    assert row["truncated"] is True and row["chars"] == 1_000


@pytest.mark.asyncio
async def test_the_count_cap_follows_the_site_plan(owner, caps, jobs, kb):
    caps(free=1, site=2, staff=3)
    free = await _site()
    paid = await _site(plan_tier="site", signed_key="site_key_" + "c" * 24, pocket_id="pocket-3")

    assert (await _upload(owner, str(free.id), "a.txt", b"one")).status_code == 202
    over = await _link(owner, str(free.id), "https://public.example/x")
    assert over.status_code == 409
    assert over.json()["detail"] == "over_limit"
    assert len((await _reload(free)).concierge_sources) == 1

    for n in range(2):
        assert (await _upload(owner, str(paid.id), f"{n}.txt", b"one")).status_code == 202
    assert (await _upload(owner, str(paid.id), "3.txt", b"one")).status_code == 409

    listed = await _list(owner, str(paid.id))
    assert (listed["plan"], listed["max_count"]) == ("site", 2)
    assert listed["max_bytes"] == 1024 * 1024
    assert listed["accepted_types"] == [".pdf", ".docx", ".md", ".txt"]
    assert (await _list(owner, str(free.id)))["plan"] == "free"


@pytest.mark.asyncio
async def test_the_count_cap_holds_when_two_adds_race(mongo_db):
    """Both requests loaded the site before either wrote, so both passed the early
    length check; the cap in the $push filter is what stops the second."""
    from fastapi import HTTPException
    from pocketpaw_ee.cloud.models.site import ConciergeKnowledgeSource
    from pocketpaw_ee.paw_bar.knowledge_routes import _push_source

    site = await _site()
    now = datetime.now(UTC)

    def _row(n: int) -> ConciergeKnowledgeSource:
        return ConciergeKnowledgeSource(
            id=f"r{n}", kind="file", name=f"{n}.txt", created_at=now, updated_at=now
        )

    await _push_source(site, _row(1), 2)
    await _push_source(site, _row(2), 2)
    with pytest.raises(HTTPException) as refused:
        await _push_source(site, _row(3), 2)

    assert (refused.value.status_code, refused.value.detail) == (409, "over_limit")
    assert [r.id for r in (await _reload(site)).concierge_sources] == ["r1", "r2"]


@pytest.mark.asyncio
async def test_an_unknown_or_legacy_plan_key_resolves_through_the_catalog(owner, caps, jobs, kb):
    caps(free=1, site=4, staff=9)
    legacy = await _site(plan_tier="pro")  # a pre-2026-08-22 name for "site"
    bogus = await _site(plan_tier="agency", signed_key="site_key_" + "d" * 24)

    assert (await _list(owner, str(legacy.id)))["max_count"] == 4
    assert (await _list(owner, str(bogus.id)))["max_count"] == 1


def test_the_caps_are_real_settings_with_env_overrides(monkeypatch):
    from pocketpaw.config import Settings

    monkeypatch.setenv("POCKETPAW_PAWBAR_CONCIERGE_SOURCE_MAX_COUNT_STAFF", "7")
    monkeypatch.setenv("POCKETPAW_PAWBAR_CONCIERGE_SOURCE_MAX_BYTES", "4096")
    s = Settings()
    assert s.pawbar_concierge_source_max_count_staff == 7
    assert s.pawbar_concierge_source_max_bytes == 4096
    defaults = Settings.model_fields
    assert defaults["pawbar_concierge_source_max_count_free"].default == 3
    assert defaults["pawbar_concierge_source_max_count_site"].default == 20
    assert defaults["pawbar_concierge_source_max_count_staff"].default == 50
    assert defaults["pawbar_concierge_source_max_bytes"].default == 10 * 1024 * 1024
    assert defaults["pawbar_concierge_source_max_chars"].default == 100_000


# --------------------------------------------------------------------------- #
# 3. The type comes from the bytes, never the name or the client
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "data", "claimed"),
    [
        ("menu.pdf", _PNG, "application/pdf"),  # an image wearing a PDF name
        ("notes.txt", _PDF_FACT, "text/plain"),  # a PDF wearing a text name
        ("notes.md", b"MZ\x90\x00\x03\x00\x00\x00", "text/markdown"),  # an executable
        ("report.docx", _zip({"payload.sh": b"echo hi"}), "application/msword"),  # not Word
        ("report.pdf", b"%PDX not really", "application/pdf"),
        ("script.exe", b"plain words", "text/plain"),  # a text body, an unaccepted name
        ("noext", b"plain words", "text/plain"),
        ("sheet.xlsx", _zip({"xl/workbook.xml": b"<x/>"}), "application/octet-stream"),
    ],
)
async def test_a_disguised_or_unaccepted_file_is_415_and_writes_nothing(
    owner, caps, jobs, kb, name, data, claimed
):
    site = await _site()

    res = await _upload(owner, str(site.id), name, data, content_type=claimed)

    assert res.status_code == 415, res.text
    assert res.json()["detail"] == "unsupported"
    assert (await _reload(site)).concierge_sources == []
    assert jobs.pending == []


@pytest.mark.asyncio
async def test_the_client_content_type_never_decides_the_mime(owner, caps, jobs, kb):
    site = await _site()

    res = await _upload(owner, str(site.id), "notes.txt", b"plain words", "application/pdf")

    assert res.status_code == 202
    assert res.json()["mime"] == "text/plain"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"url": ""}])
async def test_a_post_with_no_file_and_no_url_is_422(owner, caps, jobs, body):
    site = await _site()
    res = await owner.post(_BASE.format(sid=str(site.id)), data=body)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_a_post_with_both_a_file_and_a_url_is_422(owner, caps, jobs):
    site = await _site()
    res = await owner.post(
        _BASE.format(sid=str(site.id)),
        data={"url": "https://public.example/x"},
        files={"file": ("a.txt", b"one", "text/plain")},
    )
    assert res.status_code == 422
    assert (await _reload(site)).concierge_sources == []


# --------------------------------------------------------------------------- #
# 4. Links: the SSRF-safe fetcher
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8888/",
        "http://10.1.2.3/internal",
        "http://192.168.0.10/",
        "http://172.16.5.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
        "gopher://public.example/",
    ],
)
async def test_a_link_to_a_private_address_is_refused_at_add_time(owner, caps, jobs, kb, url):
    site = await _site()

    res = await _link(owner, str(site.id), url)

    assert res.status_code == 422, res.text
    assert res.json()["detail"] == "blocked"
    assert (await _reload(site)).concierge_sources == []
    assert jobs.pending == []


@pytest.mark.asyncio
async def test_a_link_is_fetched_as_text_and_indexed(owner, caps, jobs, kb, web):
    web.serve(
        "https://public.example/returns",
        f"<html><head><style>p{{}}</style><script>evil()</script></head>"
        f"<body><h1>Returns</h1><p>{_PAGE_FACT}</p></body></html>",
    )
    site = await _site()
    sid = str(site.id)

    res = await _link(owner, sid, "https://public.example/returns")
    assert res.status_code == 202, res.text
    row = res.json()
    assert (row["kind"], row["status"]) == ("link", "processing")
    assert row["url"] == row["name"] == "https://public.example/returns"
    await jobs.run()

    [(scope, text, _)] = kb.ingested
    assert scope == "pocket:pocket-1"
    assert _PAGE_FACT in text
    assert "evil()" not in text and "<p>" not in text
    [row] = (await _list(owner, sid))["sources"]
    assert (row["status"], row["mime"]) == ("ready", "text/html")


@pytest.mark.asyncio
async def test_a_redirect_to_a_private_address_is_blocked_during_the_fetch(
    owner, caps, jobs, kb, web
):
    web.dns["internal.example"] = "10.0.0.7"
    web.serve(
        "https://public.example/go", b"", status=302, location="http://internal.example/secrets"
    )
    web.serve("http://internal.example/secrets", "<p>the admin password is hunter2</p>")
    site = await _site()

    assert (await _link(owner, str(site.id), "https://public.example/go")).status_code == 202
    await jobs.run()

    assert web.requests == ["https://public.example/go"]
    assert kb.ingested == []
    [row] = (await _list(owner, str(site.id)))["sources"]
    assert row["status"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("serve", "status", "reason"),
    [
        (dict(body=b"%PDF-1.4", **{"content-type": "application/pdf"}), "unsupported", ""),
        (dict(body=b"missing", status=404), "failed", "unreachable"),
        (dict(body=b"<p>" + b"x" * 5000 + b"</p>"), "too_large", ""),
        (dict(body=b"<html><body><script>only()</script></body></html>"), "failed", "no_content"),
    ],
)
async def test_a_link_that_cannot_be_read_says_why(
    owner, caps, jobs, kb, web, serve, status, reason
):
    caps(max_bytes=4096)
    web.serve("https://public.example/page", **serve)
    site = await _site()

    assert (await _link(owner, str(site.id), "https://public.example/page")).status_code == 202
    await jobs.run()

    [row] = (await _list(owner, str(site.id)))["sources"]
    assert (row["status"], row["reason"]) == (status, reason)
    assert row["article_ids"] == []
    assert kb.ingested == []


@pytest.mark.asyncio
async def test_an_unresolvable_host_fails_as_unreachable(owner, caps, jobs, kb, web):
    site = await _site()

    assert (await _link(owner, str(site.id), "https://nowhere.example/")).status_code == 202
    await jobs.run()

    [row] = (await _list(owner, str(site.id)))["sources"]
    assert (row["status"], row["reason"]) == ("failed", "unreachable")


@pytest.mark.asyncio
async def test_refetch_reads_the_page_again_and_drops_the_old_article(owner, caps, jobs, kb, web):
    web.serve("https://public.example/returns", f"<p>{_PAGE_FACT}</p>")
    site = await _site()
    sid = str(site.id)
    kb.next_ids = ["returns-v1", "returns-v2"]
    source_id = (await _link(owner, sid, "https://public.example/returns")).json()["id"]
    await jobs.run()

    web.serve("https://public.example/returns", "<p>Returns now take 60 days.</p>")
    res = await owner.post(_BASE.format(sid=sid) + f"/{source_id}/refetch")
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "processing"
    await jobs.run()

    assert "60 days" in kb.ingested[-1][1]
    [row] = (await _list(owner, sid))["sources"]
    assert (row["status"], row["article_ids"]) == ("ready", ["returns-v2"])
    assert kb.removed == [("pocket:pocket-1", "returns-v1")]


@pytest.mark.asyncio
async def test_refetch_rechecks_the_address(owner, caps, jobs, kb, web):
    """A link row whose URL is private (written before a check existed, or by
    hand) is refused on refetch too, before anything is scheduled."""
    from pocketpaw_ee.cloud.models.site import ConciergeKnowledgeSource

    now = datetime.now(UTC)
    row = ConciergeKnowledgeSource(
        id="s1",
        kind="link",
        name="x",
        url="http://127.0.0.1/",
        status="ready",
        created_at=now,
        updated_at=now,
    )
    site = await _site(concierge_sources=[row])

    res = await owner.post(_BASE.format(sid=str(site.id)) + "/s1/refetch")

    assert res.status_code == 422
    assert res.json()["detail"] == "blocked"
    assert jobs.pending == []


# --------------------------------------------------------------------------- #
# 5. Status lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_ingest_failure_is_reported_on_the_row(owner, caps, jobs, kb):
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeEngineUnavailable

    site = await _site()
    sid = str(site.id)

    kb.fail = RuntimeError("compile failed")
    await _upload(owner, sid, "a.txt", b"some words")
    await jobs.run()
    kb.fail = KnowledgeEngineUnavailable("old binary")
    await _upload(owner, sid, "b.txt", b"other words")
    await jobs.run()
    kb.fail = None
    await _upload(owner, sid, "c.txt", b"   \n\t  ")
    await jobs.run()

    rows = {r["name"]: r for r in (await _list(owner, sid))["sources"]}
    assert (rows["a.txt"]["status"], rows["a.txt"]["reason"]) == ("failed", "ingest_failed")
    assert (rows["b.txt"]["status"], rows["b.txt"]["reason"]) == ("failed", "kb_unavailable")
    assert (rows["c.txt"]["status"], rows["c.txt"]["reason"]) == ("failed", "no_content")


@pytest.mark.asyncio
async def test_an_unreadable_pdf_fails_without_reaching_the_kb(owner, caps, jobs, kb):
    site = await _site()

    await _upload(owner, str(site.id), "broken.pdf", b"%PDF-1.4\n" + b"\x00garbage" * 50)
    await jobs.run()

    [row] = (await _list(owner, str(site.id)))["sources"]
    assert row["status"] == "failed"
    assert row["reason"] in {"unreadable", "no_content"}
    assert kb.ingested == []


def _source_logs(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "pocketpaw_ee.paw_bar.knowledge_routes"]


@pytest.mark.asyncio
async def test_an_ingest_failure_logs_its_cause(owner, caps, jobs, kb, caplog):
    """BUG (2026-10-01): a PDF failed with ingest_failed and nothing was logged,
    so the cause (a compile rejected as an echo) was invisible in Logfire."""
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeEngineUnavailable

    site = await _site()
    sid = str(site.id)

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.paw_bar.knowledge_routes"):
        kb.fail = RuntimeError("compile failed: looks like a verbatim echo")
        await _upload(owner, sid, "a.txt", b"some words")
        await jobs.run()
        kb.fail = KnowledgeEngineUnavailable("old binary")
        await _upload(owner, sid, "b.txt", b"other words")
        await jobs.run()

    rows = {r["name"]: r for r in (await _list(owner, sid))["sources"]}
    [failed, unavailable] = _source_logs(caplog)
    assert failed.levelno == logging.WARNING
    assert rows["a.txt"]["id"] in failed.getMessage()
    assert "file" in failed.getMessage()
    assert "verbatim echo" in failed.getMessage()
    assert failed.exc_info is not None
    assert unavailable.levelno == logging.WARNING
    assert rows["b.txt"]["id"] in unavailable.getMessage()
    assert "old binary" in unavailable.getMessage()
    assert unavailable.exc_info is not None


@pytest.mark.asyncio
async def test_a_missing_pdf_parser_logs_an_error_not_a_bad_file(
    owner, caps, jobs, kb, monkeypatch, caplog
):
    """A parser that is not installed fails every PDF: that is a deployment fault,
    logged at error. A file the parser rejects is the owner's, logged at warning."""
    from pocketpaw_ee.paw_bar import knowledge_sources

    def no_pypdf(*_a: Any) -> str:
        try:
            raise ImportError("No module named 'pypdf'")
        except ImportError as exc:
            raise RuntimeError("pypdf not installed — run: pip install pypdf") from exc

    def corrupt(*_a: Any) -> str:
        raise ValueError("EOF marker not found")

    site = await _site()
    sid = str(site.id)

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.paw_bar.knowledge_routes"):
        monkeypatch.setattr(knowledge_sources, "_extract_with_local", no_pypdf)
        await _upload(owner, sid, "guide.pdf", _PDF_FACT)
        await jobs.run()
        monkeypatch.setattr(knowledge_sources, "_extract_with_local", corrupt)
        await _upload(owner, sid, "other.pdf", _PDF_FACT)
        await jobs.run()

    rows = {r["name"]: r for r in (await _list(owner, sid))["sources"]}
    assert rows["guide.pdf"]["reason"] == rows["other.pdf"]["reason"] == "unreadable"
    [missing, bad] = _source_logs(caplog)
    assert missing.levelno == logging.ERROR
    assert rows["guide.pdf"]["id"] in missing.getMessage()
    assert "pypdf not installed" in missing.getMessage()
    assert bad.levelno == logging.WARNING
    assert rows["other.pdf"]["id"] in bad.getMessage()
    assert "EOF marker" in bad.getMessage()
    assert kb.ingested == []


@pytest.mark.asyncio
async def test_a_row_stuck_processing_reads_as_interrupted(owner, caps, jobs, kb, web):
    from pocketpaw_ee.cloud.models.site import ConciergeKnowledgeSource

    old = datetime.now(UTC) - timedelta(hours=1)
    fresh = datetime.now(UTC)
    rows = [
        ConciergeKnowledgeSource(
            id="stuck",
            kind="link",
            name="https://public.example/a",
            url="https://public.example/a",
            created_at=old,
            updated_at=old,
        ),
        ConciergeKnowledgeSource(
            id="busy",
            kind="link",
            name="https://public.example/b",
            url="https://public.example/b",
            created_at=fresh,
            updated_at=fresh,
        ),
    ]
    site = await _site(concierge_sources=rows)
    sid = str(site.id)

    listed = {r["id"]: r for r in (await _list(owner, sid))["sources"]}
    assert (listed["stuck"]["status"], listed["stuck"]["reason"]) == ("failed", "interrupted")
    assert listed["busy"]["status"] == "processing"

    busy = await owner.post(_BASE.format(sid=sid) + "/busy/refetch")
    assert busy.status_code == 409
    assert busy.json()["detail"] == "already_processing"
    web.serve("https://public.example/a", f"<p>{_PAGE_FACT}</p>")
    assert (await owner.post(_BASE.format(sid=sid) + "/stuck/refetch")).status_code == 202


@pytest.mark.asyncio
async def test_refetch_of_a_file_or_an_unknown_source(owner, caps, jobs, kb):
    site = await _site()
    sid = str(site.id)
    source_id = (await _upload(owner, sid, "a.txt", b"words")).json()["id"]
    await jobs.run()

    not_link = await owner.post(_BASE.format(sid=sid) + f"/{source_id}/refetch")
    assert not_link.status_code == 409
    assert not_link.json()["detail"] == "not_a_link"
    assert (await owner.post(_BASE.format(sid=sid) + "/nope/refetch")).status_code == 404
    assert (await owner.delete(_BASE.format(sid=sid) + "/nope")).status_code == 404


# --------------------------------------------------------------------------- #
# 6. Removal un-indexes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_removing_a_source_deletes_its_article(owner, caps, jobs, kb):
    site = await _site()
    sid = str(site.id)
    kb.next_ids = ["price-list"]
    source_id = (await _upload(owner, sid, "prices.txt", b"Espresso is $3.")).json()["id"]
    await jobs.run()

    res = await owner.delete(_BASE.format(sid=sid) + f"/{source_id}")

    assert res.status_code == 204
    assert kb.removed == [("pocket:pocket-1", "price-list")]
    assert (await _reload(site)).concierge_sources == []


@pytest.mark.asyncio
async def test_removal_spares_an_article_another_source_or_the_page_sync_holds(
    owner, caps, jobs, kb
):
    """kb-go keys an article by its compiled title, so two sources (or a source
    and a site page) can land on one id. Removing one must not take the other's
    knowledge with it."""
    site = await _site(kb_article_ids=["opening-hours"])
    sid = str(site.id)
    kb.next_ids = ["returns", "returns", "opening-hours"]
    a = (await _upload(owner, sid, "a.txt", b"returns one")).json()["id"]
    await _upload(owner, sid, "b.txt", b"returns two")
    c = (await _upload(owner, sid, "c.txt", b"hours")).json()["id"]
    await jobs.run()

    assert (await owner.delete(_BASE.format(sid=sid) + f"/{a}")).status_code == 204
    assert (await owner.delete(_BASE.format(sid=sid) + f"/{c}")).status_code == 204

    assert kb.removed == []
    assert [r.name for r in (await _reload(site)).concierge_sources] == ["b.txt"]


@pytest.mark.asyncio
async def test_a_source_removed_mid_ingest_leaves_no_orphan_article(owner, caps, jobs, kb):
    site = await _site()
    sid = str(site.id)
    kb.next_ids = ["late"]
    source_id = (await _upload(owner, sid, "a.txt", b"words")).json()["id"]

    assert (await owner.delete(_BASE.format(sid=sid) + f"/{source_id}")).status_code == 204
    await jobs.run()

    assert kb.removed == [("pocket:pocket-1", "late")]
    assert (await _reload(site)).concierge_sources == []


@pytest.mark.asyncio
async def test_delete_sources_clears_every_row_and_un_indexes(owner, caps, jobs, kb):
    from pocketpaw_ee.paw_bar.knowledge_routes import delete_sources

    site = await _site(kb_article_ids=["page-home"])
    sid = str(site.id)
    kb.next_ids = ["doc-a", "page-home"]
    await _upload(owner, sid, "a.txt", b"one")
    await _upload(owner, sid, "b.txt", b"two")
    await jobs.run()

    fresh = await _reload(site)
    assert await delete_sources(fresh) == 2

    assert kb.removed == [("pocket:pocket-1", "doc-a")]
    assert (await _reload(site)).concierge_sources == []
    assert await delete_sources(await _reload(site)) == 0


# --------------------------------------------------------------------------- #
# 7. Tenancy and the role gate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_verb_404s_on_another_workspaces_site(owner, caps, jobs, kb):
    from pocketpaw_ee.cloud.models.site import ConciergeKnowledgeSource

    now = datetime.now(UTC)
    row = ConciergeKnowledgeSource(
        id="theirs",
        kind="link",
        name="https://public.example/t",
        url="https://public.example/t",
        status="ready",
        article_ids=["their-art"],
        created_at=now,
        updated_at=now,
    )
    foreign = await _site(
        workspace="ws-2",
        pocket_id="pocket-2",
        signed_key="site_key_" + "b" * 24,
        concierge_sources=[row],
    )
    sid = str(foreign.id)

    assert (await owner.get(_BASE.format(sid=sid))).status_code == 404
    assert (await _upload(owner, sid, "a.txt", b"mine")).status_code == 404
    assert (await _link(owner, sid, "https://public.example/m")).status_code == 404
    assert (await owner.post(_BASE.format(sid=sid) + "/theirs/refetch")).status_code == 404
    assert (await owner.delete(_BASE.format(sid=sid) + "/theirs")).status_code == 404
    assert (await owner.get(_BASE.format(sid="not-an-object-id"))).status_code == 404

    assert [r.id for r in (await _reload(foreign)).concierge_sources] == ["theirs"]
    assert jobs.pending == [] and kb.removed == []


@pytest.mark.asyncio
async def test_a_member_is_refused_every_verb_and_nothing_is_written(member, caps, jobs, kb):
    from pocketpaw_ee.cloud.models.site import ConciergeKnowledgeSource

    now = datetime.now(UTC)
    row = ConciergeKnowledgeSource(
        id="s1",
        kind="link",
        name="https://public.example/t",
        url="https://public.example/t",
        status="ready",
        article_ids=["art"],
        created_at=now,
        updated_at=now,
    )
    site = await _site(concierge_sources=[row])
    sid = str(site.id)

    assert (await member.get(_BASE.format(sid=sid))).status_code == 403
    assert (await _upload(member, sid, "a.txt", b"mine")).status_code == 403
    assert (await _link(member, sid, "https://public.example/m")).status_code == 403
    assert (await member.post(_BASE.format(sid=sid) + "/s1/refetch")).status_code == 403
    assert (await member.delete(_BASE.format(sid=sid) + "/s1")).status_code == 403

    assert [r.id for r in (await _reload(site)).concierge_sources] == ["s1"]
    assert jobs.pending == [] and kb.removed == []


# --------------------------------------------------------------------------- #
# 8. A v2 visitor turn answers from an uploaded document
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_v2_answer_uses_an_uploaded_doc_as_a_retrieved_item(
    v2, owner, caps, jobs, kb, monkeypatch
):
    (client, store), rec = v2
    site = await _site()
    kb.next_ids = ["shipping-to-canada"]
    await _upload(owner, str(site.id), "shipping.pdf", _PDF_FACT)
    await jobs.run()
    [(scope, text, _)] = kb.ingested
    [row] = (await _reload(site)).concierge_sources
    # The retrieval side of kb-go, holding exactly what the upload ingested.
    _seed_kb(
        monkeypatch,
        {
            scope: [
                {
                    "id": row.article_ids[0],
                    "title": "Shipping to Canada",
                    "summary": "Flat-rate shipping.",
                    "content": text,
                }
            ]
        },
    )
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id, message="Do you ship to Canada?")

    assert res.status_code == 200, res.text
    prompt = rec.user_prompt()
    assert 'id="shipping-to-canada" source="pocket:pocket-1"' in prompt
    start, end = prompt.index("<knowledge>"), prompt.index("</knowledge>")
    assert start < prompt.index("ships whole beans to Canada") < end


# --------------------------------------------------------------------------- #
# 9. The production app serves the routes
# --------------------------------------------------------------------------- #


def test_mount_cloud_serves_the_source_routes():
    from fastapi.routing import APIRoute
    from pocketpaw_ee.cloud import mount_cloud

    app = FastAPI()
    mount_cloud(app)
    served = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    base = "/api/v1" + _BASE.replace("{sid}", "{site_id}")
    assert {
        ("GET", base),
        ("POST", base),
        ("DELETE", base + "/{source_id}"),
        ("POST", base + "/{source_id}/refetch"),
    } <= served


# --------------------------------------------------------------------------- #
# 10. A long document is ingested section by section
# --------------------------------------------------------------------------- #


@pytest.fixture
def sections(monkeypatch):
    """The REAL sectioned ingest under the routes, faked only at the LLM and the
    kb binary (``_kb``): ``.kb`` holds the articles, ``.compiler`` the prompts."""
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.agents import knowledge

    from tests.cloud.agents.test_knowledge_sectioned_ingest import _Compiler, _FakeKb, _install

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeKb()
    monkeypatch.setattr(knowledge, "_kb", fake)
    return SimpleNamespace(kb=fake, compiler=_install(monkeypatch, _Compiler()))


def _chapters(count: int, marker: str = "", topic: str = "Chapter") -> bytes:
    from tests.cloud.agents.test_knowledge_sectioned_ingest import _long_doc

    return _long_doc(count, marker).replace("Chapter", topic).encode()


@pytest.mark.asyncio
async def test_a_long_upload_records_every_section_article(owner, caps, jobs, sections):
    site = await _site()
    sid = str(site.id)

    source_id = (await _upload(owner, sid, "prices.md", _chapters(5))).json()["id"]
    await jobs.run()

    [row] = (await _list(owner, sid))["sources"]
    assert row["status"] == "ready"
    assert (row["sections_total"], row["sections_failed"], row["sections_truncated"]) == (5, 0, 0)
    assert sorted(row["article_ids"]) == sorted(sections.kb.articles)
    assert len(row["article_ids"]) == 5

    assert (await owner.delete(_BASE.format(sid=sid) + f"/{source_id}")).status_code == 204
    assert sorted(sections.kb.deleted) == sorted(row["article_ids"])
    assert sections.kb.articles == {}


@pytest.mark.asyncio
async def test_a_partly_failed_document_is_ready_with_counts(owner, caps, jobs, sections, caplog):
    import json as _json

    from tests.cloud.agents.test_knowledge_sectioned_ingest import _restructure

    def fail_marked(section: str, prompt: str) -> str:
        return "no" if "BROKEN" in section else _json.dumps(_restructure(section))

    sections.compiler.respond = fail_marked
    site = await _site()

    with caplog.at_level(logging.WARNING):
        await _upload(owner, str(site.id), "prices.md", _chapters(4, marker="BROKEN"))
        await jobs.run()

    [row] = (await _list(owner, str(site.id)))["sources"]
    assert (row["status"], row["reason"]) == ("ready", "")
    assert (row["sections_total"], row["sections_failed"]) == (4, 1)
    assert len(row["article_ids"]) == 3
    assert any("3 of 4 sections" in r.getMessage() for r in _source_logs(caplog))


@pytest.mark.asyncio
async def test_a_document_whose_every_section_fails_is_ingest_failed(owner, caps, jobs, sections):
    sections.compiler.respond = lambda section, prompt: "no"
    site = await _site()

    await _upload(owner, str(site.id), "prices.md", _chapters(3))
    await jobs.run()

    [row] = (await _list(owner, str(site.id)))["sources"]
    assert (row["status"], row["reason"]) == ("failed", "ingest_failed")
    assert row["article_ids"] == []
    assert sections.kb.payloads == []


@pytest.mark.asyncio
async def test_refetching_a_long_page_replaces_the_whole_old_set(owner, caps, jobs, sections, web):
    web.serve("https://public.example/prices", _chapters(4, topic="Spring").decode())
    site = await _site()
    sid = str(site.id)
    source_id = (await _link(owner, sid, "https://public.example/prices")).json()["id"]
    await jobs.run()
    [before] = (await _list(owner, sid))["sources"]
    assert len(before["article_ids"]) == 4

    web.serve("https://public.example/prices", _chapters(3, topic="Summer").decode())
    assert (await owner.post(_BASE.format(sid=sid) + f"/{source_id}/refetch")).status_code == 202
    await jobs.run()

    [after] = (await _list(owner, sid))["sources"]
    assert after["status"] == "ready" and len(after["article_ids"]) == 3
    assert not set(after["article_ids"]) & set(before["article_ids"])
    assert sorted(sections.kb.deleted) == sorted(before["article_ids"])
    assert sorted(sections.kb.articles) == sorted(after["article_ids"])


@pytest.mark.asyncio
async def test_sections_past_the_char_cap_are_counted(owner, caps, jobs, sections):
    doc = _chapters(6)
    caps(max_chars=len(doc) // 2)
    site = await _site()

    await _upload(owner, str(site.id), "prices.md", doc)
    await jobs.run()

    [row] = (await _list(owner, str(site.id)))["sources"]
    assert row["status"] == "ready" and row["truncated"] is True
    assert row["sections_truncated"] >= 2
    ingested = "".join(p["raw_text"] for p in sections.kb.payloads)
    assert "Chapter 6" not in ingested
