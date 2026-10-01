# test_knowledge_sectioned_ingest.py — section-wise knowledge ingest.
#
# kb-go searches only compiled articles, so a fact the compile drops cannot be
# found. ``KnowledgeService.ingest_document_to_scope`` splits a long document
# into sections (``split_into_sections``) and compiles each one into its own
# article with a restructure-not-compress prompt. These tests pin:
#   * the splitter: headings, tables, oversized paragraphs, page breaks, and a
#     short document staying one section;
#   * the real demo PDF (tests/fixtures/knowledge/cairn-field-guide.pdf, a
#     fictional store guide) keeps every probe fact in some ingested article,
#     and every article fits kb-go's 2,000-byte --context body;
#   * per-section validation: a near-verbatim section is accepted, a runaway
#     one is rejected and retried once with a JSON-only reminder;
#   * partial failure returns the landed ids with counts, total failure raises;
#   * at most three compiles run at once, kb writes never overlap, and the
#     deadline fails the sections it did not reach;
#   * the short-document path and the API-key path are unchanged.
# The LLM (``PocketPawCompilerBackend.complete``) and the kb binary (``_kb``)
# are faked at their boundaries; everything between runs for real.
"""Sectioned ingest: splitter, per-section compile, partial failure, bounds."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.agents import knowledge
from pocketpaw_ee.cloud.agents.knowledge import (
    KnowledgeEngineUnavailable,
    KnowledgeService,
    extract_ingest_article_ids,
)
from pocketpaw_ee.cloud.agents.knowledge_sections import Section, split_into_sections
from pocketpaw_ee.cloud.kb import backend_adapter

_PDF = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge" / "cairn-field-guide.pdf"
_PDF_PROBES = [
    "Juniper",
    "418 Alder Street",
    "$22 per jacket",
    "$12 per section",
    "Trail Crew",
    "$49",
    "35% off",
    "12 units",
    "40%",
]
_SECTION_RE = re.compile(r'"""\n(.*)\n"""', re.DOTALL)


# --------------------------------------------------------------------------- #
# Fakes at the two boundaries
# --------------------------------------------------------------------------- #


def _slug(title: str) -> str:
    """kb-go's slugify: the article id is the slug of the compiled title."""
    slug = re.sub(r"[\s-]+", "-", re.sub(r"[^a-z0-9\s-]", "", title.lower())).strip("-")
    return slug[:80]


class _FakeKb:
    """``knowledge._kb`` for ingest --article-json and delete, with an article
    store, a check that no two kb calls overlap, and optional failures."""

    def __init__(self) -> None:
        self.articles: dict[str, dict] = {}
        self.payloads: list[dict] = []
        self.deleted: list[str] = []
        self.fail_titles: dict[str, Exception] = {}
        self._busy = threading.Lock()
        self.overlapped = False

    def __call__(self, *args: str, input_text: str | None = None, timeout: int = 120):
        if not self._busy.acquire(blocking=False):
            self.overlapped = True
            self._busy.acquire()
        try:
            time.sleep(0.005)  # long enough for an overlap to show
            if args[:2] == ("ingest", "--article-json"):
                payload = json.loads(input_text or "{}")
                article = payload["article"]
                for needle, exc in self.fail_titles.items():
                    if needle in article["title"]:
                        raise exc
                self.payloads.append(payload)
                article_id = _slug(article["title"])
                self.articles[article_id] = payload
                return {
                    "article": article_id,
                    "title": article["title"],
                    "words": len(article["content"].split()),
                    "compiled_with": article["compiled_with"],
                }
            if args[0] == "delete":
                self.deleted.append(args[1])
                self.articles.pop(args[1], None)
                return "deleted"
            raise AssertionError(f"unexpected kb call {args}")
        finally:
            self._busy.release()


def _restructure(section: str) -> dict:
    """An honest section compile: every line kept, as markdown bullets."""
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    topic = lines[0].lstrip("#").strip()[:40]
    return {
        "title": topic,
        "summary": f"What the guide says about {topic}.",
        "content": f"## {topic}\n\n" + "\n".join(f"- {line}" for line in lines[1:] or lines),
        "concepts": ["care", "repairs"],
        "categories": ["store"],
    }


