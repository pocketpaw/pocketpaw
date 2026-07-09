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
        # Text / data
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        # Code — Python
        "text/x-python",
        "text/x-python-script",
        # Code — JavaScript / TypeScript
        "text/javascript",
        "application/javascript",
        "text/typescript",
        "application/typescript",
        # Code — Web
        "text/html",
        "text/css",
        # Code — Go
        "text/x-go",
        # Code — Rust
        "text/x-rust",
        # Code — Java
        "text/x-java-source",
        # Code — C / C++
        "text/x-csrc",
        "text/x-c++src",
        "text/x-chdr",
        # Code — Shell
        "text/x-sh",
        "application/x-sh",
        # Code — Ruby
        "text/x-ruby",
        # Code — SQL
        "text/x-sql",
        "application/sql",
        # Config / data formats
        "text/yaml",
        "application/x-yaml",
        "text/xml",
        "application/xml",
        "application/toml",
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
    # Code — Python
    "text/x-python": ".py",
    "text/x-python-script": ".py",
    # Code — JavaScript / TypeScript
    "text/javascript": ".js",
    "application/javascript": ".js",
    "text/typescript": ".ts",
    "application/typescript": ".ts",
    # Code — Web
    "text/html": ".html",
    "text/css": ".css",
    # Code — Go
    "text/x-go": ".go",
    # Code — Rust
    "text/x-rust": ".rs",
    # Code — Java
    "text/x-java-source": ".java",
    # Code — C / C++
    "text/x-csrc": ".c",
    "text/x-c++src": ".cpp",
    "text/x-chdr": ".h",
    # Code — Shell
    "text/x-sh": ".sh",
    "application/x-sh": ".sh",
    # Code — Ruby
    "text/x-ruby": ".rb",
    # Code — SQL
    "text/x-sql": ".sql",
    "application/sql": ".sql",
    # Config / data formats
    "text/yaml": ".yaml",
    "application/x-yaml": ".yaml",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "application/toml": ".toml",
}


def extension_for(mime: str) -> str:
    """Map a canonical mime type to a file extension. Returns ``""`` if unknown."""
    return _MIME_TO_EXT.get(mime, "")
