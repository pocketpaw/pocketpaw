# ee/pocketpaw_ee/paw_bar/knowledge_sources.py — reading an owner's knowledge
# source (an uploaded file or one web page) into text the concierge can quote.
#
# Created: 2026-09-28 (feat/concierge-knowledge-sources, CR-9). The machinery
# behind ``knowledge_routes``' …/knowledge/sources routes, kept out of the route
# module so each half reads on its own:
#
#   * ``sniff_upload`` decides a file's type from its BYTES and requires the
#     extension to agree. It never consults the client's Content-Type:
#     ``uploads.service._sniff_mime`` falls back to that claim, so it is not reused.
#     A DOCX's inflated size is summed from the zip directory and refused past a
#     ceiling before python-docx is handed it (a small zip can inflate to GBs).
#   * ``extract_file_text`` runs the existing ``extraction.local.LocalExtractor``
#     (pypdf, python-docx) in a thread; Markdown and text are decoded directly.
#     ``missing_parser`` tells a parser that is not installed (a deployment
#     fault) from a file the parser cannot read.
#   * ``fetch_link_text`` fetches through ``pocketpaw.security.safe_fetch`` — DNS
#     pinned, every redirect hop re-checked for a public address, body capped — and
#     turns HTML into text with ``sites.kb_ingest.html_to_text``, the same helper
#     the page sync feeds the concierge with. No second fetcher is written here.
#
# Every refusal is a ``SourceRefused`` carrying the status code the row (or the
# HTTP detail) reports: ``too_large``, ``unsupported``, ``blocked``, or ``failed``
# with a ``reason``. Content is untrusted data throughout: it is only ever
# extracted and handed to the kb engine, never executed or rendered.

from __future__ import annotations

import asyncio
import io
import tempfile
import zipfile
from pathlib import Path

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The extensions an owner may upload, and the MIME each one must sniff as. The
# order is what the list route reports as ``accepted_types``.
ACCEPTED_TYPES: dict[str, str] = {
    ".pdf": PDF_MIME,
    ".docx": DOCX_MIME,
    ".md": "text/markdown",
    ".txt": "text/plain",
}
_TEXT_EXTS = (".md", ".txt")

# A DOCX may inflate to at most this many times the upload cap. Word text
# compresses about 5-10x; embedded media barely compresses at all.
INFLATE_RATIO = 20

# What a link may serve. safe_get_streamed decodes the body as text, so a binary
# type (a PDF link) cannot be carried and is reported as unsupported.
_LINK_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")
_LINK_TIMEOUT_S = 20.0


class SourceRefused(Exception):
    """A source that cannot become knowledge. ``status`` is the row status (and the
    HTTP detail when refused synchronously); ``reason`` qualifies ``failed``."""

    def __init__(self, status: str, reason: str = "") -> None:
        super().__init__(f"{status}:{reason}" if reason else status)
        self.status = status
        self.reason = reason


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True


