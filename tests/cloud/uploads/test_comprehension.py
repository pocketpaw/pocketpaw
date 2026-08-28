# tests/cloud/uploads/test_comprehension.py — FC-4. What file comprehension must
# refuse to do.
#
# Created 2026-08-28.
#
# The happy path here is cheap to get right and cheap to test: a model returns
# JSON, a summary lands on a row. Everything worth writing a test for is a
# REFUSAL — the cases where comprehension must not run, must not trust what it
# got back, and must not take the upload down with it. So the classes below are
# named for the four things that would actually hurt:
#
#   * a category the vocabulary has never heard of reaching a row, where it
#     renders as a shelf that looks real and matches one file forever;
#   * a 400-page PDF becoming a 400-page prompt;
#   * a hidden file being sent to a model at all;
#   * a failure — of the proxy, of the model, of the counter — turning "your
#     file is stored" into "your upload failed".
#
# Two conventions worth knowing before editing this file:
#
# The hide-from-AI test asserts with a client stub that RAISES the moment it is
# constructed, and then checks that it never was. Asserting on a boolean flag
# would pass just as happily if a future refactor stopped consulting the flag.
# The construction record is the evidence; the raise is there so the failure
# cannot be quiet if the record is ever dropped.
#
# The mutation plan for all of this is ``tests/mutations/file_comprehension.json``
# — every gate below has been observed to break when its guard is removed. A
# test nobody has watched fail is a claim, not a gate.

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pocketpaw_ee.cloud._core.realtime.events import FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult
from pocketpaw_ee.cloud.uploads import comprehension, comprehension_budget

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def beanie_with_budget():
    """Beanie bound to the upload docs AND the comprehension counter.

    The package conftest deliberately does not register
    ``FileComprehensionUsage`` — which means every OTHER listener test runs
    with an unreadable counter and therefore a fail-CLOSED budget, so no
    unrelated test ever reaches the network. Tests that need comprehension to
    actually run ask for this fixture instead.
    """
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.models.file_comprehension_usage import FileComprehensionUsage
    from pocketpaw_ee.cloud.uploads.models import FileFolder, FileUpload
    from pocketpaw_ee.cloud.uploads.share_models import ShareLink

    client = AsyncMongoMockClient()
    db = client[f"test_fc_{uuid.uuid4().hex[:8]}"]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    models = [FileUpload, FileFolder, ShareLink, FileComprehensionUsage]
    await init_beanie(database=db, document_models=models)
    try:
        yield db
    finally:
        # Same teardown reason as the package conftest: a leaked Beanie binding
        # keeps this torn-down mongomock db attached to the Document classes,
        # and the next test reads rows that no longer exist.
        for model in models:
            for attr in ("_document_settings", "_settings"):
                if hasattr(model, attr):
                    try:
                        setattr(model, attr, None)
                    except Exception:  # pragma: no cover — defensive
                        pass


@pytest.fixture()
async def budget_store(beanie_with_budget):
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    return MongoFileStore()


def _model_reply(summary: str, categories: list) -> dict:
    """The OpenAI-compatible envelope the proxy returns."""
    return {
        "choices": [
            {"message": {"content": json.dumps({"summary": summary, "categories": categories})}}
        ]
    }


