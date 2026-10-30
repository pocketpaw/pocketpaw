# test_extracted_text.py — T0 "Persist the extracted text".
# Created: 2026-08-29 (T0). Covers the whole slice against a REAL
# LocalStorageAdapter over tmp_path and real (mongomock) FileUpload rows, so
# the blob write, the blob read and the two pointer columns are all exercised
# for real rather than through a double.
#
# The load-bearing test is ``test_listener_persists_the_extraction``: T0's
# failure mode is INVISIBLE from outside (if the persist silently never runs,
# every feature still works — it just re-extracts forever and pays for it), so
# the seam has to be asserted directly. Every other test here is about the
# refusals, because a reader that quietly serves the wrong text is worse than
# one that re-extracts.
#
# Covered:
#   (1) persist -> load round-trips the FULL ExtractionResult (title and
#       captions too, not just text — comprehension reads all three);
#   (2) the listener actually writes the blob AND both columns on a real
#       ingest, and records the version the row was at;
#   (3) a persist failure loses NOTHING — tags, comprehension and the KB
#       ingest all still happen (requirement 4, the containment rule);
#   (4) the reader refuses: no pointer (legacy row), hidden from AI, a stale
#       content_version, a missing blob, a corrupt blob;
#   (5) the book agent REUSES stored text and never touches the chain, and
#       falls back to the chain for a legacy row and for an edited file;
#   (6) deleting a file deletes its derived blob.
#
# Mutation-tested: see the docstring on each test for the mutation that must
# turn it red. Every one of them was applied and observed red before this file
# was committed.
"""T0: one extraction per file, reused by every consumer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.realtime.events import FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult
from pocketpaw_ee.cloud.uploads.extracted_text import (
    blob_key,
    delete_extracted_text,
    load_extracted_text,
    persist_extracted_text,
)

from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.local import LocalStorageAdapter

pytestmark = pytest.mark.asyncio


# --- doubles ---------------------------------------------------------------


class _FakeChain:
    """Extraction chain that records how many times it actually ran."""

    def __init__(self, result: ExtractionResult):
        self._result = result
        self.runs: list[tuple[Path, str]] = []

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        self.runs.append((path, mime))
        return self._result


class _ExplodingPut:
    """A storage adapter whose ``put`` always fails.

    Stands in for the PERMANENT storage failure — the case where persisting can
    never work. Everything downstream must still happen.
    """

    def __init__(self, real: LocalStorageAdapter):
        self._real = real

    def local_path(self, key: str) -> Path | None:
        return self._real.local_path(key)

    async def put(self, key, stream, mime):
        raise RuntimeError("storage is down")

    async def open(self, key):
        async for chunk in self._real.open(key):
            yield chunk

    async def delete(self, key) -> None:
        await self._real.delete(key)


class _FakeIngest:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, *, scope: str, text: str, source: str) -> dict:
        self.calls.append({"scope": scope, "text": text, "source": source})
        return {"article": "art-1"}


class _FakeAgents:
    def __init__(self) -> None:
        self.by_id: dict[str, SimpleNamespace] = {}
        self.by_slug: dict[tuple[str, str], SimpleNamespace] = {}
        self.creates: list[SimpleNamespace] = []

    async def create(self, ctx, workspace_id, body):
        agent = SimpleNamespace(id=f"agent-{len(self.creates) + 1}", name=body.name)
        self.by_id[agent.id] = agent
        self.by_slug[(workspace_id, body.slug)] = agent
        self.creates.append(agent)
        return agent

    async def get(self, agent_id):
        from pocketpaw_ee.cloud._core.errors import NotFound

        agent = self.by_id.get(agent_id)
        if agent is None:
            raise NotFound("agent", agent_id)
        return agent

    async def get_by_slug(self, workspace_id, slug):
        from pocketpaw_ee.cloud._core.errors import NotFound

        agent = self.by_slug.get((workspace_id, slug))
        if agent is None:
            raise NotFound("agent", slug)
        return agent


# --- helpers ---------------------------------------------------------------

_STORAGE_KEY = "chat/202608/book.pdf"


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": _STORAGE_KEY,
        "filename": "book.pdf",
        "mime": "application/pdf",
        "size": 1,
        "owner_id": "u1",
        "chat_id": None,
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


def _result(**overrides) -> ExtractionResult:
    defaults = {
        "text": "the whole book, every page of it",
        "title": "Thinking, Fast and Slow",
        "captions": ["a chart on page 40"],
        "metadata": {"page_count": 500},
        "backend": "local",
    }
    defaults.update(overrides)
    return ExtractionResult(**defaults)


@pytest.fixture()
def adapter(tmp_path) -> LocalStorageAdapter:
    """A REAL local storage adapter, with the uploaded blob already in it."""
    a = LocalStorageAdapter(tmp_path / "storage")
    target = (tmp_path / "storage" / _STORAGE_KEY).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"pretend this is a 500-page pdf")
    return a


def _event(**overrides) -> FileReady:
    data = {
        "workspace_id": "w1",
        "file_id": "f1",
        "filename": "book.pdf",
        "mime": "application/pdf",
        "storage_key": _STORAGE_KEY,
    }
    data.update(overrides)
    return FileReady(data=data)


def _wire_listener(monkeypatch, *, chain, adapter, ingest):
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads import listeners

    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: chain)
    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: adapter)
    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)
    # Comprehension is a separate track's model call; neutralise it so these
    # tests measure T0 and not FC-3's budget/proxy behaviour.
    monkeypatch.setattr(listeners, "_write_comprehension", _NoopComprehension())


class _NoopComprehension:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


# --- the round trip --------------------------------------------------------


async def test_persist_then_load_round_trips_the_whole_result(store, adapter):
    """The FULL ExtractionResult survives, not just ``text``.

    ``comprehend`` reads title + captions alongside text and the book agent
    names the agent from the title, so a store that kept only ``text`` would
    leave both of them re-extracting for the fields they were missing.

    Mutation: serialize only ``result.text`` — title/captions assertions red.
    """
    await store.save_scoped(_record(), "w1")

    ok = await persist_extracted_text(
        file_id="f1",
        workspace_id="w1",
        result=_result(),
        content_version=0,
        adapter=adapter,
    )
    assert ok is True

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.extracted_text_key == blob_key("f1")
    assert doc.extracted_text_version == 0

    loaded = await load_extracted_text(doc, adapter=adapter)
    assert loaded is not None
    assert loaded.text == "the whole book, every page of it"
    assert loaded.title == "Thinking, Fast and Slow"
    assert loaded.captions == ["a chart on page 40"]
    assert loaded.metadata == {"page_count": 500}
    assert loaded.backend == "local"


async def test_the_key_is_deterministic_so_a_reingest_overwrites(store, adapter):
    """Two persists for one file leave ONE blob, not two.

    A random key per pass would leak a dead object per upload event.

    Mutation: build the key with ``uuid4().hex`` — the second assertion (one
    file on disk) goes red.
    """
    await store.save_scoped(_record(), "w1")

    await persist_extracted_text(
        file_id="f1",
        workspace_id="w1",
        result=_result(text="first"),
        content_version=0,
        adapter=adapter,
    )
    await persist_extracted_text(
        file_id="f1",
        workspace_id="w1",
        result=_result(text="second"),
        content_version=0,
        adapter=adapter,
    )

    derived_dir = Path(adapter.local_path(blob_key("f1"))).parent
    assert len(list(derived_dir.iterdir())) == 1

    doc = await store.get_doc_scoped("f1", "w1")
    loaded = await load_extracted_text(doc, adapter=adapter)
    assert loaded.text == "second"


async def test_blob_key_refuses_a_traversing_file_id():
    """A key is a path on the local adapter; an id that escapes is refused."""
    assert blob_key("abc") == "derived/extraction/abc.json"
    for bad in ("../../etc/passwd", "a/b", "a\\b", "..", "", "  "):
        with pytest.raises(ValueError):
            blob_key(bad)


# --- the seam: does the listener ACTUALLY persist? -------------------------


async def test_listener_persists_the_extraction(monkeypatch, store, adapter):
    """THE seam assert. A real ingest writes the blob and both columns.

    T0's failure mode is invisible: if this never ran, every feature would
    still work and simply re-extract forever. Nothing else in the system
    notices, so this test is the only thing standing between "persisted" and
    "silently switched off".

    Mutation: delete the ``persist_extracted_text`` call from
    ``index_uploaded_file`` — red on the first assertion.
    """
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), "w1")
    chain = _FakeChain(_result())
    ingest = _FakeIngest()
    _wire_listener(monkeypatch, chain=chain, adapter=adapter, ingest=ingest)

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.extracted_text_key == blob_key("f1"), "the pointer column was never written"
    assert doc.extracted_text_version == 0

    # The blob is really on disk, and really holds the extraction.
    stored_path = adapter.local_path(blob_key("f1"))
    assert stored_path is not None, "no blob at the key the row points to"
    assert json.loads(stored_path.read_text())["title"] == "Thinking, Fast and Slow"

    # And a consumer can read it back without the chain.
    loaded = await load_extracted_text(doc, adapter=adapter)
    assert loaded.text == "the whole book, every page of it"


async def test_listener_records_the_version_the_row_was_at(monkeypatch, store, adapter):
    """An already-edited file stamps ITS version, not a hardcoded zero.

    Mutation: pass ``content_version=0`` in the listener — red.
    """
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), "w1")
    doc = await store.get_doc_scoped("f1", "w1")
    doc.content_version = 7
    await doc.save()

    _wire_listener(monkeypatch, chain=_FakeChain(_result()), adapter=adapter, ingest=_FakeIngest())
    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.extracted_text_version == 7
    # And the reader accepts it, because it matches the row.
    assert await load_extracted_text(doc, adapter=adapter) is not None


# --- containment: a persist failure must lose nothing ----------------------


async def test_persist_failure_loses_neither_the_ingest_nor_the_tags(monkeypatch, store, adapter):
    """Storage is permanently down; the upload is unharmed.

    This is requirement 4. Persisting runs BEFORE the auto-tag write and the
    KB ingest, so a persist that raised instead of failing open would take
    both of them with it — and the user asked to store a file, not to have it
    cached.

    Mutation: remove the ``try/except`` around ``adapter.put`` in
    ``persist_extracted_text`` (let it raise) — the ingest and tag assertions
    both go red.
    """
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), "w1")
    ingest = _FakeIngest()
    broken = _ExplodingPut(adapter)
    _wire_listener(
        monkeypatch,
        chain=_FakeChain(_result(text="invoice invoice payment total due balance")),
        adapter=broken,
        ingest=ingest,
    )

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    # The pointer is honestly absent — we did not claim a blob we never wrote.
    assert doc.extracted_text_key is None
    # Everything the user actually asked for still happened.
    assert len(ingest.calls) == 1, "a storage failure swallowed the KB ingest"
    assert doc.tags, "a storage failure swallowed the auto-tags"


async def test_persist_reports_failure_when_the_row_is_gone(store, adapter):
    """No row -> False, and no exception. The blob is inert and overwritten."""
    ok = await persist_extracted_text(
        file_id="ghost",
        workspace_id="w1",
        result=_result(),
        content_version=0,
        adapter=adapter,
    )
    assert ok is False


async def test_persist_reports_failure_with_no_adapter(store):
    """No storage at all -> False, not a crash.

    ``adapter=None`` falls through to ``_resolve_adapter``, which the autouse
    ``no_real_storage_adapter`` fixture pins to ``None`` — see the conftest
    header for why reaching the real singleton here is not merely untidy.
    """
    await store.save_scoped(_record(), "w1")
    ok = await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=None
    )
    assert ok is False

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.extracted_text_key is None, "a pointer was written for a blob that never landed"


# --- the reader's refusals -------------------------------------------------


async def test_reader_returns_none_for_a_legacy_row(store, adapter):
    """A row from before T0 has no pointer. ``None`` means "extract it".

    Mutation: return an empty ExtractionResult instead of None when the key is
    missing — the book-agent legacy-fallback test goes red.
    """
    await store.save_scoped(_record(), "w1")
    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.extracted_text_key is None
    assert await load_extracted_text(doc, adapter=adapter) is None


async def test_reader_refuses_a_file_hidden_from_ai(store, adapter):
    """The privacy gate lives on the READ door, not only at the call sites.

    This is a new door onto file content. The upload listener gates whether to
    PRODUCE text; this gates whether to SERVE it — and the next consumer will
    not remember to check.

    Mutation: delete the ``hide_from_ai`` check in ``load_extracted_text`` —
    red.
    """
    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=adapter
    )
    await store.set_library_metadata("f1", "w1", hide_from_ai=True)

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.extracted_text_key is not None, "fixture check: the pointer must exist"
    assert doc.hide_from_ai is True, "fixture check: the file must be hidden"
    assert await load_extracted_text(doc, adapter=adapter) is None


async def test_reader_refuses_text_extracted_before_an_edit(store, adapter):
    """An inline edit makes the stored text describe a document that is gone.

    ``cloud/file_versions`` rewrites the bytes and bumps ``content_version``
    WITHOUT emitting FileReady, so nothing re-extracts. Serving the old text
    would be a silent regression against today's behaviour, where the book
    agent always re-extracts fresh.

    Mutation: delete the version comparison in ``load_extracted_text`` — red.
    """
    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=adapter
    )

    doc = await store.get_doc_scoped("f1", "w1")
    assert await load_extracted_text(doc, adapter=adapter) is not None

    # Somebody edits the file. file_versions bumps the counter; no FileReady.
    doc.content_version = 1
    await doc.save()

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.extracted_text_key is not None, "fixture check: the pointer survives an edit"
    assert await load_extracted_text(doc, adapter=adapter) is None


async def test_reader_returns_none_when_the_blob_is_gone(store, adapter):
    """The pointer outlived its blob. Re-extract rather than raise."""
    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=adapter
    )
    await adapter.delete(blob_key("f1"))

    doc = await store.get_doc_scoped("f1", "w1")
    assert await load_extracted_text(doc, adapter=adapter) is None


async def test_reader_returns_none_on_a_corrupt_blob(store, adapter):
    """Garbage in the blob is a re-extraction, never an exception."""
    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=adapter
    )
    Path(adapter.local_path(blob_key("f1"))).write_text("{ not json at all")

    doc = await store.get_doc_scoped("f1", "w1")
    assert await load_extracted_text(doc, adapter=adapter) is None


# --- the payoff: the book agent stops re-extracting ------------------------


@pytest.fixture()
def book_wiring(monkeypatch, adapter):
    """Wire the book agent's seams, with a chain that must not be reached."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads import book_agent, extracted_text

    agents = _FakeAgents()
    ingest = _FakeIngest()
    chain = _FakeChain(_result(text="RE-EXTRACTED", title="From The Chain"))

    monkeypatch.setattr(
        book_agent,
        "_resolve_adapter",
        lambda: SimpleNamespace(
            local_path=lambda key: adapter.local_path(key),
        ),
    )
    monkeypatch.setattr(extracted_text, "_resolve_adapter", lambda: adapter)
    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: chain)
    monkeypatch.setattr("pocketpaw_ee.cloud.agents.service.create", agents.create)
    monkeypatch.setattr("pocketpaw_ee.cloud.agents.service.get", agents.get)
    monkeypatch.setattr("pocketpaw_ee.cloud.agents.service.get_by_slug", agents.get_by_slug)
    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)
    return SimpleNamespace(agents=agents, ingest=ingest, chain=chain)


