# knowledge.py — Agent knowledge service via the kb-go binary.
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
from pathlib import Path

logger = logging.getLogger(__name__)


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
    if result.returncode != 0:
        logger.warning("kb failed (exit %d): %s", result.returncode, result.stderr[:200])
        raise RuntimeError(f"kb failed: {result.stderr[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


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
        """
        return await asyncio.to_thread(
            _kb, "ingest", "--scope", scope, "--source", source, input_text=text
        )

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
        # Text/code files go directly to kb (without intermediate extraction)
        return await asyncio.to_thread(
            _kb, "ingest", file_path, "--scope", f"agent:{agent_id}", "--source", label
        )

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
    async def search_context(agent_id: str, query: str, limit: int = 3) -> str:
        """Get formatted knowledge context for agent prompt injection."""
        return await KnowledgeService.search_context_for_scope(
            scope=f"agent:{agent_id}",
            query=query,
            limit=limit,
        )

    @staticmethod
    async def search_context_for_scope(scope: str, query: str, limit: int = 3) -> str:
        """Get formatted knowledge context for any kb-go scope.

        Runs ``_kb`` in a thread so the event loop isn't blocked by the
        subprocess call. See S2 in the code review for context.
        """
        result = await asyncio.to_thread(
            _kb,
            "search",
            query,
            "--scope",
            scope,
            "--limit",
            str(limit),
            "--context",
        )
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
    from ee.cloud.extraction import build_chain
    from pocketpaw.config import get_settings

    path = Path(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    mime = mime or "application/octet-stream"
    chain = build_chain(get_settings())
    result = await chain.run(path, mime)
    return result.text
