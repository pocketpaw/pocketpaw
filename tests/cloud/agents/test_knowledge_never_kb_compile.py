# test_knowledge_never_kb_compile.py — PocketPaw never lets kb-go call an LLM.
#
# kb-go is storage and search only. Every compile goes through PocketPaw's own
# agent backend (metered, observable) and lands via `kb ingest --article-json`,
# whether or not ANTHROPIC_API_KEY happens to be set in the environment. These
# tests pin, with the key SET:
#   * ingest_text_to_scope compiles through the agent backend and calls
#     `kb ingest --article-json`, never plain `kb ingest` (code files too);
#   * a long document still takes the section-wise path;
#   * the `_kb` wrapper refuses every LLM-backed kb command (plain ingest,
#     build, recompile, watch, lint --llm) before a subprocess starts, and the
#     discovery digester's tripwire covers recompile and watch as well.
# The LLM (PocketPawCompilerBackend.complete) and subprocess.run are faked at
# their boundaries; everything between runs for real.
"""PocketPaw compiles every kb article itself; kb-go never calls an LLM."""

from __future__ import annotations

import json
import subprocess

import pytest
from pocketpaw_ee.cloud.agents import knowledge
from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService
from pocketpaw_ee.cloud.kb import backend_adapter

_ARTICLE = {
    "title": "Acme onboarding runbook",
    "summary": "How new Acme hires get access.",
    "content": "# Onboarding\n\n- Accounts on day one\n- Hardware by day three",
    "concepts": ["onboarding"],
    "categories": ["operations"],
}


class _Spy:
    """``subprocess.run`` for kb invocations: records argv + stdin and answers
    every ``ingest --article-json`` with a receipt titled after the article."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._real_run = subprocess.run

    def __call__(self, cmd, input=None, timeout=None, **kwargs):  # noqa: A002
        if not cmd or cmd[0] != knowledge.KB_BIN:
            return self._real_run(cmd, input=input, timeout=timeout, **kwargs)
        self.calls.append({"cmd": list(cmd), "input": input})
        if cmd[1:3] == ["ingest", "--article-json"]:
            article = json.loads(input)["article"]
            receipt = {
                "article": f"art-{len(self.calls)}",
                "title": article.get("title", ""),
                "compiled_with": article.get("compiled_with", ""),
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(receipt), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")


@pytest.fixture
def keyed(monkeypatch) -> _Spy:
    """An environment WITH an Anthropic key, and a kb subprocess spy."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spy = _Spy()
    monkeypatch.setattr(knowledge.subprocess, "run", spy)
    return spy


def _install_compiler(monkeypatch, respond) -> list[str]:
    prompts: list[str] = []

    async def complete(self, prompt: str, system_prompt: str = "") -> str:
        prompts.append(prompt)
        return respond(prompt)

    monkeypatch.setattr(backend_adapter.PocketPawCompilerBackend, "complete", complete)
    return prompts


def _kb_subcommands(spy: _Spy) -> list[list[str]]:
    return [call["cmd"][1:] for call in spy.calls]


# --------------------------------------------------------------------------- #
# (a) the key does not change the compile path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_key_set_still_compiles_with_agent_and_pipes_article_json(monkeypatch, keyed):
    prompts = _install_compiler(monkeypatch, lambda prompt: json.dumps(_ARTICLE))

    result = await KnowledgeService.ingest_text_to_scope(
        "workspace:w1", "Acme onboarding notes.", source="runbook.md"
    )

    assert len(prompts) == 1
    assert "Acme onboarding notes." in prompts[0]
    assert _kb_subcommands(keyed) == [
        ["ingest", "--article-json", "--scope", "workspace:w1", "--json"]
    ]
    payload = json.loads(keyed.calls[0]["input"])
    assert payload["raw_text"] == "Acme onboarding notes."
    assert payload["article"]["compiled_with"].startswith("pocketpaw-agent:")
    assert result["article"] == "art-1"


