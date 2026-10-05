# knowledge.py — agent knowledge service over the kb-go binary.
#
# Every ingest funnels through ``KnowledgeService.ingest_text_to_scope`` (one
# document, one article) or ``ingest_document_to_scope`` (a long document, one
# article per section); the caller decides the scope string (``agent:{id}``,
# ``workspace:{id}``, ``pocket:{id}``). File extraction runs through
# ``ee.cloud.extraction`` and URL extraction through
# ``sites.kb_ingest.html_to_markdown`` (a page's tables and headings kept); kb-go
# does compile, search, index and storage.
#
# kb-go searches compiled articles only, so a fact a compile drops cannot be
# found. Without an API key, ``ingest_document_to_scope`` splits a document over
# ``_SECTION_HARD_MAX_CHARS`` (``knowledge_sections``) and compiles each section
# with a restructure-not-compress prompt, three at a time under one deadline;
# the receipt lists every article id. Concierge sources, site sync, the kb REST
# routes and agent knowledge use it. Upload indexing and its reingest routes stay
# on one article because hide-from-AI tracks a single ``kb_article_id`` per file;
# the book agent does too, because it ingests inside a request.
#
# Invariants a reader must not break:
#   * A document is NEVER stored verbatim. With ANTHROPIC_API_KEY, kb compiles
#     it. Without one, ``_compile_article_with_agent`` compiles it through
#     PocketPaw's own agent backend and pipes the article to
#     ``kb ingest --article-json``. A compile failure raises; there is no
#     fallback. Any receipt with ``compiled_with == "none (fallback)"`` is
#     rejected, and on the --article-json path a receipt with NO compiled_with
#     means an old binary that ignored the flag (kb-go skips unknown flags), so
#     that raises ``KnowledgeEngineUnavailable`` naming the article to purge.
#   * ``_validate_compiled_article`` rejects a backend compile that is a
#     verbatim echo of a large input. An echo is judged by how much of the
#     content is COPIED from the input (8-word shingles), not by length alone:
#     a fact-dense document's honest compile keeps every fact and can be about
#     as long as its source. A hard length ceiling still applies. A SECTION is
#     checked only for empty fields and runaway output
#     (``_validate_section_article``): it is small and the owner's own text.
#   * Section titles lead with the document name, a tag hashed from the
#     caller's ``doc_key`` and "part i of n": kb-go keys an article by its
#     title's slug (80 chars), so no two sections, and no two documents with
#     one file name, may share one.
#   * Chat-turn search (``search_context_for_scope``) fails soft: a 5s timeout
#     or a kb error returns "" with a warning, so the KB never stalls a turn.
#     ``search_context_entries_for_scope`` (the concierge) reads kb-go's
#     ``--context --json`` entries, so a body holding a ``---`` rule stays whole,
#     and falls back to parsing an old binary's text output.
#   * ``extract_ingest_article_id`` is the one place that knows the receipt's
#     key order (id, article_id, article).
"""Agent knowledge service — thin wrapper over the `kb` Go binary.

The kb binary (github.com/qbtrix/kb-go) handles compilation, search, indexing,
and storage. URL extraction stays inline (HTML to Markdown). File extraction is
routed through `ee.cloud.extraction.build_chain` so cloud captioning can be
configured without touching this file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from pocketpaw_ee.cloud.agents.knowledge_sections import Section, split_into_sections

logger = logging.getLogger(__name__)

# The chat turn budget: a KB search that takes longer than this gets killed
# and the turn proceeds without a KB block. See search_context_for_scope.
SEARCH_CONTEXT_TIMEOUT_S = 5

# `kb ingest --article-json` does no LLM work (the article arrives
# pre-compiled), so a minute is generous.
_ARTICLE_JSON_INGEST_TIMEOUT_S = 60

# Ceiling for the agent-backend compile call. Compilation is one completion
# over a capped excerpt, but the backend may cold-start a CLI process.
_AGENT_COMPILE_TIMEOUT_S = 300

# The compiler only sees this much of the raw text. The FULL raw text is
# still stored by kb (raw_text in the --article-json payload) — the cap only
# bounds the LLM prompt.
_COMPILE_INPUT_CAP_CHARS = 80_000

# Echo detection on the agent-backend path, for inputs over _LARGE_DOC_CHARS
# (a short note's article can honestly be as long as the note, so small docs
# skip all three checks). A compile is a verbatim echo when its content is at
# least _MAX_COMPILED_RATIO of the input's length AND at least
# _ECHO_COPIED_FRACTION of its _ECHO_SHINGLE_WORDS-word runs appear verbatim in
# the input. Length alone is not evidence: a price list or care guide is all
# facts, so its honest compile keeps nearly every word and lands near 100%
# of the input while sharing almost no 8-word run with it. Eight words is
# long enough that rewording breaks the run, and short enough that a copied
# paragraph with markdown bullets added still matches (shingles ignore
# punctuation and whitespace).
_LARGE_DOC_CHARS = 4_000
_MAX_COMPILED_RATIO = 0.6
_ECHO_SHINGLE_WORDS = 8
_ECHO_COPIED_FRACTION = 0.5
# Whatever it copies, content past this multiple of the input is rejected. A
# compile restates, so markdown overhead (headings, bullets, bold labels) and
# turning table rows into sentences can push a dense doc to or a little past
# 1.0x (the price-table repro in test_knowledge_ingest_hardening compiles to
# 0.97x). Past 1.25x the article carries text its source never had: padding or
# invention, which a compression prompt should never produce.
_MAX_COMPILED_CEILING = 1.25

# kb-go's marker for "compile failed, stored verbatim". We never accept it.
_FALLBACK_COMPILED_WITH = "none (fallback)"

# Sectioned ingest (``ingest_document_to_scope``). kb-go's ``search --context``
# prints an article's body only while it is under 2,000 bytes (past that it
# prints the summary), and the concierge keeps 2,000 chars of each hit, so a
# compiled section must land under that. Restructuring adds markdown, so the
# raw section target leaves headroom. A document no longer than the hard max is
# one section and keeps the whole-document compile.
_SECTION_TARGET_CHARS = 1_500
_SECTION_HARD_MAX_CHARS = 2_000
# Compiles in flight at once for one document.
_SECTION_CONCURRENCY = 3
# The whole document's budget. It must end well inside the concierge source
# route's 15-minute stale window (``knowledge_routes._STALE_AFTER``); a section
# not compiled by then counts as failed.
_SECTIONED_INGEST_DEADLINE_S = 600
# Runaway output: a section's article may restructure and add markdown, but
# content past either limit is not the section any more.
_SECTION_MAX_GROWTH = 3
_SECTION_MAX_CONTENT_CHARS = 8_000
# A document name is clipped to this in a section's title, so the title's
# document tag and "part i of n" stay inside the 80 characters kb-go keeps of the slug (the
# slug is the article id; two sections must never share one).
_SECTION_TITLE_DOC_CHARS = 40
# Hex chars of the per-document tag in a section title (see ``_document_tag``).
_DOC_TAG_CHARS = 8

# Mirror of kb-go's detectLanguage: source-filename suffixes whose stdin
# ingest should carry a ``--lang`` hint so kb-go runs its AST parse
# (parseCode) on the text — the file PATH no longer reaches kb, so the
# hint is the only way it learns the language. Values are the canonical
# spellings kb-go's langToExt accepts.
_CODE_LANG_BY_SUFFIX = {
    ".go": "go",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
}


def _lang_for_source(source: str) -> str | None:
    """Language hint for a source filename, or ``None`` for non-code docs."""
    return _CODE_LANG_BY_SUFFIX.get(Path(source).suffix.lower())


def _resolve_kb_bin() -> str:
    """Find the kb binary, in order of preference.

    1. ``POCKETPAW_KB_BIN`` env var (explicit override).
    2. ``kb-go`` on PATH (preferred name).
    3. ``kb`` on PATH (alternate name; kb-go releases ship the binary as
       ``kb`` from a build step).
    4. Workspace-local checkout at ``<paw-workspace>/kb-go/kb`` — the
       common dev layout where the kb-go repo sits next to pocketpaw.

    Returns the literal string ``"kb-go"`` when nothing resolves so the
    error message stays informative ("kb binary not found at 'kb-go'").

    Resolved at import time; override via env if your binary moves.
    """
    explicit = os.environ.get("POCKETPAW_KB_BIN")
    if explicit:
        return explicit
    for name in ("kb-go", "kb"):
        path = shutil.which(name)
        if path:
            return path
    # Workspace-local fallback — pocketpaw lives at <ws>/pocketpaw and
    # kb-go at <ws>/kb-go in the canonical OCEAN-workspace layout. Walk
    # up from this file looking for any ancestor that contains kb-go/kb.
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "kb-go" / "kb"
        if candidate.exists():
            return str(candidate)
    return "kb-go"


KB_BIN = _resolve_kb_bin()


class KnowledgeEngineUnavailable(RuntimeError):
    """The kb binary cannot do the job at all: missing, or too old for the
    ingest contract this module speaks. Retrying another document cannot help,
    so a batch caller should stop rather than fail once per document."""


def _kb(*args: str, input_text: str | None = None, timeout: int = 120) -> dict | list | str:
    """Call kb binary, return parsed JSON or raw text."""
    cmd = [KB_BIN, *args, "--json"]
    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise KnowledgeEngineUnavailable(
            f"kb binary not found at {KB_BIN!r}. "
            "Install: go install github.com/qbtrix/kb-go@latest, "
            "or set POCKETPAW_KB_BIN to the binary path (e.g. /path/to/kb-go/kb), "
            "or place the workspace-local checkout at <paw-workspace>/kb-go/kb."
        )
    except subprocess.TimeoutExpired:
        logger.warning("kb timed out after %ds: %s", timeout, " ".join(cmd[:4]))
        raise RuntimeError(f"kb timed out after {timeout}s: {' '.join(cmd[:4])}")
    if result.returncode != 0:
        logger.warning("kb failed (exit %d): %s", result.returncode, result.stderr[:200])
        raise RuntimeError(f"kb failed: {result.stderr[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def _check_ingest_result(
    result: dict | list | str, scope: str, *, require_compiled_with: bool = False
) -> dict | list | str:
    """Reject verbatim-fallback articles (defense in depth).

    kb-go marks an article it stored WITHOUT LLM compilation as
    ``compiled_with == "none (fallback)"`` (older binaries did this silently
    on compile failure; newer ones only with ``--allow-fallback``). A
    verbatim article poisons the scope — search pays O(raw corpus) on every
    chat turn for junk snippets — so any ingest that produced one is treated
    as a failure, never a success.

    ``require_compiled_with`` is the old-binary detector for the
    ``--article-json`` path. kb-go parses flags by hand and silently IGNORES
    unknown flags — an old binary never errors on ``--article-json``; it
    reads the ``{"raw_text": ..., "article": ...}`` payload from stdin as
    raw text, stores the JSON wrapper verbatim via its keyless fallback, and
    exits 0 with old-style output that predates the ``compiled_with`` field.
    So on that path a MISSING ``compiled_with`` key IS the old-binary signal
    (the paired binary always emits it), and the poison has already landed —
    the error names the article so an operator can purge it.
    """
    if isinstance(result, dict) and result.get("compiled_with") == _FALLBACK_COMPILED_WITH:
        article_id = result.get("id") or result.get("article_id") or result.get("article") or "?"
        logger.warning(
            "kb ingest stored a VERBATIM fallback article (scope=%s, article_id=%s); "
            "rejecting — the scope may need a purge (kb delete %s --scope %s)",
            scope,
            article_id,
            article_id,
            scope,
        )
        raise RuntimeError(
            f"kb ingest produced a verbatim fallback article (scope={scope}, "
            f"article_id={article_id}); refusing to accept uncompiled content"
        )
    if require_compiled_with and (not isinstance(result, dict) or "compiled_with" not in result):
        article_id = "?"
        if isinstance(result, dict):
            article_id = (
                result.get("id") or result.get("article_id") or result.get("article") or "?"
            )
        logger.warning(
            "kb ingest --article-json returned no compiled_with (scope=%s, article_id=%s): "
            "the kb binary silently ignored the flag and stored the payload VERBATIM. "
            "Purge the article (kb delete %s --scope %s) and deploy the paired kb-go build.",
            scope,
            article_id,
            article_id,
            scope,
        )
        raise KnowledgeEngineUnavailable(
            f"kb binary does not support `ingest --article-json` — it silently ignored "
            f"the flag and stored the payload verbatim (scope={scope}, "
            f"article_id={article_id}). Deploy the paired kb-go build (binary: {KB_BIN}) "
            f"and purge the article (kb delete {article_id} --scope {scope})."
        )
    return result


def extract_ingest_article_id(result: dict | list | str | None) -> str | None:
    """Article id from a kb ingest receipt, or ``None``.

    kb-go's ``finishIngest`` emits ``{"article": "<id>", "title": ...,
    "words": ..., "compiled_with": ...}`` — the id key is ``article``, not
    ``id``. Older/other shapes used ``id``/``article_id``; mirror
    ``_check_ingest_result``'s key order so every receipt shape resolves.
    Callers that read ``result["id"]`` directly never saw the id (FL-11b
    tracking silently never fired) — always go through this helper.
    """
    if not isinstance(result, dict):
        return None
    for key in ("id", "article_id", "article"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def count_document_sections(text: str) -> int:
    """How many sections ``ingest_document_to_scope`` would split ``text`` into."""
    return len(
        split_into_sections(text, target=_SECTION_TARGET_CHARS, hard_max=_SECTION_HARD_MAX_CHARS)
    )


def extract_ingest_article_ids(result: dict | list | str | None) -> list[str]:
    """Every article id a receipt names: the ``articles`` list of a sectioned
    ingest, else the single id ``extract_ingest_article_id`` finds."""
    if isinstance(result, dict) and isinstance(result.get("articles"), list):
        return [a for a in result["articles"] if isinstance(a, str) and a]
    article_id = extract_ingest_article_id(result)
    return [article_id] if article_id else []


def _parse_article_json(raw: str) -> dict:
    """Extract the article JSON object from an LLM response.

    Tolerates markdown fences and stray prose around the object; raises
    ``ValueError`` when no JSON object can be recovered.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty compiler response")
    candidates = [text]
    if "```" in text:
        # Take the first fenced block's body.
        parts = text.split("```")
        if len(parts) >= 3:
            body = parts[1]
            if body.startswith("json"):
                body = body[4:]
            candidates.append(body.strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"compiler response is not a JSON object: {text[:200]!r}")


