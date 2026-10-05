# test_knowledge_ingest_hardening.py — ingest compile hardening tests.
# Created: 2026-08-04 — silent-poisoning fix. On boxes without an
# ANTHROPIC_API_KEY, kb's own LLM compile failed and kb silently stored docs
# VERBATIM, poisoning the scope. These tests pin the new contract:
#   * no key → article compiled via PocketPaw's agent backend and piped to
#     `kb ingest --article-json` (spied at the subprocess boundary: exact
#     argv + stdin payload — the seam under test is NOT mocked away);
#   * key present → the original plain `kb ingest` path, byte-identical argv;
#   * compile failure / garbage / verbatim echo → raises, NO kb call at all.
#     An echo is judged by 8-word runs copied from the input, not length
#     alone, so a fact-dense doc's honest compile is accepted;
#   * compiled_with == "none (fallback)" in any ingest result → rejected
#     loudly, warning names the scope and article id;
#   * chat-turn search (search_context_for_scope) fails soft: timeout or
#     subprocess failure → "" plus a warning naming the scope;
#   * context search reads kb-go's ``--context --json`` entries (a body holding
#     a Markdown rule stays whole), falls back to an old binary's text output,
#     and passes ``--context-chars`` after the query;
#   * URL ingest extracts a page as Markdown (tables and headings kept).
"""Ingest hardening: agent-backend compile, fallback rejection, search guard."""

from __future__ import annotations

import json
import logging
import subprocess

import pytest
from pocketpaw_ee.cloud.agents import knowledge
from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService
from pocketpaw_ee.cloud.kb import backend_adapter


class _SubprocessSpy:
    """Stand-in for ``subprocess.run`` that records exact argv + stdin.

    The seam under test is the kb CLI contract, so the spy captures what
    would hit the binary instead of mocking ``_kb`` away.
    """

    def __init__(self, responses: list) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)
        self._real_run = subprocess.run

    def __call__(self, cmd, input=None, timeout=None, **kwargs):  # noqa: A002
        # Only intercept kb invocations — other code (e.g. the credentials
        # store behind get_settings) may legitimately shell out mid-test.
        if not cmd or cmd[0] != knowledge.KB_BIN:
            return self._real_run(cmd, input=input, timeout=timeout, **kwargs)
        self.calls.append({"cmd": list(cmd), "input": input, "timeout": timeout})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        returncode, stdout, stderr = response
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _install_spy(monkeypatch, responses: list) -> _SubprocessSpy:
    spy = _SubprocessSpy(responses)
    monkeypatch.setattr(knowledge.subprocess, "run", spy)
    return spy


def _install_compiler(monkeypatch, response: str) -> list[dict]:
    """Fake only the LLM boundary (the backend completion), nothing else."""
    calls: list[dict] = []

    async def fake_complete(self, prompt: str, system_prompt: str = "") -> str:
        calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return response

    monkeypatch.setattr(backend_adapter.PocketPawCompilerBackend, "complete", fake_complete)
    return calls


_ARTICLE = {
    "title": "Acme onboarding runbook",
    "summary": "How new Acme hires get access. Covers accounts and hardware.",
    "content": "# Onboarding\n\n- Accounts on day one\n- Hardware by day three",
    "concepts": ["onboarding", "access", "hardware"],
    "categories": ["operations"],
}


# --------------------------------------------------------------------------- #
# Agent-backend compile path (no ANTHROPIC_API_KEY)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_key_compiles_with_agent_and_pipes_article_json(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    compiler_calls = _install_compiler(monkeypatch, json.dumps(_ARTICLE))
    spy = _install_spy(
        monkeypatch,
        [(0, json.dumps({"id": "art-1", "compiled_with": "pocketpaw-agent:claude_agent_sdk"}), "")],
    )

    raw_text = "Acme onboarding: accounts on day one, hardware by day three."
    result = await KnowledgeService.ingest_text_to_scope(
        "workspace:w1", raw_text, source="runbook.md"
    )

    assert result["id"] == "art-1"
    # Exactly one completion, exactly one kb call.
    assert len(compiler_calls) == 1
    assert raw_text in compiler_calls[0]["prompt"]
    assert len(spy.calls) == 1
    cmd = spy.calls[0]["cmd"]
    assert cmd[0] == knowledge.KB_BIN
    assert cmd[1:] == ["ingest", "--article-json", "--scope", "workspace:w1", "--json"]
    payload = json.loads(spy.calls[0]["input"])
    assert payload["raw_text"] == raw_text
    article = payload["article"]
    assert article["title"] == _ARTICLE["title"]
    assert article["summary"] == _ARTICLE["summary"]
    assert article["content"] == _ARTICLE["content"]
    assert article["concepts"] == _ARTICLE["concepts"]
    assert article["categories"] == _ARTICLE["categories"]
    assert article["source"] == "runbook.md"
    assert article["compiled_with"].startswith("pocketpaw-agent:")