class _Capture:
    """Records every proxy request and answers with a canned body."""

    def __init__(self, body: dict | None = None, status: int = 200):
        self.requests: list[httpx.Request] = []
        self._body = body if body is not None else _model_reply("A thing.", ["other"])
        self._status = status

    def transport(self) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self._status, json=self._body)

        return httpx.MockTransport(_handler)

    @property
    def sent_payload(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture()
def proxy(monkeypatch):
    """Point the module's transport seam at a capturing stub."""

    def _install(body: dict | None = None, status: int = 200) -> _Capture:
        cap = _Capture(body, status)
        monkeypatch.setattr(comprehension, "_PROXY_TRANSPORT", cap.transport())
        return cap

    return _install


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class TestTheVocabularyIsClosed:
    async def test_a_category_outside_the_list_is_dropped(self, proxy):
        """An unknown category is worse than none — it renders as a shelf that
        looks exactly like a real one and matches this file alone forever."""
        proxy(_model_reply("A board deck.", ["deck", "board-materials", "q3"]))

        got = await comprehension.comprehend("Deck", "revenue", [], mime="application/pdf")

        assert got is not None
        assert got.categories == ["deck"]

    async def test_nothing_is_mapped_to_the_nearest_legal_value(self, proxy):
        """Dropping is not the same as guessing. "board contract" must not
        become ``contract``: a wrong shelf is delivered with exactly the same
        confidence as a right one."""
        proxy(_model_reply("A deck.", ["board contract", "invoicing"]))

        got = await comprehension.comprehend("Deck", "revenue", [], mime="application/pdf")

        assert got is not None
        assert got.categories == []

    async def test_more_than_three_categories_are_capped(self, proxy):
        proxy(
            _model_reply(
                "Everything at once.",
                ["deck", "invoice", "spec", "research", "design", "data"],
            )
        )

        got = await comprehension.comprehend("X", "y", [], mime="application/pdf")

        assert got is not None
        assert got.categories == ["deck", "invoice", "spec"]
        assert len(got.categories) <= comprehension.MAX_CATEGORIES

    async def test_every_category_is_lowercase_and_unique(self):
        """The vocabulary is a wire format: ``validate_categories`` compares
        against it verbatim, so a stray capital would silently reject a value
        the prompt told the model to use."""
        assert list(comprehension.CATEGORIES) == [c.lower() for c in comprehension.CATEGORIES]
        assert len(set(comprehension.CATEGORIES)) == len(comprehension.CATEGORIES)


# ---------------------------------------------------------------------------
# The input cap
# ---------------------------------------------------------------------------


class TestTheInputIsCapped:
    async def test_oversized_text_is_truncated_before_the_call(self, proxy):
        """A 400-page PDF must not become a 400-page prompt. Asserted on the
        bytes that actually left, not on the truncation helper — the helper
        being correct while the payload is built from the original is exactly
        the bug this guards."""
        cap = proxy()
        huge = "lorem ipsum " * 20_000
        assert len(huge) > comprehension.MAX_INPUT_CHARS * 5

        await comprehension.comprehend("Big", huge, [], mime="application/pdf")

        sent = cap.sent_payload["messages"][-1]["content"]
        assert len(sent) < comprehension.MAX_INPUT_CHARS + 2000
        assert huge not in sent

    async def test_short_text_is_sent_whole(self, proxy):
        cap = proxy()

        await comprehension.comprehend("Small", "a short memo about parking", [], mime="text/plain")

        assert "a short memo about parking" in cap.sent_payload["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# Who pays
# ---------------------------------------------------------------------------


class TestThePlatformPaysNotTheUser:
    async def test_the_request_never_carries_an_x_api_key(self, proxy, monkeypatch):
        """The gateway forwards ``x-api-key`` upstream (BYOK header-forwarding),
        so one on this request would bill a user's own Anthropic account to
        summarise their own upload. This is a platform call; it carries the
        proxy Bearer and nothing else."""
        monkeypatch.setattr(comprehension, "_proxy_key", lambda: "sk-platform")
        cap = proxy()

        await comprehension.comprehend("X", "y", [], mime="text/plain")

        headers = cap.requests[-1].headers
        assert headers["authorization"] == "Bearer sk-platform"
        assert "x-api-key" not in headers


# ---------------------------------------------------------------------------
# Failure is never the upload's problem
# ---------------------------------------------------------------------------


class TestFailureReturnsNone:
    async def test_a_non_2xx_is_none(self, proxy):
        proxy({"error": "model not found"}, status=404)
        assert await comprehension.comprehend("X", "y", [], mime="text/plain") is None

    async def test_prose_instead_of_json_is_none(self, proxy):
        proxy({"choices": [{"message": {"content": "Sure! It looks like a deck."}}]})
        assert await comprehension.comprehend("X", "y", [], mime="text/plain") is None

    async def test_a_missing_summary_is_none_even_with_good_categories(self, proxy):
        """The summary is the part a person reads; categories alone are not
        worth a write."""
        proxy(_model_reply("   ", ["deck"]))
        assert await comprehension.comprehend("X", "y", [], mime="text/plain") is None

    async def test_a_fenced_reply_still_parses(self, proxy):
        """Models fence JSON even when told not to. Failing a whole
        comprehension over two backticks would be a self-inflicted wound."""
        fenced = '```json\n{"summary": "A spec.", "categories": ["spec"]}\n```'
        proxy({"choices": [{"message": {"content": fenced}}]})

        got = await comprehension.comprehend("X", "y", [], mime="text/plain")

        assert got is not None
        assert got.summary == "A spec."

    async def test_no_signal_at_all_never_calls_the_model(self, proxy):
        """Nothing but a mime type is nothing to classify. The model would
        confidently invent something; skip the spend instead."""
        cap = proxy()
        assert await comprehension.comprehend(None, "   ", [], mime="text/plain") is None
        assert cap.requests == []


# ---------------------------------------------------------------------------
# The daily cap
# ---------------------------------------------------------------------------


class TestTheDailyCap:
    async def test_the_cap_refuses_the_next_claim(self, beanie_with_budget, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "2")

        first = await comprehension_budget.try_spend("w1")
        second = await comprehension_budget.try_spend("w1")
        third = await comprehension_budget.try_spend("w1")

        assert first[0] is True
        assert second[0] is True
        assert third[0] is False, "the third claim on a cap of 2 must be refused"
        assert third[1:] == (2, 2)

    async def test_a_refused_claim_does_not_consume_a_slot(self, beanie_with_budget, monkeypatch):
        """An over-cap claim is rolled back, so the counter cannot run away to
        thousands and leave the workspace refused long after midnight."""
        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "1")

        await comprehension_budget.try_spend("w1")
        for _ in range(5):
            await comprehension_budget.try_spend("w1")

        from pocketpaw_ee.cloud.models.file_comprehension_usage import FileComprehensionUsage

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await FileComprehensionUsage.find_one(FileComprehensionUsage.key == f"w1:{day}")
        assert row is not None
        assert row.used == 1

    async def test_one_workspace_cannot_spend_anothers_budget(
        self, beanie_with_budget, monkeypatch
    ):
        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "1")

        await comprehension_budget.try_spend("w1")
        other = await comprehension_budget.try_spend("w2")

        assert other[0] is True

    async def test_an_unreadable_counter_fails_CLOSED(self, monkeypatch):
        """No Beanie binding in this test, so the collection genuinely cannot
        be read — the degraded-database case reached honestly rather than
        simulated. A degraded database must not become an open tab: skipping a
        summary costs a summary, an ungated ingest costs money."""
        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "5")

        allowed, _spent, _cap = await comprehension_budget.try_spend("w1")

        assert allowed is False

    async def test_no_workspace_is_refused(self, beanie_with_budget, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "5")
        assert (await comprehension_budget.try_spend(""))[0] is False
        assert (await comprehension_budget.try_spend(None))[0] is False

    async def test_a_zero_cap_disables_the_feature(self, beanie_with_budget, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "0")
        allowed, _spent, cap = await comprehension_budget.try_spend("w1")
        assert allowed is False
        assert cap == 0

    async def test_a_nonsense_cap_falls_back_to_the_default(self, monkeypatch):
        """ "five hundred" must not read as zero and switch comprehension off
        for a whole deployment."""
        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "five hundred")
        assert comprehension_budget.daily_cap() == 500


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


