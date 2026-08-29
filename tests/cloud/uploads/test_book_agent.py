# test_book_agent.py — BA-4. Tests for "Make an agent of this book".
# Created: 2026-08-29 (BA-4). Covers the provisioning slice end to end against
# real (mongomock) FileUpload rows, with the agents service, the extraction
# chain and kb-go faked:
#   (1) pressing twice returns the SAME agent, and does no work the second
#       time — one create, one extraction, one ingest across both presses.
#       The call COUNTS are the point: the deterministic slug means a missing
#       idempotency check would still yield one agent, so counting creates
#       alone cannot see the bug. Re-extracting a 400-page PDF on every press
#       is the bug.
#   (2) a stale bind (agent deleted out from under the file) re-provisions
#       rather than failing forever.
#   (3) another workspace's file is NotFound — the tenant filter is the read.
#   (4) an ingest failure keeps the agent, reports indexed=False, and leaves
#       the file unbound so the next press retries against that same agent.
#   (5) the agent is named after the BOOK — extraction's title first, a
#       cleaned-up filename otherwise.
#   (6) the book lands in exactly ``agent:{id}`` — the scope the chat path
#       already searches. Any other scope means the agent never read it.
#   (7) a file hidden from AI is refused (FL-11b's gate, same answer here).
#   (8) a file with no readable text makes no agent at all.
#
# Mutations that must break these tests live in tests/mutations/book_agent.json.
"""Provisioning a dedicated co-reader agent from an uploaded book."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

from pocketpaw.uploads.file_store import FileRecord

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


class _FakeAdapter:
    def __init__(self, local_path_value: Path):
        self._local = local_path_value

    def local_path(self, key: str) -> Path | None:
        return self._local

    async def open(self, key: str):  # pragma: no cover — local path wins
        yield b""


class _FakeAgents:
    """In-memory stand-in for the agents service.

    Only the three functions the provisioner calls: ``create``, ``get`` and
    ``get_by_slug``. ``creates`` is the list the duplicate tests assert on.
    """

    def __init__(self) -> None:
        self.by_id: dict[str, SimpleNamespace] = {}
        self.by_slug: dict[tuple[str, str], SimpleNamespace] = {}
        self.creates: list[SimpleNamespace] = []
        self._seq = 0

    async def create(self, ctx, workspace_id, body):
        self._seq += 1
        agent = SimpleNamespace(
            id=f"agent-{self._seq}",
            name=body.name,
            slug=body.slug,
            persona=body.persona,
            visibility=body.visibility,
            workspace=workspace_id,
            owner=ctx.user_id,
        )
        self.by_id[agent.id] = agent
        self.by_slug[(workspace_id, body.slug)] = agent
        self.creates.append(agent)
        return agent

    async def get(self, agent_id):
        agent = self.by_id.get(agent_id)
        if agent is None:
            raise NotFound("agent", agent_id)
        return agent

    async def get_by_slug(self, workspace_id, slug):
        agent = self.by_slug.get((workspace_id, slug))
        if agent is None:
            raise NotFound("agent", slug)
        return agent


class _FakeIngest:
    """kb-go ingest double. Records every call; can be made to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def __call__(self, *, scope: str, text: str, source: str) -> dict:
        self.calls.append({"scope": scope, "text": text, "source": source})
        if self.fail:
            raise RuntimeError("kb ingest exploded")
        return {"article": "art-1"}


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": "chat/202608/book.pdf",
        "filename": "book.pdf",
        "mime": "application/pdf",
        "size": 1,
        "owner_id": "u1",
        "chat_id": None,
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


@pytest.fixture()
def wiring(monkeypatch, tmp_path):
    """Patch the three seams ``ensure_book_agent`` reaches out through.

    Patched at their SOURCE modules, because the provisioner imports them
    lazily inside the call — patching a name it never binds would silently
    do nothing and the tests would exercise the real services.
    """
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads import book_agent

    blob = tmp_path / "book.pdf"
    blob.write_bytes(b"unused; the chain is faked")

    chain = _FakeChain(ExtractionResult(text="the whole book", title=None, backend="local"))
    agents = _FakeAgents()
    ingest = _FakeIngest()

    monkeypatch.setattr(book_agent, "_resolve_adapter", lambda: _FakeAdapter(blob))
    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: chain)
    monkeypatch.setattr("pocketpaw_ee.cloud.agents.service.create", agents.create)
    monkeypatch.setattr("pocketpaw_ee.cloud.agents.service.get", agents.get)
    monkeypatch.setattr("pocketpaw_ee.cloud.agents.service.get_by_slug", agents.get_by_slug)
    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)

    return SimpleNamespace(chain=chain, agents=agents, ingest=ingest, blob=blob)