@pytest.mark.asyncio
async def test_no_key_tolerates_fenced_json(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _install_compiler(monkeypatch, f"```json\n{json.dumps(_ARTICLE)}\n```")
    spy = _install_spy(
        monkeypatch,
        [(0, json.dumps({"id": "art-2", "compiled_with": "pocketpaw-agent:claude_agent_sdk"}), "")],
    )

    result = await KnowledgeService.ingest_text_to_scope("pocket:p1", "some text", source="s")

    assert result["id"] == "art-2"
    payload = json.loads(spy.calls[0]["input"])
    assert payload["article"]["title"] == _ARTICLE["title"]


@pytest.mark.asyncio
async def test_api_key_keeps_plain_ingest_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    compiler_calls = _install_compiler(monkeypatch, json.dumps(_ARTICLE))
    spy = _install_spy(monkeypatch, [(0, json.dumps({"id": "art-3", "compiled_with": "llm"}), "")])

    result = await KnowledgeService.ingest_text_to_scope("workspace:w1", "doc text", source="a.md")

    assert result["id"] == "art-3"
    assert compiler_calls == []  # no agent-backend compile when kb can do it itself
    cmd = spy.calls[0]["cmd"]
    assert cmd[1:] == ["ingest", "--scope", "workspace:w1", "--source", "a.md", "--json"]
    assert spy.calls[0]["input"] == "doc text"


# --------------------------------------------------------------------------- #
# Compile failure → raises, never a verbatim ingest
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_compile_garbage_raises_and_never_touches_kb(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _install_compiler(monkeypatch, "I'm sorry, I can't compile this document.")
    spy = _install_spy(monkeypatch, [])

    with pytest.raises(RuntimeError, match="compile failed"):
        await KnowledgeService.ingest_text_to_scope("workspace:w1", "doc", source="a.md")

    assert spy.calls == []  # no verbatim ingest attempted


@pytest.mark.asyncio
async def test_compile_empty_fields_rejected(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _install_compiler(monkeypatch, json.dumps({"title": "", "content": "", "summary": "x"}))
    spy = _install_spy(monkeypatch, [])

    with pytest.raises(RuntimeError, match="missing a title or content"):
        await KnowledgeService.ingest_text_to_scope("workspace:w1", "doc", source="a.md")

    assert spy.calls == []


@pytest.mark.asyncio
async def test_compile_verbatim_echo_of_large_doc_rejected(monkeypatch):
    """A large doc whose 'compiled' content is as big as the input is an echo."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    big = "word " * 3000  # 15k chars, over the large-doc threshold
    echo = dict(_ARTICLE, content=big)
    _install_compiler(monkeypatch, json.dumps(echo))
    spy = _install_spy(monkeypatch, [])

    with pytest.raises(RuntimeError, match="verbatim echo"):
        await KnowledgeService.ingest_text_to_scope("workspace:w1", big, source="big.md")

    assert spy.calls == []


def _dense_fact_doc() -> tuple[str, str]:
    """A fact-dense source (a price/service table, like a store's care-and-repair
    guide) and an honest compiled article for it: every fact kept, reworded into
    markdown bullets. Real compiles of such documents land well above 60% of the
    input length because there is nothing to drop — every row is a fact."""
    items = [f"service {i:03d}" for i in range(150)]
    source = "\n".join(
        f"Row {i:03d} | {name} | price ${10 + i % 40} | turnaround {1 + i % 9} business days"
        for i, name in enumerate(items)
    )
    compiled = "# Services and prices\n\n" + "\n".join(
        f"- **{name.title()}** costs ${10 + i % 40} and is ready in {1 + i % 9} business days."
        for i, name in enumerate(items)
    )
    return source, compiled


@pytest.mark.asyncio
async def test_compile_of_a_dense_fact_doc_is_not_mistaken_for_an_echo(monkeypatch):
    """BUG REPRO (2026-10-01): a concierge PDF (a 6.5k-char care, repair and
    pricing guide) failed with ingest_failed while a short .txt worked. The
    compile kept every fact, as the prompt asks, so the article came out above
    60% of the input and the length-only echo check rejected it. A reworded,
    restructured article is not a verbatim echo, whatever its length ratio."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source, compiled = _dense_fact_doc()
    assert len(source) > knowledge._LARGE_DOC_CHARS
    assert len(compiled) > len(source) * knowledge._MAX_COMPILED_RATIO  # the trigger
    _install_compiler(monkeypatch, json.dumps(dict(_ARTICLE, content=compiled)))
    spy = _install_spy(
        monkeypatch,
        [(0, json.dumps({"id": "art-dense", "compiled_with": "pocketpaw-agent:sdk"}), "")],
    )

    result = await KnowledgeService.ingest_text_to_scope(
        "pocket:p1", source, source="cairn-field-guide.pdf"
    )

    assert result["id"] == "art-dense"
    assert json.loads(spy.calls[0]["input"])["article"]["content"] == compiled


def _prose_doc() -> str:
    """~6k chars of varied prose sentences, over the large-doc threshold."""
    return "\n".join(
        f"Step {i}: the technician inspects valve {i * 7} and records pressure "
        f"reading {i * 13} before closing ticket {1000 + i} for customer {i * 3}."
        for i in range(60)
    )


@pytest.mark.asyncio
async def test_lightly_reformatted_echo_of_large_doc_still_rejected(monkeypatch):
    """Markdown bullets and changed whitespace do not turn a copy into a compile:
    the copied-run check ignores punctuation and whitespace."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source = _prose_doc()
    echo = "# Procedure\n\n" + "\n".join(
        "-  " + "  ".join(line.split()) for line in source.splitlines()
    )
    assert len(source) > knowledge._LARGE_DOC_CHARS
    assert len(echo) <= len(source) * knowledge._MAX_COMPILED_CEILING  # not the ceiling
    _install_compiler(monkeypatch, json.dumps(dict(_ARTICLE, content=echo)))
    spy = _install_spy(monkeypatch, [])

    with pytest.raises(RuntimeError, match="verbatim echo"):
        await KnowledgeService.ingest_text_to_scope("workspace:w1", source, source="proc.md")

    assert spy.calls == []


@pytest.mark.asyncio
async def test_compile_longer_than_the_ceiling_rejected(monkeypatch):
    """However little it copies, an article well past its source's length is
    carrying text the source never had."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source, compiled = _dense_fact_doc()
    bloated = (
        compiled
        + "\n\n"
        + "\n".join(f"- Note {i}: ask our team about loyalty discount tier {i}." for i in range(80))
    )
    assert len(bloated) > len(source) * knowledge._MAX_COMPILED_CEILING
    _install_compiler(monkeypatch, json.dumps(dict(_ARTICLE, content=bloated)))
    spy = _install_spy(monkeypatch, [])

    with pytest.raises(RuntimeError, match="longer than its source"):
        await KnowledgeService.ingest_text_to_scope("pocket:p1", source, source="guide.pdf")

    assert spy.calls == []


@pytest.mark.asyncio
async def test_small_doc_is_exempt_from_the_echo_and_ceiling_checks(monkeypatch):
    """A short note's article may copy it and run longer than it: small docs skip
    every length and copy check, as before."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    note = "Shop hours: open nine to five on weekdays, closed on public holidays."
    content = "# Shop hours\n\n" + note + "\n\n" + note
    assert len(note) < knowledge._LARGE_DOC_CHARS
    assert len(content) > len(note) * knowledge._MAX_COMPILED_CEILING
    _install_compiler(monkeypatch, json.dumps(dict(_ARTICLE, content=content)))
    spy = _install_spy(
        monkeypatch,
        [(0, json.dumps({"id": "art-note", "compiled_with": "pocketpaw-agent:x"}), "")],
    )

    result = await KnowledgeService.ingest_text_to_scope("pocket:p1", note, source="hours.txt")

    assert result["id"] == "art-note"
    assert json.loads(spy.calls[0]["input"])["article"]["content"] == content


def test_echo_check_compares_against_the_excerpt_passed_in():
    """The same content is an echo of the text it copies and not of an unrelated
    excerpt of similar length: copying, not length, decides."""
    source = _prose_doc()
    article = dict(_ARTICLE, content=source)
    with pytest.raises(ValueError, match="verbatim echo"):
        knowledge._validate_compiled_article(article, compile_input=source, source="s")

    unrelated = "\n".join(
        f"Unrelated line {i} about pricing tier {i * 5} and opening hours." for i in range(100)
    )
    ratio = len(source) / len(unrelated)
    assert knowledge._MAX_COMPILED_RATIO <= ratio <= knowledge._MAX_COMPILED_CEILING
    validated = knowledge._validate_compiled_article(article, compile_input=unrelated, source="s")
    assert validated["content"] == source.strip()


@pytest.mark.asyncio
async def test_old_binary_silently_ignoring_article_json_fails_loudly(monkeypatch, caplog):
    """Reality check (live-smoke confirmed): kb-go parses flags by hand and
    silently IGNORES unknown flags. An old binary never errors on
    --article-json — it stores the JSON payload verbatim via its keyless
    fallback and exits 0 with old-style output that has NO compiled_with key.
    The missing key is the version-proof old-binary signal and must raise
    with an upgrade hint naming the already-stored article for purging."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _install_compiler(monkeypatch, json.dumps(_ARTICLE))
    # Old-style success output: exit 0, id present, compiled_with absent.
    _install_spy(monkeypatch, [(0, json.dumps({"id": "art-old", "title": "raw"}), "")])

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.agents.knowledge"):
        with pytest.raises(RuntimeError, match="does not support `ingest --article-json`"):
            await KnowledgeService.ingest_text_to_scope("workspace:w1", "doc", source="a.md")

    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "workspace:w1" in warning
    assert "art-old" in warning


# --------------------------------------------------------------------------- #
# Fallback-marker rejection (defense in depth)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fallback_marker_rejected_and_warned(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _install_spy(
        monkeypatch,
        [(0, json.dumps({"id": "art-9", "compiled_with": "none (fallback)"}), "")],
    )

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.agents.knowledge"):
        with pytest.raises(RuntimeError, match="verbatim fallback"):
            await KnowledgeService.ingest_text_to_scope("workspace:w1", "doc", source="a.md")

    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "workspace:w1" in warning
    assert "art-9" in warning


# --------------------------------------------------------------------------- #
# ingest_file text-path reroute
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ingest_file_text_path_routes_through_ingest_funnel(monkeypatch, tmp_path):
    """Text/code files no longer hand kb a file path — they are read in Python
    and piped through ingest_text_to_scope, so they get the same compile
    guarantees as every other doc."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spy = _install_spy(monkeypatch, [(0, json.dumps({"id": "art-f1", "compiled_with": "llm"}), "")])

    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\n\nremember the thing", encoding="utf-8")

    result = await KnowledgeService.ingest_file("a1", str(notes))

    assert result["id"] == "art-f1"
    cmd = spy.calls[0]["cmd"]
    # Funnel argv (stdin ingest), NOT the old direct file-path form
    # ["ingest", "<path>", "--scope", ...]. Non-code file → no --lang hint.
    assert cmd[1:] == ["ingest", "--scope", "agent:a1", "--source", "notes.md", "--json"]
    assert str(notes) not in cmd
    assert spy.calls[0]["input"] == "# Notes\n\nremember the thing"


@pytest.mark.asyncio
async def test_ingest_file_code_path_passes_lang_hint(monkeypatch, tmp_path):
    """Stdin carries no file path, so kb-go can't detect the language itself.
    Code files must carry --lang so kb-go still runs its AST parse — the
    structure awareness the old file-path form provided."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spy = _install_spy(monkeypatch, [(0, json.dumps({"id": "art-f2", "compiled_with": "llm"}), "")])

    module = tmp_path / "utils.py"
    module.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    result = await KnowledgeService.ingest_file("a1", str(module))

    assert result["id"] == "art-f2"
    cmd = spy.calls[0]["cmd"]
    assert cmd[1:] == [
        "ingest",
        "--scope",
        "agent:a1",
        "--source",
        "utils.py",
        "--lang",
        "python",
        "--json",
    ]
    assert spy.calls[0]["input"] == "def add(a, b):\n    return a + b\n"


# --------------------------------------------------------------------------- #
# Chat-turn search guard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_context_timeout_returns_empty_and_warns(monkeypatch, caplog):
    spy = _install_spy(monkeypatch, [subprocess.TimeoutExpired(cmd=["kb"], timeout=5)])

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.agents.knowledge"):
        result = await KnowledgeService.search_context_for_scope("workspace:w1", "query")

    assert result == ""
    assert spy.calls[0]["timeout"] == knowledge.SEARCH_CONTEXT_TIMEOUT_S
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "workspace:w1" in warning


@pytest.mark.asyncio
async def test_search_context_subprocess_failure_returns_empty(monkeypatch, caplog):
    _install_spy(monkeypatch, [(2, "", "index corrupt")])

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.agents.knowledge"):
        result = await KnowledgeService.search_context_for_scope("pocket:p1", "query")

    assert result == ""
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "pocket:p1" in warning


# --------------------------------------------------------------------------- #
# Context search output: kb-go's JSON entries, with the old text as fallback
# --------------------------------------------------------------------------- #

# A body holding a Markdown horizontal rule: the old text output joins articles
# with exactly this separator, so splitting on it cut such a body in half.
_RULED_BODY = "Measure over a base layer.\n\n---\n\n| US men's | EU |\n| --- | --- |\n| 10 | 44 |"


@pytest.mark.asyncio
async def test_context_entries_read_kb_json_and_pass_the_char_budget(monkeypatch):
    entries = [{"id": "size-guide", "title": "Size guide", "text": _RULED_BODY, "truncated": False}]
    spy = _install_spy(monkeypatch, [(0, json.dumps(entries), "")])

    result = await KnowledgeService.search_context_entries_for_scope(
        "pocket:p1", "shoe sizes", limit=3, context_chars=2000
    )

    assert result == entries
    # The query stays the first argument: an old kb-go reads it from there and
    # skips the flags it does not know.
    assert spy.calls[0]["cmd"][1:] == [
        "search",
        "shoe sizes",
        "--scope",
        "pocket:p1",
        "--limit",
        "3",
        "--context",
        "--context-chars",
        "2000",
        "--json",
    ]


@pytest.mark.asyncio
async def test_context_entries_fall_back_to_old_kb_text_output(monkeypatch):
    """An old kb-go ignores --json on --context and prints ``## Title`` blocks."""
    text = "## Size guide\nShoe chart.\n\n---\n\n## Returns\n60-day returns.\n"
    _install_spy(monkeypatch, [(0, text, "")])

    result = await KnowledgeService.search_context_entries_for_scope("pocket:p1", "q")

    assert [(e["id"], e["title"], e["text"]) for e in result] == [
        ("", "Size guide", "Shoe chart."),
        ("", "Returns", "60-day returns."),
    ]


@pytest.mark.asyncio
async def test_context_entries_fail_soft(monkeypatch, caplog):
    _install_spy(monkeypatch, [(2, "", "index corrupt")])

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.agents.knowledge"):
        result = await KnowledgeService.search_context_entries_for_scope("pocket:p1", "q")

    assert result == []
    assert "pocket:p1" in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_chat_context_reads_a_new_kb_json_answer_as_text(monkeypatch):
    """The chat path always passed --json; a kb-go that honours it on --context
    answers with entries, which must still reach the prompt as context text."""
    entries = [
        {"id": "a", "title": "Size guide", "text": _RULED_BODY, "truncated": False},
        {"id": "b", "title": "Returns", "text": "60-day returns.", "truncated": False},
    ]
    spy = _install_spy(monkeypatch, [(0, json.dumps(entries), "")])

    result = await KnowledgeService.search_context_for_scope("workspace:w1", "query")

    assert result == f"## Size guide\n{_RULED_BODY}\n\n---\n\n## Returns\n60-day returns."
    assert "--context-chars" not in spy.calls[0]["cmd"]


# --------------------------------------------------------------------------- #
# URL ingest extraction
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_url_extraction_keeps_tables_and_headings_as_markdown(monkeypatch):
    import re
    from pathlib import Path

    import httpx

    html = (Path(__file__).resolve().parents[2] / "fixtures" / "size_guide.html").read_text(
        encoding="utf-8"
    )
    real_client = httpx.AsyncClient

    def _client(**kw):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )
        return real_client(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    text = await knowledge._extract_url("https://paw-demo-store.vercel.app/size-guide/")

    assert re.search(r"\|\s*10\s*\|\s*11\.5\s*\|\s*9\s*\|\s*44\s*\|\s*28\.0\s*\|", text), text
    assert re.search(r"^#{1,6} Footwear\s*$", text, re.MULTILINE)
    assert "__sveltekit" not in text and "<table" not in text