_WORD_RE = re.compile(r"\w+")


def _shingles(text: str) -> list[tuple[str, ...]]:
    """The text's overlapping ``_ECHO_SHINGLE_WORDS``-word runs, lowercased, with
    punctuation and whitespace ignored so reformatting does not hide a copy."""
    words = _WORD_RE.findall(text.lower())
    n = _ECHO_SHINGLE_WORDS
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


def _copied_fraction(content: str, compile_input: str) -> float:
    """Share of ``content``'s word runs that appear verbatim in ``compile_input``."""
    runs = _shingles(content)
    if not runs:
        return 0.0
    source_runs = set(_shingles(compile_input))
    return sum(run in source_runs for run in runs) / len(runs)


def _validate_compiled_article(article: dict, *, compile_input: str, source: str) -> dict:
    """Normalize + sanity-check a backend-compiled article.

    ``compile_input`` is the exact excerpt the compiler saw. Raises
    ``ValueError`` on garbage: empty title/content, or (for large docs) content
    that is mostly copied from the input at a length close to it (a verbatim
    echo, exactly the poisoning we're preventing), or content longer than
    ``_MAX_COMPILED_CEILING`` times the input.
    """
    title = str(article.get("title") or "").strip()
    content = str(article.get("content") or "").strip()
    if not title or not content:
        raise ValueError("compiled article is missing a title or content")
    input_len = len(compile_input)
    if input_len > _LARGE_DOC_CHARS:
        ratio = len(content) / input_len
        if ratio > _MAX_COMPILED_CEILING:
            raise ValueError(
                f"compiled article is longer than its source: content is {len(content)} "
                f"chars against a {input_len}-char input (limit "
                f"{_MAX_COMPILED_CEILING:.0%})"
            )
        if ratio >= _MAX_COMPILED_RATIO:
            copied = _copied_fraction(content, compile_input)
            if copied >= _ECHO_COPIED_FRACTION:
                raise ValueError(
                    f"compiled article is not a compression: content is {len(content)} "
                    f"chars against a {input_len}-char input and {copied:.0%} of it is "
                    f"copied verbatim (limits {_MAX_COMPILED_RATIO:.0%} length, "
                    f"{_ECHO_COPIED_FRACTION:.0%} copied) — looks like a verbatim echo"
                )
    return _normalized_article(article, title=title, content=content, source=source)


