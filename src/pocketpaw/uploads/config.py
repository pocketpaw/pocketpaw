"""Upload configuration — size limits, mime allowlist, storage root."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Mimes safe to render inline (images, pdf, plain text). Everything else gets
# Content-Disposition: attachment to avoid in-origin HTML/SVG tricks.
INLINE_MIMES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
    }
)

DEFAULT_ALLOWED_MIMES: frozenset[str] = frozenset(
    {
        # Images
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        # Documents
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
        # Text / code
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }
)


@dataclass
class UploadSettings:
    """Static configuration for the upload pipeline."""

    max_file_bytes: int = 25 * 1024 * 1024  # 25 MiB
    max_files_per_batch: int = 50
    allowed_mimes: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_MIMES)
    local_root: Path = field(default_factory=lambda: Path.home() / ".pocketpaw" / "uploads")


_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
}


def extension_for(mime: str) -> str:
    """Map a canonical mime type to a file extension. Returns ``""`` if unknown."""
    return _MIME_TO_EXT.get(mime, "")