class _FakeChain:
    def __init__(self, result: ExtractionResult):
        self._result = result
        self.calls: list[tuple[Path, str]] = []

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        self.calls.append((path, mime))
        return self._result


class _FakeAdapter:
    def __init__(self, local_path_value: Path):
        self._local = local_path_value

    def local_path(self, key: str) -> Path | None:
        return self._local

    async def open(self, key: str):  # pragma: no cover — local path wins
        yield b""


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": "chat/202608/aaa.pdf",
        "filename": "deck.pdf",
        "mime": "application/pdf",
        "size": 1,
        "owner_id": "u1",
        "chat_id": "c1",
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


def _event() -> FileReady:
    return FileReady(
        data={
            "workspace_id": "w1",
            "file_id": "f1",
            "filename": "deck.pdf",
            "mime": "application/pdf",
            "storage_key": "chat/202608/aaa.pdf",
        }
    )


def _wire(monkeypatch, *, chain: _FakeChain, adapter: _FakeAdapter, ingest: AsyncMock):
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads import listeners

    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: chain)
    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: adapter)
    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)


class _ExplodingClient:
    """A stand-in for ``httpx.AsyncClient`` that cannot be built quietly.

    Records the attempt AND raises. The record is what the assertion reads;
    the raise is there so that if a future refactor drops the record, the
    failure still cannot pass silently.
    """

    attempts: list[str] = []

    def __init__(self, *args, **kwargs):
        _ExplodingClient.attempts.append("constructed")
        raise AssertionError("a model client was constructed for a hidden file")