@pytest.mark.asyncio
async def test_key_set_code_file_compiles_with_agent_not_kb_ast(monkeypatch, keyed, tmp_path):
    """A code file used to carry ``--lang`` so kb-go's own compile ran its AST
    parse. The agent compile gets the language through its code rule instead."""
    prompts = _install_compiler(monkeypatch, lambda prompt: json.dumps(_ARTICLE))
    module = tmp_path / "utils.py"
    module.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    await KnowledgeService.ingest_file("a1", str(module))

    assert len(prompts) == 1
    assert "python" in prompts[0].lower()
    assert _kb_subcommands(keyed) == [["ingest", "--article-json", "--scope", "agent:a1", "--json"]]
    assert "--lang" not in keyed.calls[0]["cmd"]


@pytest.mark.asyncio
async def test_key_set_compile_failure_raises_and_stores_nothing(monkeypatch, keyed):
    _install_compiler(monkeypatch, lambda prompt: "I can't compile this.")

    with pytest.raises(RuntimeError):
        await KnowledgeService.ingest_text_to_scope("workspace:w1", "doc text", source="a.md")

    assert keyed.calls == []


# --------------------------------------------------------------------------- #
# (b) long documents take the section-wise path regardless of the key
# --------------------------------------------------------------------------- #


def _long_doc(sections: int = 4) -> str:
    chapters = []
    for i in range(1, sections + 1):
        body = " ".join(
            f"Item {i}-{j} costs ${i * 10 + j} and ships in {j} days." for j in range(1, 25)
        )
        chapters.append(f"## Chapter {i}\n\n{body}")
    return "\n\n".join(chapters)


@pytest.mark.asyncio
async def test_key_set_long_document_is_ingested_section_by_section(monkeypatch, keyed):
    def respond(prompt: str) -> str:
        section = prompt.split('"""\n', 1)[1].rsplit('\n"""', 1)[0]
        lines = [line for line in section.splitlines() if line.strip()]
        return json.dumps(
            {
                "title": lines[0].lstrip("#").strip(),
                "summary": "A chapter.",
                "content": "\n".join(f"- {line}" for line in lines),
                "concepts": ["prices"],
                "categories": ["store"],
            }
        )

    prompts = _install_compiler(monkeypatch, respond)

    result = await KnowledgeService.ingest_document_to_scope(
        "pocket:p1", _long_doc(4), "prices.md", doc_key="src-1"
    )

    assert result["sections_total"] > 1
    assert result["sections_failed"] == 0
    assert len(result["articles"]) == result["sections_total"]
    assert len(prompts) == result["sections_total"]
    for argv in _kb_subcommands(keyed):
        assert argv == ["ingest", "--article-json", "--scope", "pocket:p1", "--json"]


# --------------------------------------------------------------------------- #
# (c) the kb wrapper refuses every LLM-backed command
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "args",
    [
        ("ingest", "--scope", "workspace:w1", "--source", "a.md"),
        ("ingest", "/tmp/doc.md", "--scope", "workspace:w1"),
        ("build", "/tmp/docs", "--scope", "workspace:w1"),
        ("recompile", "--scope", "workspace:w1"),
        ("watch", "/tmp/docs", "--scope", "workspace:w1"),
        ("lint", "--scope", "workspace:w1", "--llm"),
    ],
)
def test_kb_wrapper_refuses_llm_backed_commands(keyed, args):
    with pytest.raises(RuntimeError, match="kb-go must not compile"):
        knowledge._kb(*args, input_text="doc")

    assert keyed.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ("ingest", "--article-json", "--scope", "workspace:w1"),
        ("search", "q", "--scope", "workspace:w1"),
        ("list", "--scope", "workspace:w1"),
        ("lint", "--scope", "workspace:w1"),
        ("accept", "--scope", "workspace:w1"),
        ("stats", "--scope", "workspace:w1"),
    ],
)
def test_kb_wrapper_allows_storage_and_search_commands(keyed, args):
    knowledge._kb(*args, input_text='{"raw_text": "x", "article": {"compiled_with": "a"}}')

    assert len(keyed.calls) == 1


@pytest.mark.parametrize("command", ["ingest", "build", "recompile", "watch"])
def test_discovery_tripwire_covers_every_compile_command(monkeypatch, command):
    from pocketpaw_ee.discovery import kb_compile

    ran: list = []
    monkeypatch.setattr(kb_compile.subprocess, "run", lambda cmd, **kw: ran.append(cmd))

    with pytest.raises(RuntimeError, match="sovereignty"):
        kb_compile._kb(command, "--scope", "workspace:w1")

    assert ran == []
