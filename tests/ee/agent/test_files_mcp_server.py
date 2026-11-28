# tests/ee/agent/test_files_mcp_server.py
# Created: 2026-09-14 (fix/attachment-not-on-disk) — pins the uploaded-files MCP
# surface: the tool ids it advertises, its workspace scoping, and the
# ``hide_from_ai`` gate on BOTH of its doors.
#
# Why this server exists: uploads live in S3 and the agent had no tool that
# could reach one. Attachments are inlined into the prompt only for the turn
# they arrived on, and ``load_history_for_scope`` carries ``{role, content}``
# only — so from the next turn a document is unreachable. The claude_agent_sdk
# backend then did what a filesystem agent does: globbed its local ``cwd`` jail,
# found nothing, and told the user no file existed. It was right; the file was
# in object storage and it had no tool.
#
# What these prove, in the order they would bite:
#   * the provider advertises exactly the two ids the server hosts (the
#     per-server count-pin convention every sibling server follows);
#   * reads are workspace-scoped, and a miss reads as "not found" rather than
#     leaking that the id exists in another tenant;
#   * a ``hide_from_ai`` file is refused by read_upload EVEN WHEN a live
#     extraction would have succeeded — the fallback must not walk around the
#     gate that ``load_extracted_text`` just enforced;
#   * listing asks the store for AI-visible rows only, rather than filtering
#     after the fact (FileRecord does not even carry the flag, so a post-hoc
#     filter would be a silent no-op);
#   * a binary with no extractable text returns text=None WITH a note, instead
#     of an empty string the model would read as an empty document.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from pocketpaw_ee.agent.mcp_servers import files as files_mcp

pytestmark = pytest.mark.asyncio


def _body(resp: dict) -> Any:
    """Decode a success response's JSON payload."""
    return json.loads(resp["content"][0]["text"])


@dataclass
class FakeDoc:
    filename: str = "quarterly-report.pdf"
    mime: str = "application/pdf"
    hide_from_ai: bool = False
    extracted_text_key: str | None = "blob/1"
    extracted_text_version: int | None = 1
    content_version: int = 1
    file_id: str = "f1"
    size: int | None = None


@dataclass
class FakeRec:
    id: str
    filename: str
    mime: str = "text/plain"
    size: int = 10
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    created: str = "2026-09-14T00:00:00Z"


class FakeStore:
    """Records what it was asked for, so scoping can be asserted."""

    def __init__(self, *, doc: FakeDoc | None = None, records: list[FakeRec] | None = None) -> None:
        self._doc = doc
        self._records = records or []
        self.list_kwargs: dict[str, Any] = {}
        self.get_args: tuple[Any, ...] = ()

    async def get_doc_scoped(self, file_id, workspace):
        self.get_args = (file_id, workspace)
        return self._doc

    async def list_by_workspace(self, workspace, **kwargs):
        self.list_kwargs = {"workspace": workspace, **kwargs}
        return self._records


def _install(monkeypatch, *, store, workspace="ws1", text=None):
    monkeypatch.setattr(files_mcp, "_store", lambda: store)
    monkeypatch.setattr(files_mcp, "_identity", lambda: (workspace, "u1"))

    async def _live(_file_id, _workspace):
        return text

    monkeypatch.setattr(files_mcp, "_live_extract", _live)


async def test_provider_advertises_exactly_the_hosted_tool_ids() -> None:
    from pocketpaw_ee.extensions import CloudFilesMcpProvider

    assert set(CloudFilesMcpProvider().tool_ids()) == set(files_mcp.FILES_TOOL_IDS)
    assert len(files_mcp.FILES_TOOL_IDS) == 2


async def test_read_is_scoped_to_the_runs_own_workspace(monkeypatch) -> None:
    """The workspace comes from the ContextVar, never from tool args — otherwise
    the tool is a cross-tenant read primitive."""
    store = FakeStore(doc=None)
    _install(monkeypatch, store=store, workspace="ws-mine")

    resp = await files_mcp._read_handler({"file_id": "f1", "workspace": "ws-theirs"})

    assert store.get_args == ("f1", "ws-mine")
    assert resp.get("is_error") is True
    # A miss must not disclose that the id exists somewhere else.
    assert "not found" in resp["content"][0]["text"].lower() or "no file" in (
        resp["content"][0]["text"].lower()
    )


