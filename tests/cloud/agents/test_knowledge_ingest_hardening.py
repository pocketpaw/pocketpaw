# test_knowledge_ingest_hardening.py — ingest compile hardening tests.
# Created: 2026-08-04 — silent-poisoning fix. On boxes without an
# ANTHROPIC_API_KEY, kb's own LLM compile failed and kb silently stored docs
# VERBATIM, poisoning the scope. These tests pin the new contract:
#   * no key → article compiled via PocketPaw's agent backend and piped to
#     `kb ingest --article-json` (spied at the subprocess boundary: exact
#     argv + stdin payload — the seam under test is NOT mocked away);
#   * key present → the original plain `kb ingest` path, byte-identical argv;
#   * compile failure / garbage / verbatim echo → raises, NO kb call at all;
#   * compiled_with == "none (fallback)" in any ingest result → rejected
#     loudly, warning names the scope and article id;
#   * chat-turn search (search_context_for_scope) fails soft: timeout or
#     subprocess failure → "" plus a warning naming the scope.
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