def _normalized_article(article: dict, *, title: str, content: str, source: str) -> dict:
    """The ``--article-json`` article fields, trimmed, from a parsed compile."""
    concepts = [str(c).strip() for c in article.get("concepts") or [] if str(c).strip()]
    categories = [str(c).strip() for c in article.get("categories") or [] if str(c).strip()]
    return {
        "title": title,
        "summary": str(article.get("summary") or "").strip(),
        "content": content,
        "concepts": concepts,
        "categories": categories,
        "source": source,
    }


async def _compile_article_with_agent(text: str, source: str, lang: str | None = None) -> dict:
    """Compile ``text`` into a kb article using PocketPaw's own agent backend.

    This is the no-ANTHROPIC_API_KEY path: kb's internal LLM compile cannot
    run, so we produce the article with the same backend infrastructure the
    chat runtime uses (``PocketPawCompilerBackend`` → agent registry → the
    active backend, e.g. the Claude Code SDK backend which authenticates via
    the CLI, not the API key).

    ``lang`` (when the source is a recognized code file) steers the article
    toward documenting code structure instead of prose-summarizing — the
    keyless stand-in for the AST parse kb-go runs on the keyed path.

    Failures raise: compile timeouts and invalid/garbage articles are
    translated to ``RuntimeError``; backend-level errors (an unavailable
    backend, a failing completion) propagate as raised. Either way callers
    must NEVER fall back to verbatim ingestion.
    """
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.kb.backend_adapter import PocketPawCompilerBackend

    excerpt = text[:_COMPILE_INPUT_CAP_CHARS]
    truncated = len(text) > len(excerpt)
    code_rule = _code_rule(lang)
    prompt = (
        "Compile the document below into a knowledge-base article. Respond with "
        "ONLY one JSON object, no prose and no markdown fences:\n"
        '{"title": "...", "summary": "...", "content": "...", '
        '"concepts": ["..."], "categories": ["..."]}\n\n'
        "Rules:\n"
        "- title: short and descriptive.\n"
        "- summary: at most 2 sentences.\n"
        "- content: a well-structured wiki-style article (markdown headings and "
        "lists) that COMPRESSES the document — capture the facts, structure, "
        "names, and numbers; do NOT reproduce the document verbatim.\n"
        + code_rule
        + "- concepts: 3-10 key concepts.\n"
        "- categories: 1-3 broad categories.\n\n"
        f"Source: {source}\n"
        + ("(Document truncated for compilation; compress what you see.)\n" if truncated else "")
        + f'Document:\n"""\n{excerpt}\n"""'
    )
    backend = PocketPawCompilerBackend()
    try:
        raw = await asyncio.wait_for(
            backend.complete(
                prompt,
                system_prompt=(
                    "You are a knowledge-base article compiler. "
                    "Output ONLY a single valid JSON object."
                ),
            ),
            timeout=_AGENT_COMPILE_TIMEOUT_S,
        )
    except TimeoutError:
        raise RuntimeError(
            f"agent-backend article compile timed out after {_AGENT_COMPILE_TIMEOUT_S}s "
            f"(source={source!r})"
        )
    try:
        article = _validate_compiled_article(
            _parse_article_json(raw), compile_input=excerpt, source=source
        )
    except ValueError as exc:
        raise RuntimeError(f"agent-backend article compile failed for {source!r}: {exc}")
    article["compiled_with"] = f"pocketpaw-agent:{get_settings().agent_backend}"
    return article