class _Compiler:
    """``PocketPawCompilerBackend.complete``: records prompts, tracks how many
    compiles are in flight, and answers through ``respond(section, prompt)``."""

    def __init__(self, respond=None, delay: float = 0.0) -> None:
        self.prompts: list[str] = []
        self.respond = respond or (lambda section, prompt: json.dumps(_restructure(section)))
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(self, prompt: str, system_prompt: str = "") -> str:
        self.prompts.append(prompt)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            match = _SECTION_RE.search(prompt)
            return self.respond(match.group(1) if match else "", prompt)
        finally:
            self.in_flight -= 1


@pytest.fixture
def kb(monkeypatch) -> _FakeKb:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeKb()
    monkeypatch.setattr(knowledge, "_kb", fake)
    return fake


def _install(monkeypatch, compiler: _Compiler) -> _Compiler:
    async def complete(self, prompt: str, system_prompt: str = "") -> str:
        return await compiler.complete(prompt, system_prompt)

    monkeypatch.setattr(backend_adapter.PocketPawCompilerBackend, "complete", complete)
    return compiler


def _long_doc(sections: int = 6, marker: str = "") -> str:
    """``sections`` headed chapters of ~1,200 chars each, every one with a
    unique price fact; ``marker`` goes into chapter 2 only."""
    chapters = []
    for i in range(1, sections + 1):
        body = " ".join(
            f"Item {i}-{j} costs ${i * 10 + j} and ships in {j} days." for j in range(1, 25)
        )
        extra = f" {marker}" if marker and i == 2 else ""
        chapters.append(f"# Chapter {i}\n\n{body}{extra}")
    return "\n\n".join(chapters)


# --------------------------------------------------------------------------- #
# split_into_sections
# --------------------------------------------------------------------------- #


def test_a_short_document_is_one_section():
    text = "# Hours\n\nOpen nine to five.\n\n## Holidays\n\nClosed on public holidays."
    assert split_into_sections(text) == [Section(title_hint="Hours", text=text)]
    assert split_into_sections("x" * 2400) == [Section(title_hint="", text="x" * 2400)]
    assert split_into_sections("  \n\n ") == []


def test_headings_start_sections_and_become_the_hint():
    doc = _long_doc(4)
    sections = split_into_sections(doc, target=1500, hard_max=2000)
    assert [s.title_hint for s in sections] == [f"Chapter {i}" for i in range(1, 5)]
    assert all(s.text.startswith(f"# {s.title_hint}") for s in sections)
    assert all(len(s.text) <= 1500 for s in sections)


def test_heading_like_lines_in_extracted_pdf_text_are_boundaries():
    """PDF text has no blank lines or markdown: a short, unpunctuated,
    capitalised line after a sentence end is a heading; a price row is not."""
    filler = "This sentence is filler about the shop and its long history. " * 14
    doc = (
        f"1. The Workshop\n{filler}\nRepair prices\nZipper slider replacement $18 Most jackets\n"
        f"{filler}\n2. Caring for your gear\n{filler}\n"
    )
    sections = split_into_sections(doc, target=1000, hard_max=1200)
    hints = [s.title_hint for s in sections]
    assert "1. The Workshop" in hints and "2. Caring for your gear" in hints
    assert "Zipper slider replacement $18 Most jackets" not in hints


def test_an_oversized_paragraph_splits_at_sentences_not_mid_sentence():
    sentences = [
        f"Sentence number {i} states that plan {i} costs ${i}.00 a month." for i in range(80)
    ]
    doc = " ".join(sentences)
    sections = split_into_sections(doc, target=600, hard_max=800)
    assert len(sections) > 1
    assert all(len(s.text) <= 600 for s in sections)
    for section in sections:
        assert section.text.endswith(".")
    rejoined = " ".join(s.text for s in sections)
    assert rejoined == doc


def test_multi_line_paragraphs_are_cut_at_line_ends():
    lines = [f"Line {i} of the policy, which wraps here and" for i in range(60)]
    lines = [f"{line} ends." if i % 3 == 2 else line for i, line in enumerate(lines)]
    doc = "\n".join(lines)
    sections = split_into_sections(doc, target=700, hard_max=900)
    assert len(sections) > 1
    kept = [line for s in sections for line in s.text.split("\n") if line]
    assert kept == lines  # every line whole, in order, none repeated
    assert all(s.title_hint == "" for s in sections)  # wrapped lines are not headings