def _docx_inflated_size(data: bytes) -> int | None:
    """Total uncompressed size of a zip that is a Word document, else None."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    if "word/document.xml" not in {info.filename for info in infos}:
        return None
    return sum(info.file_size for info in infos)


def sniff_upload(data: bytes, filename: str, *, max_bytes: int) -> str:
    """The upload's MIME, decided by its bytes. Raises ``SourceRefused``.

    The extension must be one of ``ACCEPTED_TYPES`` AND agree with the bytes: a PNG
    named ``.pdf`` or a PDF named ``.txt`` is ``unsupported``. A Word zip whose
    members inflate past ``INFLATE_RATIO`` × ``max_bytes`` is ``too_large``.
    """
    ext = Path(filename or "").suffix.lower()
    claimed = ACCEPTED_TYPES.get(ext)
    if claimed is None:
        raise SourceRefused("unsupported")

    if data.startswith(b"%PDF-"):
        actual = PDF_MIME
    elif data.startswith(b"PK\x03\x04"):
        inflated = _docx_inflated_size(data)
        if inflated is None:
            raise SourceRefused("unsupported")
        if inflated > max_bytes * INFLATE_RATIO:
            raise SourceRefused("too_large")
        actual = DOCX_MIME
    elif _is_text(data):
        if ext not in _TEXT_EXTS:
            raise SourceRefused("unsupported")
        return claimed
    else:
        raise SourceRefused("unsupported")

    if actual != claimed:
        raise SourceRefused("unsupported")
    return actual


def _extract_with_local(data: bytes, ext: str, mime: str) -> str:
    """pypdf / python-docx through the existing LocalExtractor. It routes by file
    suffix, so the temp file carries the real one. A directory rather than a
    NamedTemporaryFile: on Windows an open temp file cannot be reopened by name."""
    from pocketpaw_ee.cloud.extraction.local import LocalExtractor

    with tempfile.TemporaryDirectory(prefix="pawbar-source-") as tmp:
        path = Path(tmp) / f"source{ext}"
        path.write_bytes(data)
        result = asyncio.run(LocalExtractor().extract(path, mime))
    return result.text or ""


def missing_parser(exc: BaseException | None) -> bool:
    """True when ``exc`` (or anything in its cause chain) says a parser library is
    not installed. LocalExtractor turns that ``ImportError`` into a RuntimeError
    reading "pypdf not installed"; either form is a deployment fault, not a bad
    file, and the caller logs it as an error."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ImportError) or "not installed" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def extract_file_text(data: bytes, ext: str, mime: str) -> str:
    """The text of an upload already accepted by ``sniff_upload``. A file the
    parser cannot read raises ``SourceRefused("failed", "unreadable")`` chained
    to the parser's exception (see ``missing_parser``)."""
    if mime not in (PDF_MIME, DOCX_MIME):
        return data.decode("utf-8-sig", errors="replace")
    try:
        # The parsers are synchronous and CPU-bound; keep them off the event loop.
        return await asyncio.to_thread(_extract_with_local, data, ext, mime)
    except Exception as exc:  # noqa: BLE001 — a corrupt file is the owner's, not a crash
        raise SourceRefused("failed", "unreadable") from exc


async def check_link(url: str) -> None:
    """Refuse a link whose address is not a public http(s) host, before any row is
    written. DNS failure is not a refusal here: the fetch reports it."""
    from pocketpaw.security.safe_fetch import (
        BlockedURLError,
        FetchFailedError,
        UnsupportedSchemeError,
        assert_public_url,
    )

    try:
        await assert_public_url(url)
    except (UnsupportedSchemeError, BlockedURLError) as exc:
        raise SourceRefused("blocked") from exc
    except FetchFailedError:
        return
    except Exception as exc:  # noqa: BLE001 — an unparseable URL is not fetchable
        raise SourceRefused("blocked") from exc


async def fetch_link_text(url: str, *, max_bytes: int) -> tuple[str, str]:
    """``(text, mime)`` of one web page, fetched once through the SSRF-safe fetcher.

    Mapping of the fetcher's errors: a non-public address on any hop (including a
    redirect target) or a non-http scheme → ``blocked``; a content type we cannot
    read → ``unsupported``; a body past ``max_bytes`` → ``too_large``; DNS failure,
    a network error or a non-2xx answer → ``failed``/``unreachable``.
    ``FetchFailedError`` covers both DNS failure and a refused content type, so the
    two are told apart by its message — the one place this reads prose.
    """
    from pocketpaw.security.safe_fetch import (
        BlockedURLError,
        FetchFailedError,
        UnsupportedSchemeError,
        safe_get_streamed,
    )
    from pocketpaw_ee.sites.foreign_grounding import GROUNDING_USER_AGENT
    from pocketpaw_ee.sites.kb_ingest import html_to_text

    try:
        result = await safe_get_streamed(
            url,
            max_bytes=max_bytes + 1,
            timeout=_LINK_TIMEOUT_S,
            allowed_content_types=_LINK_TYPES,
            headers={"User-Agent": GROUNDING_USER_AGENT},
        )
    except (UnsupportedSchemeError, BlockedURLError) as exc:
        raise SourceRefused("blocked") from exc
    except FetchFailedError as exc:
        if "content-type" in str(exc):
            raise SourceRefused("unsupported") from exc
        raise SourceRefused("failed", "unreachable") from exc
    except Exception as exc:  # noqa: BLE001 — timeouts, resets, TLS: all "unreachable"
        raise SourceRefused("failed", "unreachable") from exc

    if not 200 <= result.status_code < 300:
        raise SourceRefused("failed", "unreachable")
    if result.truncated:
        raise SourceRefused("too_large")
    mime = result.content_type.split(";", 1)[0].strip().lower()
    if mime in ("text/html", "application/xhtml+xml"):
        return html_to_text(result.text), mime
    return result.text, mime


__all__ = [
    "ACCEPTED_TYPES",
    "SourceRefused",
    "check_link",
    "extract_file_text",
    "fetch_link_text",
    "sniff_upload",
]