def _code_rule(lang: str | None) -> str:
    """The compile-prompt rule for a recognized code file, or ``""``."""
    if not lang:
        return ""
    return (
        f"- The document is {lang} source code: in the content, document its "
        "structure — the module's purpose, key functions and classes with their "
        "signatures, and exports — rather than summarizing it as prose.\n"
    )


def _document_tag(scope: str, source: str, doc_key: str | None) -> str:
    """A short stable tag for one document, from the caller's ``doc_key`` (a
    source id, a site page). Without one, a per-ingest nonce: the document
    still cannot collide with another, but a re-ingest makes new articles."""
    basis = f"key:{doc_key}" if doc_key else f"nonce:{scope}:{source}:{uuid.uuid4().hex}"
    return hashlib.sha256(basis.encode()).hexdigest()[:_DOC_TAG_CHARS]


def _section_title(topic: str, doc_source: str, index: int, total: int, tag: str) -> str:
    """``<document> [tag] — part i of n: <topic>``. The document name, its tag
    and the part number lead, inside the 80 characters kb-go keeps of the slug
    it keys the article by, so the id is unique per section AND per document:
    two documents with one file name never overwrite each other's sections."""
    doc = (Path(doc_source).name or doc_source).strip() or "document"
    if len(doc) > _SECTION_TITLE_DOC_CHARS:
        doc = doc[: _SECTION_TITLE_DOC_CHARS - 1].rstrip() + "…"
    return f"{doc} [{tag}] — part {index} of {total}: {topic}"


def _validate_section_article(article: dict, *, section_text: str, source: str) -> dict:
    """Normalize a compiled section. Rejects only an empty title or content, or
    runaway output (content past ``_SECTION_MAX_GROWTH`` times the section or
    ``_SECTION_MAX_CONTENT_CHARS``). There is no echo or compression check: a
    section is small and is the owner's own text, so a near-verbatim article
    is an acceptable one."""
    title = str(article.get("title") or "").strip()
    content = str(article.get("content") or "").strip()
    if not title or not content:
        raise ValueError("compiled section is missing a title or content")
    limit = min(_SECTION_MAX_GROWTH * len(section_text), _SECTION_MAX_CONTENT_CHARS)
    if len(content) > limit:
        raise ValueError(
            f"compiled section runs away: content is {len(content)} chars against a "
            f"{len(section_text)}-char section (limit {limit})"
        )
    return _normalized_article(article, title=title, content=content, source=source)