async def test_book_agent_reuses_stored_text_and_never_runs_the_chain(
    beanie_upload_db, store, adapter, book_wiring
):
    """The whole point of T0. Stored text -> zero extraction chain runs.

    The chain double records its runs; asserting the COUNT is the only thing
    that can see this, because a re-extraction produces a working agent too —
    just seconds slower and a captioning bill heavier.

    Mutation: delete the ``load_extracted_text`` branch at the top of
    ``book_agent._extract_text`` — ``chain.runs`` becomes 1 and the ingested
    text becomes "RE-EXTRACTED". Both assertions red.
    """
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=adapter
    )

    result = await ensure_book_agent("f1", "w1", "u1")

    assert result.indexed is True
    assert book_wiring.chain.runs == [], "the book agent re-extracted despite stored text"
    assert book_wiring.ingest.calls[0]["text"] == "the whole book, every page of it"
    # The agent is named from the STORED title, so the title has to survive too.
    assert book_wiring.agents.creates[0].name == "Thinking, Fast and Slow"


async def test_book_agent_falls_back_to_the_chain_for_a_legacy_row(
    beanie_upload_db, store, book_wiring
):
    """No stored text (a file uploaded before T0) -> the old path, unchanged.

    Mutation: make ``load_extracted_text`` raise instead of returning None on a
    missing pointer — red, because the fallback never runs.
    """
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")

    result = await ensure_book_agent("f1", "w1", "u1")

    assert result.indexed is True
    assert len(book_wiring.chain.runs) == 1, "the legacy fallback did not run the chain"
    assert book_wiring.ingest.calls[0]["text"] == "RE-EXTRACTED"