async def test_a_hidden_file_is_refused_even_when_extraction_would_work(monkeypatch) -> None:
    """The whole point of the gate. The live-extraction fallback must not route
    around the refusal the stored-text door makes."""
    store = FakeStore(doc=FakeDoc(hide_from_ai=True, extracted_text_key=None))
    # A fallback that WOULD have produced text, to prove it is never consulted.
    _install(monkeypatch, store=store, text="secret contents")

    resp = await files_mcp._read_handler({"file_id": "f1"})

    assert resp.get("is_error") is True
    assert "hidden from ai" in resp["content"][0]["text"].lower()
    assert "secret contents" not in resp["content"][0]["text"]


async def test_listing_asks_the_store_for_ai_visible_rows_only(monkeypatch) -> None:
    """FileRecord carries no hide_from_ai, so filtering must happen in the query.
    A post-hoc filter here would be a silent no-op."""
    store = FakeStore(records=[FakeRec(id="a", filename="brief.txt")])
    _install(monkeypatch, store=store)

    resp = await files_mcp._list_handler({})

    assert store.list_kwargs["ai_visible_only"] is True
    assert store.list_kwargs["workspace"] == "ws1"
    assert _body(resp)["files"][0]["file_id"] == "a"


async def test_query_filters_by_filename(monkeypatch) -> None:
    store = FakeStore(
        records=[FakeRec(id="a", filename="brief.txt"), FakeRec(id="b", filename="logo.png")]
    )
    _install(monkeypatch, store=store)

    body = _body(await files_mcp._list_handler({"query": "BRIEF"}))

    assert [f["file_id"] for f in body["files"]] == ["a"]
    assert body["count"] == 1


async def test_a_binary_reports_no_text_rather_than_an_empty_document(monkeypatch) -> None:
    """text="" would read as a document that says nothing, which is how a model
    ends up confidently summarising an empty string."""
    store = FakeStore(doc=FakeDoc(filename="logo.png", mime="image/png", extracted_text_key=None))
    _install(monkeypatch, store=store, text=None)

    body = _body(await files_mcp._read_handler({"file_id": "f1"}))

    assert body["text"] is None
    assert "do not invent" in body["note"].lower()


async def test_long_text_is_truncated_and_says_so(monkeypatch) -> None:
    store = FakeStore(doc=FakeDoc(extracted_text_key=None))
    _install(monkeypatch, store=store, text="x" * (files_mcp._READ_CHARS_CAP + 500))

    body = _body(await files_mcp._read_handler({"file_id": "f1"}))

    assert body["truncated"] is True
    assert len(body["text"]) <= files_mcp._READ_CHARS_CAP


async def test_no_workspace_on_the_run_fails_closed(monkeypatch) -> None:
    store = FakeStore(doc=FakeDoc())
    monkeypatch.setattr(files_mcp, "_store", lambda: store)
    monkeypatch.setattr(files_mcp, "_identity", lambda: (None, None))

    assert (await files_mcp._read_handler({"file_id": "f1"})).get("is_error") is True
    assert (await files_mcp._list_handler({})).get("is_error") is True


# --- code-like uploads (HTML / JS / CSS / SVG) ------------------------------
#
# A saved Claude artifact page is a <title>, an empty root div, a big inline
# script and base64 images. Its stored extraction (made at upload time by a
# visible-text pass) is the title alone. read_upload must ignore that and read
# the blob as source, or the agent gets one line and truncated=false.


def _artifact_html() -> str:
    import base64

    blob = base64.b64encode(bytes(range(256)) * 200).decode()
    script = "function renderSlide(n){return n;}\n" * 4000
    return (
        "<!DOCTYPE html><html><head><title>The Last Boring Slide (Copy)</title></head>"
        f"<body><div id='root'></div><img src=\"data:image/png;base64,{blob}\">"
        f"<script>{script}</script></body></html>"
    )


def _install_blob(monkeypatch, tmp_path, body: str, *, title_only_stored: bool = True):
    """Point the upload resolver at a real file, and make the stored
    extraction the title-only text the bug produced."""
    from contextlib import asynccontextmanager

    from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult
    from pocketpaw_ee.cloud.uploads import extracted_text, resolver

    path = tmp_path / "artifact.html"
    path.write_text(body, encoding="utf-8")

    class _Resolver:
        @asynccontextmanager
        async def open_local_for_id(self, file_id, workspace):
            assert workspace == "ws1"
            yield FakeRec(id="f1", filename=path.name, mime="text/html"), path

    monkeypatch.setattr(resolver, "default_resolver", lambda: _Resolver())

    async def _stored(_doc, **_kw):
        if title_only_stored:
            return ExtractionResult(text="The Last Boring Slide (Copy)", backend="local")
        return None

    monkeypatch.setattr(extracted_text, "load_extracted_text", _stored)