async def _compile_section_with_agent(
    section: Section,
    doc_source: str,
    index: int,
    total: int,
    lang: str | None = None,
    *,
    tag: str = "",
    retry: bool = False,
    timeout: float = _AGENT_COMPILE_TIMEOUT_S,
) -> dict:
    """Compile one section into a kb article with PocketPaw's agent backend.

    The prompt RESTRUCTURES, it does not compress: every name, number, price,
    date, quantity, condition and contact detail stays as written. ``retry``
    adds a JSON-only reminder for the second attempt. The returned title is
    ``_section_title`` around the compiler's topic. Raises ``RuntimeError`` on
    a timeout or an unusable article; backend errors propagate.
    """
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.kb.backend_adapter import PocketPawCompilerBackend

    where = f"Section {index} of {total}" + (
        f', under the heading "{section.title_hint}"' if section.title_hint else ""
    )
    prompt = (
        (
            "Your previous reply could not be used. Reply with the JSON object "
            "ONLY: no prose before or after it, no markdown fences.\n\n"
            if retry
            else ""
        )
        + "Restructure ONE section of a business document into a knowledge-base "
        "article. Respond with ONLY one JSON object, no prose and no markdown fences:\n"
        '{"title": "...", "summary": "...", "content": "...", '
        '"concepts": ["..."], "categories": ["..."]}\n\n'
        "Rules:\n"
        "- title: the section's topic in a few words (the document name is added "
        "for you).\n"
        "- summary: one sentence saying what the section covers.\n"
        "- content: the section as compact markdown (headings, lists, tables). "
        "Keep every name, number, price, date, quantity, condition and contact "
        "detail exactly as written. Restructure, do not compress: you may merge "
        "duplicates, but do not drop facts and do not invent anything.\n"
        + _code_rule(lang)
        + "- concepts: 3-10 key concepts.\n"
        "- categories: 1-3 broad categories.\n\n"
        f"Document: {doc_source}\n{where}:\n"
        f'"""\n{section.text}\n"""'
    )
    backend = PocketPawCompilerBackend()
    try:
        raw = await asyncio.wait_for(
            backend.complete(
                prompt,
                system_prompt=(
                    "You are a knowledge-base article compiler. "
                    "Output ONLY a single valid JSON object."
                ),
            ),
            timeout=timeout,
        )
    except TimeoutError:
        raise RuntimeError(f"section {index} of {total} compile timed out after {timeout:.0f}s")
    try:
        article = _validate_section_article(
            _parse_article_json(raw), section_text=section.text, source=doc_source
        )
    except ValueError as exc:
        raise RuntimeError(f"section {index} of {total} compile failed: {exc}")
    article["title"] = _section_title(article["title"], doc_source, index, total, tag)
    article["compiled_with"] = f"pocketpaw-agent:{get_settings().agent_backend}"
    return article


async def _ingest_compiled_article(scope: str, raw_text: str, article: dict) -> dict:
    """``kb ingest --article-json`` for one pre-compiled article, with the
    old-binary and verbatim-fallback checks."""
    payload = json.dumps({"raw_text": raw_text, "article": article})
    try:
        result = await asyncio.to_thread(
            _kb,
            "ingest",
            "--article-json",
            "--scope",
            scope,
            input_text=payload,
            timeout=_ARTICLE_JSON_INGEST_TIMEOUT_S,
        )
    except RuntimeError as exc:
        # Belt-and-braces only: current kb-go parses flags by hand and
        # silently IGNORES unknown ones, so an old binary never produces
        # a flag error. The PRIMARY old-binary detector is the missing
        # ``compiled_with`` key below (require_compiled_with).
        msg = str(exc)
        if "unknown flag" in msg or "flag provided but not defined" in msg:
            raise KnowledgeEngineUnavailable(
                "kb binary does not support `ingest --article-json` — it predates "
                "the pre-compiled-article contract. Deploy the paired kb-go build "
                f"(binary: {KB_BIN}). Original error: {msg}"
            ) from exc
        raise
    # The paired binary ALWAYS emits compiled_with on this path; a result
    # without it means the flag was silently ignored (old binary) and the
    # payload was stored verbatim — reject loudly, naming the article.
    return _check_ingest_result(result, scope, require_compiled_with=True)