class TestTheListener:
    async def test_a_deck_gets_a_summary_and_a_shelf(
        self, budget_store, monkeypatch, proxy, tmp_path
    ):
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "10")
        proxy(_model_reply("A board deck reviewing Q3 revenue.", ["deck"]))
        await budget_store.save_scoped(_record(), workspace="w1")

        path = tmp_path / "deck.pdf"
        path.write_bytes(b"unused; chain mocked")
        chain = _FakeChain(
            ExtractionResult(title="Q3 Board Deck", text="revenue revenue", backend="local")
        )
        _wire(
            monkeypatch,
            chain=chain,
            adapter=_FakeAdapter(path),
            ingest=AsyncMock(return_value={"article": "a1"}),
        )

        await index_uploaded_file(_event())

        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None
        assert doc.summary == "A board deck reviewing Q3 revenue."
        assert doc.collections == ["deck"]
        # The FL-6 tag path is untouched by any of this.
        assert doc.tags

    async def test_hide_from_ai_means_ZERO_model_calls(self, budget_store, monkeypatch, tmp_path):
        """A hidden file must not reach a model, and the proof is that no
        client was ever built — not that a flag was consulted."""
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "10")
        _ExplodingClient.attempts = []
        monkeypatch.setattr(comprehension.httpx, "AsyncClient", _ExplodingClient)

        await budget_store.save_scoped(_record(), workspace="w1")
        await budget_store.set_library_metadata("f1", "w1", hide_from_ai=True)

        path = tmp_path / "secret.pdf"
        path.write_bytes(b"unused")
        chain = _FakeChain(ExtractionResult(text="confidential", backend="local"))
        ingest = AsyncMock()
        _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(path), ingest=ingest)

        await index_uploaded_file(_event())

        assert _ExplodingClient.attempts == [], "a hidden file reached the model"
        assert chain.calls == [], "a hidden file was extracted"
        ingest.assert_not_awaited()
        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None
        assert doc.summary is None
        assert doc.collections == []

    async def test_a_raising_comprehension_does_not_take_the_upload_down(
        self, budget_store, monkeypatch, tmp_path
    ):
        """The listener's own containment, tested directly.

        ``comprehend`` swallows its own failures, so a proxy error alone can
        never reach the listener — which means the listener's try/except is
        untested by every other case here and could be narrowed or deleted
        without anything noticing. This drives a raise straight at it. The
        upload must complete: KB ingest awaited, tags written, no exception
        out of ``index_uploaded_file``.
        """
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "10")

        async def _boom(*_a, **_kw):
            raise RuntimeError("the comprehension path blew up")

        # ``_write_comprehension`` imports ``comprehend`` inside the function,
        # so the attribute is resolved at call time and this patch lands.
        monkeypatch.setattr(comprehension, "comprehend", _boom)

        await budget_store.save_scoped(_record(), workspace="w1")
        path = tmp_path / "deck.pdf"
        path.write_bytes(b"unused")
        chain = _FakeChain(
            ExtractionResult(title="Invoice", text="payment payment due", backend="local")
        )
        ingest = AsyncMock(return_value={"article": "a1"})
        _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(path), ingest=ingest)

        await index_uploaded_file(_event())

        ingest.assert_awaited_once()
        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None
        assert doc.summary is None
        assert doc.tags, "the tag path must survive a raising comprehension"

    async def test_a_human_set_summary_survives_reingest(
        self, budget_store, monkeypatch, proxy, tmp_path
    ):
        """Somebody read the file and typed a correction. A guess made from the
        first few thousand characters does not get to overwrite it."""
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "10")
        cap = proxy(_model_reply("A machine guess.", ["invoice"]))

        await budget_store.save_scoped(_record(), workspace="w1")
        await budget_store.set_library_metadata(
            "f1", "w1", summary="The 2027 board pack. Ignore the cover date."
        )

        path = tmp_path / "deck.pdf"
        path.write_bytes(b"unused")
        chain = _FakeChain(ExtractionResult(title="Deck", text="revenue", backend="local"))
        _wire(
            monkeypatch,
            chain=chain,
            adapter=_FakeAdapter(path),
            ingest=AsyncMock(return_value={"article": "a1"}),
        )

        await index_uploaded_file(_event())

        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None
        assert doc.summary == "The 2027 board pack. Ignore the cover date."
        assert cap.requests == [], "the model was called for a file already summarised"

    async def test_a_model_failure_leaves_the_file_indexed_and_tagged(
        self, budget_store, monkeypatch, proxy, tmp_path
    ):
        """Fail OPEN. Somebody asked us to store a file, not to describe it."""
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "10")
        proxy({"error": "no such model"}, status=404)
        await budget_store.save_scoped(_record(), workspace="w1")

        path = tmp_path / "deck.pdf"
        path.write_bytes(b"unused")
        chain = _FakeChain(
            ExtractionResult(title="Invoice", text="payment payment due", backend="local")
        )
        ingest = AsyncMock(return_value={"article": "a1"})
        _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(path), ingest=ingest)

        await index_uploaded_file(_event())

        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None
        assert doc.summary is None
        assert doc.tags, "the tag path must survive a comprehension failure"
        ingest.assert_awaited_once()

    async def test_an_exhausted_cap_leaves_the_file_indexed_and_tagged(
        self, budget_store, monkeypatch, proxy, tmp_path
    ):
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "0")
        cap = proxy(_model_reply("Never sent.", ["deck"]))
        await budget_store.save_scoped(_record(), workspace="w1")

        path = tmp_path / "deck.pdf"
        path.write_bytes(b"unused")
        chain = _FakeChain(ExtractionResult(title="Deck", text="revenue", backend="local"))
        ingest = AsyncMock(return_value={"article": "a1"})
        _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(path), ingest=ingest)

        await index_uploaded_file(_event())

        assert cap.requests == [], "a capped workspace still called the model"
        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None
        assert doc.summary is None
        assert doc.tags
        ingest.assert_awaited_once()

    async def test_an_existing_shelf_is_not_removed_by_comprehension(
        self, budget_store, monkeypatch, proxy, tmp_path
    ):
        """A shelf a person put this file on is not the model's to take away."""
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        monkeypatch.setenv("POCKETPAW_FILE_COMPREHENSION_DAILY", "10")
        proxy(_model_reply("A deck.", ["deck"]))

        await budget_store.save_scoped(_record(), workspace="w1")
        await budget_store.set_library_metadata("f1", "w1", collections=["research"])

        path = tmp_path / "deck.pdf"
        path.write_bytes(b"unused")
        chain = _FakeChain(ExtractionResult(title="Deck", text="revenue", backend="local"))
        _wire(
            monkeypatch,
            chain=chain,
            adapter=_FakeAdapter(path),
            ingest=AsyncMock(return_value={"article": "a1"}),
        )

        await index_uploaded_file(_event())

        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None
        assert doc.collections == ["research", "deck"]