async def test_book_agent_re_extracts_after_the_file_was_edited(
    beanie_upload_db, store, adapter, book_wiring
):
    """Stale stored text is refused, so the co-reader reads the CURRENT file.

    Without the version guard this test would hand the agent the pre-edit
    book — the regression T0 would otherwise introduce.

    Mutation: delete the version comparison in ``load_extracted_text`` — the
    ingested text becomes the stale copy and both assertions go red.
    """
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=adapter
    )
    doc = await store.get_doc_scoped("f1", "w1")
    doc.content_version = 2
    await doc.save()

    await ensure_book_agent("f1", "w1", "u1")

    assert len(book_wiring.chain.runs) == 1, "stale stored text was served as fresh"
    assert book_wiring.ingest.calls[0]["text"] == "RE-EXTRACTED"


async def test_book_agent_refuses_a_stored_extraction_with_no_text(
    beanie_upload_db, store, adapter, book_wiring
):
    """Stored-but-empty is refused on the same terms a fresh empty is.

    An image-only scan must not quietly produce a co-reader that has read
    nothing just because its emptiness happened to be cached.
    """
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1",
        workspace_id="w1",
        result=_result(text="   "),
        content_version=0,
        adapter=adapter,
    )

    with pytest.raises(ValidationError):
        await ensure_book_agent("f1", "w1", "u1")
    assert book_wiring.agents.creates == [], "an agent was made for an unreadable file"


# --- lifecycle -------------------------------------------------------------


async def test_deleting_the_blob_is_idempotent_and_silent(adapter):
    """Deleting twice, or deleting nothing, is not an error."""
    await delete_extracted_text("never-persisted", adapter=adapter)
    await delete_extracted_text("never-persisted", adapter=adapter)
    await delete_extracted_text("", adapter=adapter)


async def test_deleting_a_file_removes_its_derived_blob(store, adapter):
    """A deleted file leaves no extracted text at rest.

    Mutation: delete the ``delete_extracted_text`` call in
    ``EEUploadService.delete`` — red.
    """
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    from pocketpaw.uploads.config import UploadSettings

    await store.save_scoped(_record(), "w1")
    await persist_extracted_text(
        file_id="f1", workspace_id="w1", result=_result(), content_version=0, adapter=adapter
    )
    assert adapter.local_path(blob_key("f1")) is not None

    svc = EEUploadService(adapter=adapter, meta=store, cfg=UploadSettings())
    await svc.delete("f1", "u1", "w1")

    assert adapter.local_path(blob_key("f1")) is None, "the derived blob outlived the file"