async def _ingest_sections(
    scope: str, sections: list[Section], source: str, lang: str | None, tag: str
) -> dict:
    """Compile and ingest each section as its own article, titled with the
    document's ``tag`` (``_document_tag``).

    At most ``_SECTION_CONCURRENCY`` compiles run at once, and the kb writes
    are serialized (each ``kb ingest`` rebuilds the scope's indexes from the
    articles on disk, so two at once could each write an index missing the
    other's article). A failed compile is retried once with a JSON-only
    reminder. Everything shares one ``_SECTIONED_INGEST_DEADLINE_S`` budget: a
    section still waiting or compiling when it runs out fails with
    ``deadline``. A missing or outdated kb binary stops the document and
    raises ``KnowledgeEngineUnavailable``; if no section landed, raises
    ``RuntimeError``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SECTIONED_INGEST_DEADLINE_S
    total = len(sections)
    gate = asyncio.Semaphore(_SECTION_CONCURRENCY)
    kb_write = asyncio.Lock()
    receipts: dict[int, dict] = {}
    failures: dict[int, str] = {}
    engine_error: list[KnowledgeEngineUnavailable] = []

    async def compile_one(index: int, section: Section) -> dict:
        for retry in (False, True):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("deadline")
            try:
                return await _compile_section_with_agent(
                    section,
                    source,
                    index,
                    total,
                    lang,
                    tag=tag,
                    retry=retry,
                    timeout=min(_AGENT_COMPILE_TIMEOUT_S, remaining),
                )
            except Exception as exc:  # noqa: BLE001 — any failure earns one retry
                if retry:
                    raise
                logger.info(
                    "kb section %d of %d of %r failed once, retrying: %s",
                    index,
                    total,
                    source,
                    exc,
                )
        raise AssertionError("unreachable")

    async def run(index: int, section: Section) -> None:
        try:
            await asyncio.wait_for(gate.acquire(), max(deadline - loop.time(), 0))
        except TimeoutError:
            failures[index] = "deadline"
            return
        try:
            if engine_error:
                failures[index] = "kb_unavailable"
                return
            article = await compile_one(index, section)
        except TimeoutError:
            failures[index] = "deadline"
            return
        except Exception as exc:  # noqa: BLE001 — one section's failure is that section's
            failures[index] = str(exc) or type(exc).__name__
            return
        finally:
            gate.release()
        async with kb_write:
            if engine_error:
                failures[index] = "kb_unavailable"
                return
            try:
                receipts[index] = await _ingest_compiled_article(scope, section.text, article)
            except KnowledgeEngineUnavailable as exc:
                engine_error.append(exc)
                failures[index] = "kb_unavailable"
            except Exception as exc:  # noqa: BLE001
                failures[index] = str(exc) or type(exc).__name__

    await asyncio.gather(*(run(i, s) for i, s in enumerate(sections, start=1)))

    ids: list[str] = []
    titles: list[str] = []
    compiled_with = ""
    for index in sorted(receipts):
        article_id = extract_ingest_article_id(receipts[index])
        if not article_id:
            failures[index] = "no article id in the kb receipt"
            continue
        ids.append(article_id)
        titles.append(str(receipts[index].get("title") or ""))
        compiled_with = compiled_with or str(receipts[index].get("compiled_with") or "")
    for index in sorted(failures):
        logger.warning(
            "kb sectioned ingest: section %d of %d of %r (scope=%s, heading=%r) failed: %s",
            index,
            total,
            source,
            scope,
            sections[index - 1].title_hint,
            failures[index],
        )
    if engine_error:
        raise engine_error[0]
    if not ids:
        raise RuntimeError(
            f"every section of {source!r} failed to ingest ({total} sections): "
            + "; ".join(f"{i}: {failures[i]}" for i in sorted(failures))
        )
    return {
        "article": ids[0],
        "title": titles[0],
        "articles": ids,
        "titles": titles,
        "compiled_with": compiled_with,
        "sections_total": total,
        "sections_failed": len(failures),
        "failures": [
            {"section": i, "heading": sections[i - 1].title_hint, "reason": failures[i]}
            for i in sorted(failures)
        ],
    }


class KnowledgeService:
    """Knowledge operations via the kb Go binary.

    All ingest paths funnel through :meth:`ingest_text_to_scope` or its
    sectioned twin :meth:`ingest_document_to_scope`, so the scope shape
    (``agent:{id}``, ``workspace:{id}``, ``pocket:{id}``) is decided by the
    caller, not by this class.
    """

    @staticmethod
    async def ingest_text_to_scope(scope: str, text: str, source: str = "manual") -> dict:
        """Ingest ``text`` into an arbitrary kb-go scope.

        ``scope`` is the literal scope string the kb binary understands
        (e.g. ``"workspace:w1"``, ``"agent:a1"``, ``"pocket:p1"``). No
        validation here — kb-go rejects unknown scope shapes itself.

        Compilation strategy (2026-08-04 hardening):

        * ``ANTHROPIC_API_KEY`` set → plain ``kb ingest``; kb compiles the
          article with its own LLM call (fast, works, unchanged).
        * No key (e.g. the Claude Code agent-backend deployment) → compile
          the article with PocketPaw's OWN agent backend and hand kb the
          pre-compiled article via ``kb ingest --article-json``. kb makes no
          LLM call of its own on this path.

        Either way, a compile failure RAISES. There is no verbatim
        fallback — an uncompiled article poisons the scope and makes every
        chat turn pay O(raw corpus) search cost for junk snippets.
        """
        lang = _lang_for_source(source)
        if os.environ.get("ANTHROPIC_API_KEY"):
            args = ["ingest", "--scope", scope, "--source", source]
            if lang:
                # Stdin carries no file path, so kb-go can't detect the
                # language itself — the hint re-enables its AST parse.
                args += ["--lang", lang]
            result = await asyncio.to_thread(_kb, *args, input_text=text)
            return _check_ingest_result(result, scope)

        article = await _compile_article_with_agent(text, source, lang=lang)
        return await _ingest_compiled_article(scope, text, article)

    @staticmethod
    async def ingest_document_to_scope(
        scope: str, text: str, source: str = "manual", *, doc_key: str | None = None
    ) -> dict:
        """Ingest a document so that every fact in it stays searchable.

        The sectioned twin of :meth:`ingest_text_to_scope`, for callers whose
        documents can be long and dense (price lists, policies, spec sheets).
        Without ``ANTHROPIC_API_KEY``, a document over ``_SECTION_HARD_MAX_CHARS``
        is split by ``split_into_sections`` and each section is compiled and
        ingested as its own article (``_ingest_sections``); the receipt lists
        every article id under ``articles``. A short document, and every
        document on the API-key path, goes through :meth:`ingest_text_to_scope`
        unchanged, and its receipt gains a one-element ``articles`` list.

        ``doc_key`` is the caller's stable identity for the document (a source
        id, a site page). It is hashed into every section title, so two
        documents with the same name never share an article, and the same
        document re-ingested lands on the same ids. Without it each ingest gets
        a fresh tag: no collision, but a re-ingest writes new articles.

        Raises when nothing was ingested. A partial result returns with
        ``sections_failed`` > 0 and a ``failures`` list.
        """
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sections = split_into_sections(
                text, target=_SECTION_TARGET_CHARS, hard_max=_SECTION_HARD_MAX_CHARS
            )
            if len(sections) > 1:
                tag = _document_tag(scope, source, doc_key)
                return await _ingest_sections(
                    scope, sections, source, _lang_for_source(source), tag
                )
        result = await KnowledgeService.ingest_text_to_scope(scope, text, source)
        if isinstance(result, dict):
            article_id = extract_ingest_article_id(result)
            result = {
                **result,
                "articles": [article_id] if article_id else [],
                "sections_total": 1,
                "sections_failed": 0,
            }
        return result

    @staticmethod
    async def ingest_text(agent_id: str, text: str, source: str = "manual") -> dict:
        return await KnowledgeService.ingest_document_to_scope(f"agent:{agent_id}", text, source)

    @staticmethod
    async def ingest_url(agent_id: str, url: str) -> dict:
        """Fetch a URL, extract it (HTML as Markdown), pipe the text to kb."""
        try:
            text = await _extract_url(url)
            return await KnowledgeService.ingest_document_to_scope(f"agent:{agent_id}", text, url)
        except Exception as exc:
            return {"error": str(exc), "url": url}

    @staticmethod
    async def ingest_file(agent_id: str, file_path: str, source: str | None = None) -> dict:
        """Extract file content (PDF/DOCX via Python if needed), pipe to kb.

        ``source`` overrides the stored title/source — pass the original
        filename so the KB doesn't store temp paths.
        """
        path = Path(file_path)
        label = source or path.name
        if path.suffix.lower() in (".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"):
            text = await _extract_file(file_path)
            return await KnowledgeService.ingest_document_to_scope(f"agent:{agent_id}", text, label)
        # Text/code files: read in Python and route through the common ingest
        # path so they get the same compile guarantees (agent-backend compile
        # without an API key, verbatim-fallback rejection) as every other doc.
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        return await KnowledgeService.ingest_document_to_scope(f"agent:{agent_id}", text, label)

    @staticmethod
    async def list_articles(agent_id: str) -> list[dict]:
        """List ingested articles for an agent."""
        result = await asyncio.to_thread(_kb, "list", "--scope", f"agent:{agent_id}")
        return result if isinstance(result, list) else []

    @staticmethod
    async def get_article(agent_id: str, article_id: str) -> dict:
        """Fetch a single article's full body."""
        result = await asyncio.to_thread(_kb, "show", article_id, "--scope", f"agent:{agent_id}")
        return result if isinstance(result, dict) else {"content": str(result)}

    @staticmethod
    async def get_article_for_scope(scope: str, article_id: str) -> dict:
        """One article of any scope as ``{id, title, summary, content, ...}``.

        Scope-form sibling of :meth:`get_article`. Raises on subprocess failure
        (an unknown id makes kb-go exit non-zero); callers on the public concierge
        path wrap it.
        """
        result = await asyncio.to_thread(_kb, "show", article_id, "--scope", scope)
        return result if isinstance(result, dict) else {"content": str(result)}

    @staticmethod
    async def remove_article(scope: str, article_id: str) -> bool:
        """Delete a single article from a kb-go scope (FL-11b purge path).

        Mirrors :meth:`get_article` but calls ``kb delete``. Used to
        retroactively purge a file's KB content when it's hidden from AI after
        having been indexed. Resilient like the other kb calls: any subprocess
        error is logged and swallowed (returns ``False``) so a purge failure
        never breaks the caller (the hide flag is still applied; a sweeper can
        re-purge). kb-go's ``delete`` is idempotent — deleting a missing id is a
        no-op. Returns ``True`` when the subprocess call completed without
        raising.
        """
        try:
            await asyncio.to_thread(_kb, "delete", article_id, "--scope", scope)
            return True
        except Exception:
            logger.warning(
                "kb delete failed for article_id=%s scope=%s; KB content may "
                "still be retrievable (retry/sweeper can re-purge)",
                article_id,
                scope,
                exc_info=True,
            )
            return False

    @staticmethod
    async def search(agent_id: str, query: str, limit: int = 5) -> list[str]:
        results = await asyncio.to_thread(
            _kb,
            "search",
            query,
            "--scope",
            f"agent:{agent_id}",
            "--limit",
            str(limit),
        )
        if isinstance(results, list):
            return [r.get("summary", r.get("title", "")) for r in results]
        return []

    @staticmethod
    async def search_articles_for_scope(scope: str, query: str, limit: int = 5) -> list[dict]:
        """Raw search hits (``{id, title, summary, concepts}`` dicts) for any scope.

        The scope-form sibling of :meth:`search` — callers that need the article
        id and title (not a formatted context block) use this. Like the other
        scope-form entry points, the scope string is the caller's to shape
        (``pocket:{pid}``, ``workspace:{wid}``); kb-go validates it itself.
        Raises on subprocess failure — callers that must be fail-soft (the public
        concierge sources path) wrap it.
        """
        results = await asyncio.to_thread(
            _kb, "search", query, "--scope", scope, "--limit", str(limit)
        )
        return results if isinstance(results, list) else []

    @staticmethod
    async def list_articles_for_scope(scope: str) -> list[dict]:
        """List a scope's articles as raw ``{id, title, summary, ...}`` dicts.

        Scope-form sibling of :meth:`list_articles`, same contract notes as
        :meth:`search_articles_for_scope`.
        """
        result = await asyncio.to_thread(_kb, "list", "--scope", scope)
        return result if isinstance(result, list) else []

    @staticmethod
    async def search_context(agent_id: str, query: str, limit: int = 3) -> str:
        """Get formatted knowledge context for agent prompt injection."""
        return await KnowledgeService.search_context_for_scope(
            scope=f"agent:{agent_id}",
            query=query,
            limit=limit,
        )

    @staticmethod
    async def search_context_for_scope(
        scope: str,
        query: str,
        limit: int = 3,
        *,
        timeout: int = SEARCH_CONTEXT_TIMEOUT_S,
        context_chars: int | None = None,
    ) -> str:
        """Get formatted knowledge context for any kb-go scope.

        Runs ``_kb`` in a thread so the event loop isn't blocked by the
        subprocess call. See S2 in the code review for context.

        This is the chat-turn path (``_build_kb_snippets_block`` calls it on
        EVERY turn), so it is fail-soft with a hard timeout: on timeout or
        any subprocess failure it logs a warning and returns ``""`` — the
        caller simply skips the KB block. A slow or broken KB must never
        stall a chat turn.

        The result is ``## Title\\nbody`` blocks joined by ``---`` whichever
        output kb-go gives: ``_kb`` always passes ``--json``, which an old
        binary ignores on ``--context`` (text) and a newer one honours
        (entries, re-joined here). ``context_chars`` passes kb-go's
        per-article budget (``--context-chars``).
        """
        result = await _search_context(scope, query, limit, timeout, context_chars)
        if isinstance(result, list):
            return format_context_entries(_context_entries(result))
        return result if isinstance(result, str) else ""

    @staticmethod
    async def search_context_entries_for_scope(
        scope: str,
        query: str,
        limit: int = 3,
        *,
        timeout: int = SEARCH_CONTEXT_TIMEOUT_S,
        context_chars: int | None = None,
    ) -> list[dict]:
        """Context search as entries: ``[{"id", "title", "text", "truncated"}]``.

        Each body arrives whole (a body holding a Markdown ``---`` rule is not
        split) when kb-go answers ``--context --json``. An old binary prints
        text instead, which is parsed as before (``parse_context_text``: ids
        empty, bodies split on the separator). ``context_chars`` is kb-go's
        per-article budget: past it a newer binary returns an excerpt focused on
        the query rather than the summary. Same fail-soft contract as
        :meth:`search_context_for_scope`: any failure returns ``[]``.
        """
        result = await _search_context(scope, query, limit, timeout, context_chars)
        if isinstance(result, list):
            return _context_entries(result)
        return parse_context_text(result) if isinstance(result, str) else []

    @staticmethod
    async def clear(agent_id: str) -> dict:
        result = await asyncio.to_thread(_kb, "clear", "--scope", f"agent:{agent_id}")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def stats(agent_id: str) -> dict:
        return _kb("stats", "--scope", f"agent:{agent_id}")

    @staticmethod
    async def lint(agent_id: str) -> list[dict]:
        result = await asyncio.to_thread(_kb, "lint", "--scope", f"agent:{agent_id}")
        return result if isinstance(result, list) else []


