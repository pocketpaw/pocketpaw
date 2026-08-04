# knowledge.py — Agent knowledge service via the kb-go binary.
# Updated: 2026-08-04 — Ingest hardening (silent-poisoning fix). On boxes with
#   no ANTHROPIC_API_KEY (the Claude Code agent backend deployment), kb's own
#   LLM compile used to fail and kb silently stored every doc VERBATIM — a
#   54-article / 4M-word scope that every chat turn then paid to search.
#   Three changes: (1) ingest_text_to_scope now compiles the article with
#   PocketPaw's own agent backend (PocketPawCompilerBackend) when the key is
#   absent and pipes the pre-compiled article to `kb ingest --article-json`;
#   compile failure RAISES — never a verbatim fallback. (2) Any ingest result
#   with compiled_with == "none (fallback)" is rejected loudly (defense in
#   depth against older binaries / --allow-fallback misuse). (3) The chat-turn
#   search path (search_context_for_scope) got a hard 5s timeout and fails
#   soft (returns "") so a slow KB can never stall a chat turn; _kb translates
#   subprocess timeouts into clear RuntimeErrors. ingest_file's text-file path
#   now routes through ingest_text_to_scope so it gets the same guarantees.
#   Requires the kb-go binary with --article-json support; an older binary
#   errors loudly with an upgrade hint instead of poisoning the scope.
# Updated: 2026-07-30 — Paw Bar reply sources. Added the scope-form read pair
#   search_articles_for_scope / list_articles_for_scope (raw {id, title, summary}
#   hit dicts, mirroring ingest_text_to_scope's "caller owns the scope shape"
#   contract) so the public concierge router can attribute a reply to the synced
#   site pages and list them, without importing this module's private ``_kb``.
# Updated: 2026-07-03 — FL-11b "hide-from-AI purge". Added
#   KnowledgeService.remove_article(scope, article_id) → `kb delete
#   <article_id> --scope <scope>`, mirroring get_article. Resilient: logs and
#   swallows subprocess errors (returns False) like the other kb calls, so a
#   purge failure never propagates back into the PATCH handler that hides a
#   file. kb-go's `delete` is idempotent (deleting a missing id is a no-op).
# Updated: 2026-04-30 — Stage 1.B of "Files as Knowledge". Added
#   ingest_text_to_scope so callers (notably the FileReady listener) can
#   target arbitrary kb-go scopes (workspace:{wid}, pocket:{pid}) without
#   shoehorning everything through agent:{aid}. Existing ingest_text and
#   ingest_file are now thin wrappers over the new entry point.
# Updated: 2026-04-30 — File extraction routed through the pluggable
#   ee/cloud/extraction chain (LocalExtractor preserves the previous pypdf
#   / python-docx / pytesseract behaviour; cloud adapters slot in via
#   POCKETPAW_EXTRACTION_CHAIN). Stage 1.A of "Files as Knowledge".
# Updated: 2026-04-07 — Switched from Python knowledge_base package to kb Go binary.
# Heavy extraction (PDF, OCR, URL) done in Python, piped as text to kb.
# All other operations delegate to subprocess calls.
"""Agent knowledge service — thin wrapper over the `kb` Go binary.

The kb binary (github.com/qbtrix/kb-go) handles compilation, search, indexing,
and storage. URL extraction stays inline (trafilatura). File extraction is
routed through `ee.cloud.extraction.build_chain` so cloud captioning can be
configured without touching this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import time
from pathlib import Path

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

# Above this size, a compiled article must be meaningfully shorter than the
# text the compiler saw, or we treat it as a verbatim echo and reject it.
_LARGE_DOC_CHARS = 4_000
_MAX_COMPILED_RATIO = 0.6

# kb-go's marker for "compile failed, stored verbatim". We never accept it.
_FALLBACK_COMPILED_WITH = "none (fallback)"


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
        raise RuntimeError(
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


def _check_ingest_result(result: dict | list | str, scope: str) -> dict | list | str:
    """Reject verbatim-fallback articles (defense in depth).

    kb-go marks an article it stored WITHOUT LLM compilation as
    ``compiled_with == "none (fallback)"`` (older binaries did this silently
    on compile failure; newer ones only with ``--allow-fallback``). A
    verbatim article poisons the scope — search pays O(raw corpus) on every
    chat turn for junk snippets — so any ingest that produced one is treated
    as a failure, never a success.
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
    return result


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


def _validate_compiled_article(article: dict, *, compile_input_len: int, source: str) -> dict:
    """Normalize + sanity-check a backend-compiled article.

    Raises ``ValueError`` on garbage: empty title/content, or (for large
    docs) content that isn't meaningfully shorter than what the compiler
    saw — that's a verbatim echo, exactly the poisoning we're preventing.
    """
    title = str(article.get("title") or "").strip()
    content = str(article.get("content") or "").strip()
    if not title or not content:
        raise ValueError("compiled article is missing a title or content")
    compiled_cap = compile_input_len * _MAX_COMPILED_RATIO
    if compile_input_len > _LARGE_DOC_CHARS and len(content) > compiled_cap:
        raise ValueError(
            f"compiled article is not a compression: content is {len(content)} chars "
            f"against a {compile_input_len}-char input (limit "
            f"{_MAX_COMPILED_RATIO:.0%}) — looks like a verbatim echo"
        )
    summary = str(article.get("summary") or "").strip()
    concepts = [str(c).strip() for c in article.get("concepts") or [] if str(c).strip()]
    categories = [str(c).strip() for c in article.get("categories") or [] if str(c).strip()]
    return {
        "title": title,
        "summary": summary,
        "content": content,
        "concepts": concepts,
        "categories": categories,
        "source": source,
    }