async def test_an_html_upload_returns_its_source_not_just_its_title(monkeypatch, tmp_path) -> None:
    body = _artifact_html()
    store = FakeStore(doc=FakeDoc(filename="The Last Boring Slide (Copy).html", mime="text/html"))
    _install(monkeypatch, store=store)
    _install_blob(monkeypatch, tmp_path, body)

    out = _body(await files_mcp._read_handler({"file_id": "f1"}))

    assert out["text"] != "The Last Boring Slide (Copy)"
    assert out["text"].startswith("<!DOCTYPE html>")
    assert "<script>" in out["text"]
    assert "base64,[" in out["text"]  # the blob left a marker, not its bytes
    assert out["content_dropped"] is True
    assert out["truncated"] is True  # ~150k chars of script > the read cap
    assert out["total_chars"] > files_mcp._READ_CHARS_CAP
    assert out["next_offset"] == files_mcp._READ_CHARS_CAP


async def test_offset_pages_through_a_long_source(monkeypatch, tmp_path) -> None:
    body = _artifact_html()
    store = FakeStore(doc=FakeDoc(filename="page.html", mime="text/html; charset=utf-8"))
    _install(monkeypatch, store=store)
    _install_blob(monkeypatch, tmp_path, body)

    first = _body(await files_mcp._read_handler({"file_id": "f1"}))
    last = _body(
        await files_mcp._read_handler({"file_id": "f1", "offset": first["total_chars"] - 50})
    )

    assert last["offset"] == first["total_chars"] - 50
    assert last["text"].endswith("</html>")
    assert last["truncated"] is False
    assert "next_offset" not in last


async def test_a_hidden_html_file_is_refused_before_the_blob_is_read(monkeypatch, tmp_path) -> None:
    store = FakeStore(doc=FakeDoc(filename="page.html", mime="text/html", hide_from_ai=True))
    _install(monkeypatch, store=store)
    _install_blob(monkeypatch, tmp_path, _artifact_html())

    resp = await files_mcp._read_handler({"file_id": "f1"})

    assert resp.get("is_error") or resp.get("isError")
    assert "hidden" in resp["content"][0]["text"]


# --- the real resolver, not a fake one ---------------------------------------
#
# The tests above stub ``default_resolver``, which hid the production bug: the
# handler built ``upload://<id>``, a URL ``parse_upload_url`` has never
# accepted, so every live read in prod resolved to None and quietly fell back
# to the stored title-only text. These go through ``EEUploadResolver`` itself.


def _install_real_resolver(monkeypatch, tmp_path, body: str, *, mime: str = "text/html"):
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.uploads import resolver

    from pocketpaw.uploads.file_store import FileRecord
    from pocketpaw.uploads.local import LocalStorageAdapter

    root = tmp_path / "blobs"
    (root / "chat").mkdir(parents=True)
    (root / "chat" / "f1.html").write_text(body, encoding="utf-8")
    rec = FileRecord(
        id="f1",
        storage_key="chat/f1.html",
        filename="The Last Boring Slide (Copy).html",
        mime=mime,
        size=len(body),
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC),
    )

    class _Meta:
        async def get_scoped(self, file_id, workspace):
            return rec if (file_id, workspace) == ("f1", "ws1") else None

    real = resolver.EEUploadResolver(adapter=LocalStorageAdapter(root), meta=_Meta())
    monkeypatch.setattr(resolver, "default_resolver", lambda: real)


async def test_read_upload_reads_the_blob_through_the_real_resolver(monkeypatch, tmp_path) -> None:
    body = _artifact_html()
    doc = FakeDoc(filename="The Last Boring Slide (Copy).html", mime="text/html", size=len(body))
    _install(monkeypatch, store=FakeStore(doc=doc))
    _install_blob(monkeypatch, tmp_path, body)  # title-only stored text
    _install_real_resolver(monkeypatch, tmp_path, body)

    out = _body(await files_mcp._read_handler({"file_id": "f1"}))

    assert "<script>" in out["text"]
    assert out["total_chars"] > 1000


async def test_live_extract_also_reaches_the_blob(monkeypatch, tmp_path) -> None:
    """The non-source fallback used the same broken URL."""
    _install_real_resolver(monkeypatch, tmp_path, "plain words here", mime="text/plain")

    text = await files_mcp._live_extract("f1", "ws1")

    assert text and "plain words here" in text