# --- idempotency -----------------------------------------------------------


async def test_pressing_twice_returns_the_same_agent_and_does_no_work_twice(
    beanie_upload_db, store, wiring
):
    """Two presses, one agent — and the second press re-reads nothing.

    Mutation: delete the ``file.agent_id`` early return in
    ``ensure_book_agent``. The create COUNT stays 1 (the deterministic slug
    adopts the existing agent), so only the extraction / ingest counts catch
    it — which is exactly the cost the early return exists to avoid.
    """
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")

    first = await ensure_book_agent("f1", "w1", "u1")
    second = await ensure_book_agent("f1", "w1", "u1")

    assert first.agent_id == second.agent_id
    assert first.created is True
    assert second.created is False
    assert second.indexed is True

    assert len(wiring.agents.creates) == 1
    assert len(wiring.chain.runs) == 1
    assert len(wiring.ingest.calls) == 1


async def test_the_bind_is_persisted_on_the_file(beanie_upload_db, store, wiring):
    """The file remembers its agent — that bind IS the idempotency key."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    result = await ensure_book_agent("f1", "w1", "u1")

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.agent_id == result.agent_id


async def test_a_stale_bind_reprovisions(beanie_upload_db, store, wiring):
    """A bind pointing at a DELETED agent is not a permanent failure."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    await store.set_book_agent("f1", "w1", agent_id="agent-that-was-deleted")

    result = await ensure_book_agent("f1", "w1", "u1")

    assert result.created is True
    assert result.indexed is True
    assert result.agent_id != "agent-that-was-deleted"
    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.agent_id == result.agent_id


async def test_a_stale_bind_is_cleared_even_if_the_reprovision_fails(
    beanie_upload_db, store, wiring
):
    """A bind must never outlive the agent it names.

    Stale bind + a re-provision that can't finish its ingest: the row must not
    keep pointing at the deleted agent, or the library offers "open the agent"
    on a dead id.
    """
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    wiring.ingest.fail = True
    await store.save_scoped(_record(), "w1")
    await store.set_book_agent("f1", "w1", agent_id="agent-that-was-deleted")

    result = await ensure_book_agent("f1", "w1", "u1")

    assert result.indexed is False
    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.agent_id is None


# --- tenancy ---------------------------------------------------------------


async def test_another_workspaces_file_is_not_found(beanie_upload_db, store, wiring):
    """Cross-tenant press → NotFound, and nothing is created."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")

    with pytest.raises(NotFound):
        await ensure_book_agent("f1", "w2", "u2")

    assert wiring.agents.creates == []
    assert wiring.ingest.calls == []


async def test_a_file_hidden_from_ai_is_refused(beanie_upload_db, store, wiring):
    """FL-11b's privacy gate holds on this door too (no KB end-run)."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    await store.set_library_metadata("f1", "w1", hide_from_ai=True)

    with pytest.raises(Forbidden):
        await ensure_book_agent("f1", "w1", "u1")

    assert wiring.agents.creates == []
    assert wiring.ingest.calls == []


# --- the ingest ------------------------------------------------------------


async def test_the_book_lands_in_the_agents_own_scope(beanie_upload_db, store, wiring):
    """Scope must be exactly ``agent:{id}`` — what the chat path searches."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    result = await ensure_book_agent("f1", "w1", "u1")

    assert wiring.ingest.calls == [
        {
            "scope": f"agent:{result.agent_id}",
            "text": "the whole book",
            "source": "book.pdf",
        }
    ]


async def test_a_failed_ingest_keeps_the_agent_and_says_it_is_unread(
    beanie_upload_db, store, wiring
):
    """The agent survives a KB failure; the file stays unbound on purpose."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    wiring.ingest.fail = True
    await store.save_scoped(_record(), "w1")

    result = await ensure_book_agent("f1", "w1", "u1")

    assert result.created is True
    assert result.indexed is False
    # The agent the user can see is still there — never deleted to tidy up.
    assert await wiring.agents.get(result.agent_id) is not None
    # ...and the file is NOT bound, so the next press retries the ingest.
    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.agent_id is None