# --- Context search output ---

# kb-go's text ``--context`` output joins ``## Title\nbody`` blocks with this.
_CONTEXT_SEPARATOR = "\n\n---\n\n"


async def _search_context(
    scope: str, query: str, limit: int, timeout: int, context_chars: int | None
) -> dict | list | str | None:
    """``kb search --context`` for one scope, or None on any failure (logged).

    The query stays the first argument: an old kb-go reads it from there and
    skips flags it does not know (``--context-chars``)."""
    args = ["search", query, "--scope", scope, "--limit", str(limit), "--context"]
    if context_chars:
        args += ["--context-chars", str(int(context_chars))]
    start = time.monotonic()
    try:
        return await asyncio.to_thread(_kb, *args, timeout=timeout)
    except Exception:
        logger.warning(
            "kb search for chat context failed (scope=%s, elapsed=%.1fs, "
            "timeout=%ds); returning empty context",
            scope,
            time.monotonic() - start,
            timeout,
            exc_info=True,
        )
        return None


def _context_entries(raw: list) -> list[dict]:
    """kb-go's ``--context --json`` entries, normalized; malformed rows dropped."""
    entries: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        text = str(row.get("text") or "").strip()
        if not title and not text:
            continue
        entries.append(
            {
                "id": str(row.get("id") or ""),
                "title": title,
                "text": text,
                "truncated": bool(row.get("truncated", False)),
            }
        )
    return entries