def test_a_large_markdown_table_repeats_its_header_on_each_piece():
    header = "| Product | Price | Stock |\n| --- | --- | --- |"
    rows = [f"| Widget {i:03d} | ${i}.99 | {i * 3} units |" for i in range(120)]
    doc = "# Price list\n\n" + header + "\n" + "\n".join(rows)
    sections = split_into_sections(doc, target=900, hard_max=1200)
    assert len(sections) > 2
    assert all(len(s.text) <= 900 for s in sections)
    for section in sections:
        assert header in section.text
        assert section.title_hint == "Price list"
    body_rows = [
        line for s in sections for line in s.text.split("\n") if line.startswith("| Widget")
    ]
    assert body_rows == rows


def test_a_spaced_column_table_is_cut_only_at_row_ends():
    rows = [f"Model {i:03d}    {i * 2} kg    ${i * 15}" for i in range(150)]
    doc = "Spec sheet\n\n" + "\n".join(rows)
    sections = split_into_sections(doc, target=800, hard_max=1000)
    kept = [line for s in sections for line in s.text.split("\n") if line.startswith("Model")]
    assert kept == rows


def test_page_breaks_end_paragraphs():
    page = "Words on a page that end in a full stop. " * 30
    doc = "\f".join([page.strip()] * 4)
    sections = split_into_sections(doc, target=1300, hard_max=1500)
    assert all("\f" not in s.text for s in sections)
    assert sum(s.text.count("Words on a page") for s in sections) == 30 * 4
    assert all(len(s.text) <= 1300 for s in sections)


def test_bad_limits_are_refused():
    with pytest.raises(ValueError):
        split_into_sections("text", target=500, hard_max=400)


# --------------------------------------------------------------------------- #
# The real document
# --------------------------------------------------------------------------- #


async def test_every_fact_in_the_real_field_guide_lands_in_some_article(monkeypatch, kb):
    from pocketpaw_ee.paw_bar.knowledge_sources import PDF_MIME, extract_file_text

    pytest.importorskip("pypdf")
    text = await extract_file_text(_PDF.read_bytes(), ".pdf", PDF_MIME)
    for probe in _PDF_PROBES:
        assert probe in text, f"fixture lost {probe!r}"
    compiler = _install(monkeypatch, _Compiler())

    result = await KnowledgeService.ingest_document_to_scope(
        "pocket:p1", text, "cairn-field-guide.pdf"
    )

    assert result["sections_total"] > 1
    assert result["sections_failed"] == 0
    assert len(compiler.prompts) == result["sections_total"]
    assert sorted(result["articles"]) == sorted(kb.articles)
    assert extract_ingest_article_ids(result) == result["articles"]
    contents = [kb.articles[a]["article"]["content"] for a in result["articles"]]
    for probe in _PDF_PROBES:
        assert any(probe in c for c in contents), f"{probe!r} is in no article"
    # Each article's body fits kb-go's --context body limit and the
    # concierge's per-item slice.
    for article_id in result["articles"]:
        payload = kb.articles[article_id]
        assert len(payload["article"]["content"].encode()) < 2000
        # raw_text is the section itself: whole lines of the document.
        assert all(line in text for line in payload["raw_text"].split("\n"))
        assert payload["article"]["title"].startswith("cairn-field-guide.pdf — part ")
        assert payload["article"]["compiled_with"].startswith("pocketpaw-agent:")
    # The prompt restructures rather than compresses.
    assert "do not drop facts" in compiler.prompts[0]
    assert "COMPRESS" not in compiler.prompts[0]


# --------------------------------------------------------------------------- #
# Per-section validation and retry
# --------------------------------------------------------------------------- #


async def test_a_near_verbatim_section_is_accepted(monkeypatch, kb):
    def verbatim(section: str, prompt: str) -> str:
        return json.dumps({"title": "Prices", "summary": "s", "content": section})

    _install(monkeypatch, _Compiler(verbatim))
    doc = _long_doc(5)

    result = await KnowledgeService.ingest_document_to_scope("pocket:p1", doc, "prices.md")

    assert result["sections_failed"] == 0
    assert len(result["articles"]) == result["sections_total"] == 5
    assert all(p["article"]["content"] == p["raw_text"] for p in kb.payloads)


