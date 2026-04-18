# knowledge.py — Agent knowledge service via the kb-go binary.
# Updated: 2026-04-07 — Switched from Python knowledge_base package to kb Go binary.
# Heavy extraction (PDF, OCR, URL) done in Python, piped as text to kb.
# All other operations delegate to subprocess calls.
"""Agent knowledge service — thin wrapper over the `kb` Go binary.

The kb binary (github.com/qbtrix/kb-go) handles compilation, search, indexing,
and storage. This wrapper handles heavy extraction (PDF, URL, OCR, DOCX) in
Python and pipes extracted text to kb via stdin.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

KB_BIN = os.environ.get("POCKETPAW_KB_BIN", "kb")


def _kb(*args: str, input_text: str | None = None, timeout: int = 120) -> dict | list | str:
    """Call kb binary, return parsed JSON or raw text."""
    cmd = [KB_BIN, *args, "--json"]
    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"kb binary not found at '{KB_BIN}'. "
            "Install: go install github.com/qbtrix/kb-go@latest "
            "or set POCKETPAW_KB_BIN to the binary path."
        )
    if result.returncode != 0:
        logger.warning("kb failed (exit %d): %s", result.returncode, result.stderr[:200])
        raise RuntimeError(f"kb failed: {result.stderr[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


class KnowledgeService:
    """Agent-scoped knowledge operations via the kb Go binary."""

    @staticmethod
    async def ingest_text(agent_id: str, text: str, source: str = "manual") -> dict:
        return _kb("ingest", "--scope", f"agent:{agent_id}", "--source", source, input_text=text)

    @staticmethod
    async def ingest_url(agent_id: str, url: str) -> dict:
        """Fetch URL with trafilatura (Python), pipe text to kb."""
        try:
            text = await _extract_url(url)
            return _kb(
                "ingest",
                "--scope",
                f"agent:{agent_id}",
                "--source",
                url,
                input_text=text,
            )
        except Exception as exc:
            return {"error": str(exc), "url": url}

    @staticmethod
    async def ingest_file(agent_id: str, file_path: str) -> dict:
        """Extract file content (PDF/DOCX via Python if needed), pipe to kb."""
        try:
            path = Path(file_path)
            if path.suffix in (".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"):
                text = await _extract_file(file_path)
                return _kb(
                    "ingest",
                    "--scope",
                    f"agent:{agent_id}",
                    "--source",
                    file_path,
                    input_text=text,
                )
            # Text/code files go directly to kb
            return _kb("ingest", file_path, "--scope", f"agent:{agent_id}")
        except Exception as exc:
            return {"error": str(exc)}

    @staticmethod
    async def search(agent_id: str, query: str, limit: int = 5) -> list[str]:
        results = _kb(
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
        result = _kb(
            "search",
            query,
            "--scope",
            f"agent:{agent_id}",
            "--limit",
            str(limit),
            "--context",
        )
        return result if isinstance(result, str) else ""

    @staticmethod
    async def clear(agent_id: str) -> dict:
        return _kb("clear", "--scope", f"agent:{agent_id}")

    @staticmethod
    def stats(agent_id: str) -> dict:
        return _kb("stats", "--scope", f"agent:{agent_id}")

    @staticmethod
    async def lint(agent_id: str) -> list[dict]:
        return _kb("lint", "--scope", f"agent:{agent_id}")


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
    """Extract text from PDF, DOCX, or image files."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError:
            raise RuntimeError("pypdf not installed — run: pip install pypdf")

    if suffix in (".docx", ".doc"):
        try:
            from docx import Document

            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            raise RuntimeError("python-docx not installed — run: pip install python-docx")

    if suffix in (".png", ".jpg", ".jpeg"):
        try:
            import pytesseract
            from PIL import Image

            return pytesseract.image_to_string(Image.open(file_path))
        except ImportError:
            raise RuntimeError("pytesseract not installed — run: pip install pytesseract Pillow")

    # Fallback: read as text
    return path.read_text(encoding="utf-8", errors="replace")