async def _compile_article_with_agent(text: str, source: str) -> dict:
    """Compile ``text`` into a kb article using PocketPaw's own agent backend.

    This is the no-ANTHROPIC_API_KEY path: kb's internal LLM compile cannot
    run, so we produce the article with the same backend infrastructure the
    chat runtime uses (``PocketPawCompilerBackend`` → agent registry → the
    active backend, e.g. the Claude Code SDK backend which authenticates via
    the CLI, not the API key). Raises ``RuntimeError`` on any failure —
    callers must NEVER fall back to verbatim ingestion.
    """
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.kb.backend_adapter import PocketPawCompilerBackend

    excerpt = text[:_COMPILE_INPUT_CAP_CHARS]
    truncated = len(text) > len(excerpt)
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
        "- concepts: 3-10 key concepts.\n"
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
            _parse_article_json(raw), compile_input_len=len(excerpt), source=source
        )
    except ValueError as exc:
        raise RuntimeError(f"agent-backend article compile failed for {source!r}: {exc}")
    article["compiled_with"] = f"pocketpaw-agent:{get_settings().agent_backend}"
    return article


class KnowledgeService:
    """Knowledge operations via the kb Go binary.

    All ingest paths funnel through :meth:`ingest_text_to_scope` so the
    scope shape (``agent:{id}``, ``workspace:{id}``, ``pocket:{id}``) is
    decided by the caller, not by this class.
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
        if os.environ.get("ANTHROPIC_API_KEY"):
            result = await asyncio.to_thread(
                _kb, "ingest", "--scope", scope, "--source", source, input_text=text
            )
            return _check_ingest_result(result, scope)

        article = await _compile_article_with_agent(text, source)
        payload = json.dumps({"raw_text": text, "article": article})
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
            msg = str(exc)
            if "unknown flag" in msg or "flag provided but not defined" in msg:
                raise RuntimeError(
                    "kb binary does not support `ingest --article-json` — it predates "
                    "the pre-compiled-article contract. Deploy the paired kb-go build "
                    f"(binary: {KB_BIN}). Original error: {msg}"
                ) from exc
            raise
        return _check_ingest_result(result, scope)

    @staticmethod
    async def ingest_text(agent_id: str, text: str, source: str = "manual") -> dict:
        return await KnowledgeService.ingest_text_to_scope(f"agent:{agent_id}", text, source)

    @staticmethod
    async def ingest_url(agent_id: str, url: str) -> dict:
        """Fetch URL with trafilatura (Python), pipe text to kb."""
        try:
            text = await _extract_url(url)
            return await KnowledgeService.ingest_text_to_scope(f"agent:{agent_id}", text, url)
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
            return await KnowledgeService.ingest_text_to_scope(f"agent:{agent_id}", text, label)
        # Text/code files: read in Python and route through the common ingest
        # path so they get the same compile guarantees (agent-backend compile
        # without an API key, verbatim-fallback rejection) as every other doc.
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        return await KnowledgeService.ingest_text_to_scope(f"agent:{agent_id}", text, label)

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
    ) -> str:
        """Get formatted knowledge context for any kb-go scope.

        Runs ``_kb`` in a thread so the event loop isn't blocked by the
        subprocess call. See S2 in the code review for context.

        This is the chat-turn path (``_build_kb_snippets_block`` calls it on
        EVERY turn), so it is fail-soft with a hard timeout: on timeout or
        any subprocess failure it logs a warning and returns ``""`` — the
        caller simply skips the KB block. A slow or broken KB must never
        stall a chat turn.
        """
        start = time.monotonic()
        try:
            result = await asyncio.to_thread(
                _kb,
                "search",
                query,
                "--scope",
                scope,
                "--limit",
                str(limit),
                "--context",
                timeout=timeout,
            )
        except Exception:
            logger.warning(
                "kb search for chat context failed (scope=%s, elapsed=%.1fs, "
                "timeout=%ds); returning empty context",
                scope,
                time.monotonic() - start,
                timeout,
                exc_info=True,
            )
            return ""
        return result if isinstance(result, str) else ""

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


# --- Heavy extraction (stays in Python) ---


async def _extract_url(url: str) -> str:
    """Extract article text from URL using trafilatura."""
    try:
        import httpx
        import trafilatura

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url)
        return trafilatura.extract(resp.text) or resp.text[:5000]
    except ImportError:
        # Fallback: just fetch raw HTML
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url)
        return resp.text[:10000]


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
