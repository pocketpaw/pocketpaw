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