async def test_short_text_from_a_big_text_file_says_content_was_dropped(monkeypatch) -> None:
    """28 chars back from a 5 MB HTML file must never report content_dropped=false,
    whichever path produced the text."""
    doc = FakeDoc(filename="page.html", mime="text/html", size=5_039_647)
    _install(monkeypatch, store=FakeStore(doc=doc))

    async def _no_source(_fid, _ws):
        return None

    async def _stored(_doc, **_kw):
        from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

        return ExtractionResult(text="The Last Boring Slide (Copy)", backend="local")

    from pocketpaw_ee.cloud.uploads import extracted_text

    monkeypatch.setattr(files_mcp, "_live_source", _no_source)
    monkeypatch.setattr(extracted_text, "load_extracted_text", _stored)

    out = _body(await files_mcp._read_handler({"file_id": "f1"}))

    assert out["total_chars"] == 28
    assert out["content_dropped"] is True
    assert "5039647" in out["note"]


async def test_a_truncated_page_tells_the_agent_to_keep_reading(monkeypatch, tmp_path) -> None:
    body = _artifact_html()
    doc = FakeDoc(filename="page.html", mime="text/html", size=len(body))
    _install(monkeypatch, store=FakeStore(doc=doc))
    _install_blob(monkeypatch, tmp_path, body)
    _install_real_resolver(monkeypatch, tmp_path, body)

    out = _body(await files_mcp._read_handler({"file_id": "f1"}))

    assert out["truncated"] is True
    assert f"offset={out['next_offset']}" in out["note"]


# --- query: jump to a section instead of paging ------------------------------
#
# A bundled artifact page is ~1.5M chars of source, about 37 pages. ``query``
# finds a substring in the SAME stripped text ``offset`` pages, so every match
# offset plugs straight into ``read_upload(file_id, offset=...)``.


def _slide_html(n_slides: int) -> str:
    vendor = "function vendorCode(a){return a*2;}\n" * 3000
    slides = "".join(
        f"<section class='slide'><h2>Slide Title {i}</h2></section>\n" for i in range(n_slides)
    )
    return (
        "<!DOCTYPE html><html><head><title>Deck</title></head>"
        f"<body><script>{vendor}</script>{slides}</body></html>"
    )


def _install_real_html(monkeypatch, tmp_path, body: str) -> None:
    doc = FakeDoc(filename="deck.html", mime="text/html", size=len(body))
    _install(monkeypatch, store=FakeStore(doc=doc))
    _install_blob(monkeypatch, tmp_path, body)  # stored text is title-only
    _install_real_resolver(monkeypatch, tmp_path, body)


async def test_query_offsets_land_on_the_match(monkeypatch, tmp_path) -> None:
    _install_real_html(monkeypatch, tmp_path, _slide_html(3))

    out = _body(await files_mcp._read_handler({"file_id": "f1", "query": "slide title"}))

    assert out["total_matches"] == 3
    assert len(out["matches"]) == 3
    for m in out["matches"]:
        assert "Slide Title" in m["snippet"]
        page = _body(await files_mcp._read_handler({"file_id": "f1", "offset": m["offset"]}))
        assert page["text"].lower().startswith("slide title")
    # the slides sit past the first page, which is the point of searching
    assert out["matches"][0]["offset"] > files_mcp._READ_CHARS_CAP


async def test_query_caps_matches_and_says_how_to_get_the_rest(monkeypatch, tmp_path) -> None:
    _install_real_html(monkeypatch, tmp_path, _slide_html(25))

    first = _body(await files_mcp._read_handler({"file_id": "f1", "query": "Slide Title"}))

    assert first["total_matches"] == 25
    assert len(first["matches"]) == files_mcp._QUERY_MAX_MATCHES
    nxt = first["next_offset"]
    assert f"offset={nxt}" in first["note"]
    rest = _body(
        await files_mcp._read_handler({"file_id": "f1", "query": "Slide Title", "offset": nxt})
    )
    assert len(rest["matches"]) == 25 - files_mcp._QUERY_MAX_MATCHES
    assert "next_offset" not in rest


async def test_query_with_no_match_says_so(monkeypatch, tmp_path) -> None:
    _install_real_html(monkeypatch, tmp_path, _slide_html(2))

    out = _body(await files_mcp._read_handler({"file_id": "f1", "query": "no such words"}))

    assert out["total_matches"] == 0
    assert out["matches"] == []
    assert "no match" in out["note"].lower()


async def test_an_overlong_query_is_refused(monkeypatch, tmp_path) -> None:
    _install_real_html(monkeypatch, tmp_path, _slide_html(1))

    resp = await files_mcp._read_handler({"file_id": "f1", "query": "x" * 201})

    assert resp.get("is_error")


async def test_the_paging_note_points_at_query(monkeypatch, tmp_path) -> None:
    _install_real_html(monkeypatch, tmp_path, _slide_html(1))

    out = _body(await files_mcp._read_handler({"file_id": "f1"}))

    assert out["truncated"] is True
    assert "query" in out["note"]