async def test_a_runaway_section_is_rejected_and_retried_once(monkeypatch, kb):
    def runaway_first(section: str, prompt: str) -> str:
        if "Your previous reply could not be used" in prompt:
            return json.dumps(_restructure(section))
        return json.dumps(dict(_restructure(section), content=section * 4))

    compiler = _install(monkeypatch, _Compiler(runaway_first))
    doc = _long_doc(3)

    result = await KnowledgeService.ingest_document_to_scope("pocket:p1", doc, "prices.md")

    assert result["sections_failed"] == 0 and len(result["articles"]) == 3
    assert len(compiler.prompts) == 6  # every section twice
    retries = [p for p in compiler.prompts if "Your previous reply could not be used" in p]
    assert len(retries) == 3


def test_section_validation_limits():
    section = "a" * 1000
    ok = {"title": "t", "content": "b" * 2900}
    assert knowledge._validate_section_article(ok, section_text=section, source="s")
    with pytest.raises(ValueError, match="runs away"):
        knowledge._validate_section_article(
            {"title": "t", "content": "b" * 3001}, section_text=section, source="s"
        )
    with pytest.raises(ValueError, match="runs away"):
        knowledge._validate_section_article(
            {"title": "t", "content": "b" * 8001}, section_text="a" * 5000, source="s"
        )
    with pytest.raises(ValueError, match="missing a title or content"):
        knowledge._validate_section_article(
            {"title": "", "content": "b"}, section_text=section, source="s"
        )


# --------------------------------------------------------------------------- #
# Partial and total failure
# --------------------------------------------------------------------------- #


async def test_a_failed_section_is_reported_and_the_rest_land(monkeypatch, kb, caplog):
    def fail_marked(section: str, prompt: str) -> str:
        if "BROKEN" in section:
            return "I cannot do that."
        return json.dumps(_restructure(section))

    _install(monkeypatch, _Compiler(fail_marked))
    doc = _long_doc(4, marker="BROKEN")

    with caplog.at_level("WARNING", logger="pocketpaw_ee.cloud.agents.knowledge"):
        result = await KnowledgeService.ingest_document_to_scope("pocket:p1", doc, "prices.md")

    assert result["sections_total"] == 4
    assert result["sections_failed"] == 1
    assert len(result["articles"]) == 3
    [failure] = result["failures"]
    assert failure["section"] == 2 and failure["heading"] == "Chapter 2"
    assert "not a JSON object" in failure["reason"]
    assert any("section 2 of 4" in r.getMessage() for r in caplog.records)


async def test_every_section_failing_raises(monkeypatch, kb):
    _install(monkeypatch, _Compiler(lambda section, prompt: "no"))

    with pytest.raises(RuntimeError, match="every section"):
        await KnowledgeService.ingest_document_to_scope("pocket:p1", _long_doc(3), "x.md")

    assert kb.payloads == []


async def test_an_outdated_kb_binary_stops_the_document(monkeypatch, kb):
    _install(monkeypatch, _Compiler())
    kb.fail_titles["part 1 of"] = KnowledgeEngineUnavailable("old binary")

    with pytest.raises(KnowledgeEngineUnavailable):
        await KnowledgeService.ingest_document_to_scope("pocket:p1", _long_doc(3), "x.md")


async def test_section_titles_are_unique_article_ids(monkeypatch, kb):
    """kb-go keys an article by its title's slug (80 chars). Sections that the
    compiler gives the same topic must still be separate articles."""
    same = json.dumps({"title": "Prices " * 20, "summary": "s", "content": "c"})
    _install(monkeypatch, _Compiler(lambda section, prompt: same))
    long_name = "a-very-long-supplier-price-list-file-name-for-2026-edition.pdf"

    result = await KnowledgeService.ingest_document_to_scope("pocket:p1", _long_doc(4), long_name)

    assert len(set(result["articles"])) == 4


# --------------------------------------------------------------------------- #
# Concurrency and the deadline
# --------------------------------------------------------------------------- #


async def test_at_most_three_compiles_run_at_once_and_kb_writes_never_overlap(monkeypatch, kb):
    compiler = _install(monkeypatch, _Compiler(delay=0.02))

    result = await KnowledgeService.ingest_document_to_scope("pocket:p1", _long_doc(8), "x.md")

    assert len(result["articles"]) == 8
    assert compiler.max_in_flight == knowledge._SECTION_CONCURRENCY == 3
    assert not kb.overlapped