def parse_context_text(context: str) -> list[dict]:
    """Entries from an old kb-go's text ``--context`` output (``## Title\\nbody``
    blocks joined by ``---``). The text carries no ids, and a body holding the
    separator itself is cut there: the reason newer callers ask for JSON."""
    entries: list[dict] = []
    for block in context.split(_CONTEXT_SEPARATOR):
        block = block.strip()
        if not block.startswith("## "):
            continue
        head, _, body = block.partition("\n")
        title = head[3:].strip()
        if title:
            entries.append({"id": "", "title": title, "text": body.strip(), "truncated": False})
    return entries


def format_context_entries(entries: list[dict]) -> str:
    """Entries back to the text ``--context`` form the chat prompt expects."""
    return _CONTEXT_SEPARATOR.join(
        f"## {e['title']}\n{e['text']}".strip() for e in entries if e["title"] or e["text"]
    )


# --- Heavy extraction (stays in Python) ---


async def _extract_url(url: str) -> str:
    """A web page's content for ingest: HTML as Markdown (tables and headings
    kept, see ``sites.kb_ingest.html_to_markdown``), any other text as is."""
    import httpx

    from pocketpaw_ee.sites.kb_ingest import html_to_markdown

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url)
    content_type = resp.headers.get("content-type", "").lower()
    body = resp.text
    if "html" in content_type or (not content_type and body.lstrip().startswith("<")):
        return html_to_markdown(body)
    return body


async def _extract_file(file_path: str) -> str:
    """Extract text via the configured extraction chain.

    Behaviour parity with the previous suffix-routed pypdf/python-docx/
    pytesseract helper is preserved by `LocalExtractor`, which is always
    available as the offline fallback. Chain config (`extraction_chain`,
    `extraction_per_mime`) lives on `Settings`.
    """
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.extraction import build_chain

    path = Path(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    mime = mime or "application/octet-stream"
    chain = build_chain(get_settings())
    result = await chain.run(path, mime)
    return result.text
