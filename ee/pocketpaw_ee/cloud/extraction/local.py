# local.py — Local extraction adapter (pypdf + python-docx + pytesseract).
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Wraps the existing _extract_file logic from agents/knowledge.py so
# self-hosted and offline deployments keep a no-network extraction path.
"""LocalExtractor — wraps the traditional extraction libraries.

Behavior must match the previous `_extract_file` in `agents/knowledge.py`
(line-for-line port). MIME routing here is suffix-based to mirror the
original helper, with the public `supports_mimes = {"*"}` so the chain
treats Local as a catch-all fallback.
"""

from __future__ import annotations

from pathlib import Path

from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult


class LocalExtractor:
    """Behavior-preserving wrapper around pypdf / python-docx / pytesseract."""

    name = "local"
    supports_mimes = {"*"}
    requires_network = False

    async def extract(self, path: Path, mime: str) -> ExtractionResult:
        text = await _extract_text(path, mime)
        return ExtractionResult(
            text=text,
            metadata={"path": str(path), "mime": mime},
            backend=self.name,
        )


async def _extract_text(path: Path, mime: str = "") -> str:
    """Extract text from PDF, DOCX, image, VTT, or fall back to raw read.

    Routing precedence: explicit ``mime`` first, then file suffix. Mime
    wins because callers sometimes hand us a temp file whose extension
    doesn't match the source (e.g. a transcript stored under storage_key
    ``planner-XXXX.txt`` is really WebVTT — only the mime tells us to
    strip cue tags). Suffix is the fallback for legacy callers and for
    files where mime isn't reliably set (uploaded blobs).
    """
    file_path = str(path)
    suffix = path.suffix.lower()
    norm_mime = (mime or "").split(";", 1)[0].strip().lower()

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError as exc:
            raise RuntimeError("pypdf not installed — run: pip install pypdf") from exc

    if suffix in (".docx", ".doc"):
        try:
            from docx import Document

            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError as exc:
            raise RuntimeError("python-docx not installed — run: pip install python-docx") from exc

    if suffix in (".png", ".jpg", ".jpeg"):
        try:
            import pytesseract
            from PIL import Image

            return pytesseract.image_to_string(Image.open(file_path))
        except ImportError as exc:
            raise RuntimeError(
                "pytesseract not installed — run: pip install pytesseract Pillow"
            ) from exc

    if suffix == ".vtt" or norm_mime == "text/vtt":
        return _vtt_to_plain(path.read_text(encoding="utf-8", errors="replace"))

    return path.read_text(encoding="utf-8", errors="replace")


def _vtt_to_plain(vtt: str) -> str:
    """Strip a WebVTT blob down to readable speech for KB indexing.

    Keeps speaker-prefixed lines (``Speaker: text``) and drops the
    ``WEBVTT`` header, ``NOTE`` blocks, cue identifiers, and
    ``00:00:01.234 --> 00:00:05.678`` timestamp lines. Cue tags
    (``<v Speaker>...</v>``) are unwrapped into ``Speaker: ...``.
    Adjacent same-speaker turns are collapsed.

    The raw VTT remains the on-disk artifact for download; only the KB
    extraction sees the cleaned text. Embeddings + keyword search then
    score against speech instead of timestamps + markup noise.
    """
    import re

    cue_re = re.compile(r"<v\s+([^>]+)>([\s\S]*?)</v>", re.MULTILINE)
    timestamp_re = re.compile(r"^\s*\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*")

    lines: list[str] = []
    last_speaker: str | None = None
    in_note = False
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            in_note = False
            continue
        if line == "WEBVTT" or line.startswith("WEBVTT "):
            continue
        if line.startswith("NOTE"):
            in_note = True
            continue
        if in_note:
            continue
        if timestamp_re.match(line):
            continue

        m = cue_re.search(line)
        if m:
            speaker = m.group(1).strip()
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not text:
                continue
            if speaker == last_speaker and lines:
                lines[-1] = f"{lines[-1]} {text}"
            else:
                lines.append(f"{speaker}: {text}")
                last_speaker = speaker
            continue

        # Plain line (no cue tag) — likely a single-speaker VTT. Keep it,
        # but drop bare cue identifiers (a single integer or short slug
        # on its own line, which VTT uses to label cues).
        if line.isdigit() or (len(line) <= 32 and "-" in line and " " not in line):
            continue
        lines.append(line)
        last_speaker = None

    return "\n".join(lines).strip()