async def test_a_retry_after_a_failed_ingest_adopts_the_same_agent(beanie_upload_db, store, wiring):
    """The unbound retry must not mint a second agent for the same book."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    wiring.ingest.fail = True
    await store.save_scoped(_record(), "w1")
    first = await ensure_book_agent("f1", "w1", "u1")

    wiring.ingest.fail = False
    second = await ensure_book_agent("f1", "w1", "u1")

    assert second.agent_id == first.agent_id
    assert second.created is False
    assert second.indexed is True
    assert len(wiring.agents.creates) == 1


async def test_no_readable_text_makes_no_agent(beanie_upload_db, store, wiring, monkeypatch):
    """An image-only scan is refused BEFORE anything is created."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    empty = _FakeChain(ExtractionResult(text="   ", title="Scanned", backend="local"))
    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: empty)
    await store.save_scoped(_record(), "w1")

    with pytest.raises(ValidationError):
        await ensure_book_agent("f1", "w1", "u1")

    assert wiring.agents.creates == []


# --- the name --------------------------------------------------------------


async def test_the_agent_is_named_after_the_book_when_extraction_finds_a_title(
    beanie_upload_db, store, wiring, monkeypatch
):
    """A detected title beats the filename — the user thinks in book names."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    titled = _FakeChain(
        ExtractionResult(text="call me ishmael", title="Moby-Dick", backend="local")
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: titled)
    await store.save_scoped(_record(filename="mobydick_ocr_FINAL_v2.pdf"), "w1")

    await ensure_book_agent("f1", "w1", "u1")

    assert wiring.agents.creates[0].name == "Moby-Dick"


async def test_the_agent_falls_back_to_a_cleaned_up_filename(beanie_upload_db, store, wiring):
    """No title → the filename, minus the extension and the separators."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(filename="thinking-fast_and.slow.pdf"), "w1")

    await ensure_book_agent("f1", "w1", "u1")

    assert wiring.agents.creates[0].name == "thinking fast and slow"


async def test_the_agent_is_a_co_reader_not_a_generic_assistant(beanie_upload_db, store, wiring):
    """The persona names the book and the co-reading job — that IS the feature."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(filename="Dune.pdf"), "w1")

    await ensure_book_agent("f1", "w1", "u1")

    persona = wiring.agents.creates[0].persona
    assert "co-reader" in persona.lower()
    assert "Dune" in persona


async def test_the_agent_is_made_in_the_files_own_workspace(beanie_upload_db, store, wiring):
    """Never cross-tenant: the agent lands in the file's workspace."""
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent

    await store.save_scoped(_record(), "w1")
    await ensure_book_agent("f1", "w1", "u1")

    assert wiring.agents.creates[0].workspace == "w1"
    assert wiring.agents.creates[0].slug == "book-f1"


async def test_the_agent_follows_the_file_even_if_the_read_stops_filtering(
    beanie_upload_db, store, wiring, monkeypatch
):
    """Defense in depth: the FILE's workspace decides, not the caller's claim.

    Today the two are equal by construction — ``get_doc_scoped`` filters on
    exactly that — so this forces the case the tenant filter normally makes
    impossible: a read that hands back a foreign row. The book's contents must
    still land in the workspace that owns the book, never in the caller's.
    """
    from pocketpaw_ee.cloud.uploads.book_agent import ensure_book_agent
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    await store.save_scoped(_record(), "w1")
    leaked = await store.get_doc_scoped("f1", "w1")

    async def _unfiltered(self, file_id, workspace):
        return leaked

    monkeypatch.setattr(MongoFileStore, "get_doc_scoped", _unfiltered)

    await ensure_book_agent("f1", "w2", "u2")

    assert wiring.agents.creates[0].workspace == "w1"