async def test_sections_past_the_deadline_fail_and_the_rest_land(monkeypatch, kb):
    monkeypatch.setattr(knowledge, "_SECTIONED_INGEST_DEADLINE_S", 0.3)
    _install(monkeypatch, _Compiler(delay=0.2))
    started = time.monotonic()

    result = await KnowledgeService.ingest_document_to_scope("pocket:p1", _long_doc(9), "x.md")

    assert time.monotonic() - started < 1.5
    assert len(result["articles"]) == 3  # the first wave of three
    assert result["sections_failed"] == 6
    assert {f["reason"] for f in result["failures"]} == {"deadline"}


# --------------------------------------------------------------------------- #
# Unchanged paths
# --------------------------------------------------------------------------- #


async def test_a_short_document_keeps_the_whole_document_compile(monkeypatch, kb):
    article = {"title": "Hours", "summary": "s", "content": "# Hours\n\nNine to five."}
    compiler = _install(monkeypatch, _Compiler(lambda section, prompt: json.dumps(article)))
    note = "Shop hours: open nine to five on weekdays, closed on public holidays."

    result = await KnowledgeService.ingest_document_to_scope("pocket:p1", note, "hours.txt")

    [prompt] = compiler.prompts
    assert "COMPRESSES the document" in prompt
    assert result["articles"] == ["hours"]
    assert (result["sections_total"], result["sections_failed"]) == (1, 0)
    assert kb.payloads[0]["raw_text"] == note
    assert kb.payloads[0]["article"]["title"] == "Hours"


async def test_the_api_key_path_is_plain_kb_ingest_even_for_a_long_document(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    compiler = _install(monkeypatch, _Compiler())
    calls: list[tuple] = []

    def fake_kb(*args, input_text=None, timeout=120):
        calls.append((args, input_text))
        return {"article": "whole-doc", "compiled_with": "claude"}

    monkeypatch.setattr(knowledge, "_kb", fake_kb)
    doc = _long_doc(6)

    result = await KnowledgeService.ingest_document_to_scope("pocket:p1", doc, "prices.md")

    assert compiler.prompts == []
    assert calls == [(("ingest", "--scope", "pocket:p1", "--source", "prices.md"), doc)]
    assert result["articles"] == ["whole-doc"]


# --------------------------------------------------------------------------- #
# Concierge retrieval needs no change
# --------------------------------------------------------------------------- #


async def test_several_sections_of_one_document_reach_a_concierge_turn_whole(monkeypatch):
    """Sections are sized so a compiled one fits the concierge's per-item slice:
    three sections of one document come back as three items, each with its
    whole body (the fact at the END of each body survives), and all of them fit
    the knowledge budget."""
    from types import SimpleNamespace

    from pocketpaw_ee.paw_bar import concierge_runtime

    titles = [f"cairn-field-guide.pdf — part {i} of 7: Topic {i}" for i in (1, 2, 3)]
    bodies = [
        "\n".join(f"- Line {j} of section {i}." for j in range(80)) + f"\n- Tail fact {i}."
        for i in (1, 2, 3)
    ]
    assert all(len(f"## {t}\n{b}") <= concierge_runtime._ITEM_CHARS for t, b in zip(titles, bodies))
    hits = [{"id": _slug(t), "title": t, "summary": "s"} for t in titles]
    context = "\n\n---\n\n".join(f"## {t}\n{b}" for t, b in zip(titles, bodies))

    async def articles(scope, query, limit=5):
        return hits if scope.startswith("pocket:") else []

    async def search_context(scope, query, limit=3):
        return context if scope.startswith("pocket:") else ""

    monkeypatch.setattr(KnowledgeService, "search_articles_for_scope", staticmethod(articles))
    monkeypatch.setattr(KnowledgeService, "search_context_for_scope", staticmethod(search_context))

    items = await concierge_runtime.retrieve(SimpleNamespace(pocket_id="p1"), "tail facts")
    chosen = concierge_runtime.select_knowledge(items)

    assert [i.id for i in chosen] == [h["id"] for h in hits]
    for i, item in enumerate(chosen, start=1):
        assert item.text.endswith(f"Tail fact {i}.")
