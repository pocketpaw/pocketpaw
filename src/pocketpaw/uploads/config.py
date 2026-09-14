"""Upload configuration — size limits, mime policy, storage root.

2026-09-14 (feat/uploads-multipart-adapter): added ``max_large_file_bytes``
(5 GiB), ``multipart_part_bytes`` (8 MiB), ``multipart_ttl_hours`` (168), and
the ``part_size_for``/``part_count_for`` sizing math.

``max_file_bytes`` stays at 25 MiB: ``security/body_limit.py`` derives the
global ASGI request ceiling from it, and multipart bytes never arrive as one
request body. Separate number on purpose.

2026-09-11 — ``allowed_mimes`` defaults to ``*/*`` instead of the ~35-type
``DEFAULT_ALLOWED_MIMES``, which refused .blend/.psd/.fbx and everything else
nobody listed. Narrow it per deploy with ``POCKETPAW_UPLOAD_ALLOWED_MIMES``.

Safe because the type list never was the control that kept an uploaded .html
off our origin — ``INLINE_MIMES`` is, and every download rail serves anything
outside it as ``Content-Disposition: attachment``. That set is unchanged.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Accepts every type. Also valid as a family form (``image/*``).
ANY_MIME = "*/*"
ALLOW_ANY_MIMES: frozenset[str] = frozenset({ANY_MIME})

#: Comma-separated policy, e.g. ``image/*,application/pdf``.
ALLOWED_MIMES_ENV = "POCKETPAW_UPLOAD_ALLOWED_MIMES"

#: Per-file ceiling. ``security/body_limit.py`` derives the ASGI request
#: ceiling from this x ``max_files_per_batch``, so raising it raises that too.
MAX_BYTES_ENV = "POCKETPAW_UPLOAD_MAX_BYTES"

_DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB

_MIB = 1024 * 1024

#: Per-file ceiling for the MULTIPART path (not ``MAX_BYTES_ENV``).
MAX_LARGE_FILE_BYTES_ENV = "POCKETPAW_MAX_LARGE_FILE_BYTES"

#: Baseline part size, scaled up by :func:`part_size_for` when needed.
MULTIPART_PART_BYTES_ENV = "POCKETPAW_MULTIPART_PART_BYTES"

#: How long a multipart session stays resumable.
MULTIPART_TTL_HOURS_ENV = "POCKETPAW_MULTIPART_TTL_HOURS"

_DEFAULT_MAX_LARGE_FILE_BYTES = 5 * 1024 * _MIB  # 5 GiB
_DEFAULT_MULTIPART_PART_BYTES = 8 * _MIB  # 8 MiB
_DEFAULT_MULTIPART_TTL_HOURS = 168  # 7 days

#: S3's hard ceiling on parts per upload. Enforced at complete time — i.e.
#: after every byte has moved — so the sizing has to get it right up front.
MAX_MULTIPART_PARTS = 10_000

#: S3's minimum for every part except the last.
MIN_MULTIPART_PART_BYTES = 5 * _MIB

# What a client sends when it does not recognise the file. Means "ask the
# filename instead", not "this is a binary blob".
_GENERIC_MIMES: frozenset[str] = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
)

# Mimes safe to render inline (images, pdf, plain text). Everything else gets
# Content-Disposition: attachment to avoid in-origin HTML/SVG tricks. This is
# the security boundary the wildcard type policy leans on — it stays short.
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

# The curated browser-native set. No longer the default policy (see the module
# docstring) — kept because it still describes "types a browser names correctly
# and we render first-class", and because callers outside the upload gate read
# it. Set ``POCKETPAW_UPLOAD_ALLOWED_MIMES`` to restore it as a gate.
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


def normalize_mime(raw: str | None) -> str:
    """Strip transport noise off a content type: parameters, case, whitespace.

    ``"TEXT/Plain; charset=utf-8"`` and ``"text/plain"`` are the same type, and
    an allowlist that treats them differently rejects real uploads for a reason
    no user can see.
    """
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def is_generic_mime(mime: str | None) -> bool:
    """True when the type says nothing — callers should ask the filename."""
    return normalize_mime(mime) in _GENERIC_MIMES


def mime_allowed(mime: str | None, allowed: frozenset[str]) -> bool:
    """Whether ``mime`` passes ``allowed``: ``*/*``, exact, or ``type/*``.

    An unnamed type passes only under ``*/*`` — under a narrowed policy,
    "I don't know what this is" is not a reason to let it through.
    """
    if ANY_MIME in allowed:
        return True
    norm = normalize_mime(mime)
    if not norm:
        return False
    if norm in allowed:
        return True
    family = norm.split("/", 1)[0]
    return f"{family}/*" in allowed


def allowed_mimes_from_env() -> frozenset[str]:
    """The type policy from the environment.

    Unset or unparseable yields ``*/*``: a typo must not lock every upload out
    of the product. Same fail-to-the-default rule as ``body_limit``.
    """
    raw = os.environ.get(ALLOWED_MIMES_ENV, "").strip()
    if not raw:
        return ALLOW_ANY_MIMES
    entries = {normalize_mime(part) for part in raw.split(",")}
    entries.discard("")
    if not entries:
        logger.warning("%s=%r parsed to nothing; accepting any type", ALLOWED_MIMES_ENV, raw)
        return ALLOW_ANY_MIMES
    return frozenset(entries)


def _positive_int_from_env(name: str, default: int) -> int:
    """Read a positive int from ``name``. Malformed or non-positive warns and
    falls back — a typo must not read as "unlimited"."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; using the default", name, raw)
        return default
    if val <= 0:
        logger.warning("%s=%d is not positive; using the default", name, val)
        return default
    return val


def _max_file_bytes_from_env() -> int:
    """Per-file ceiling in bytes. Malformed or non-positive falls back."""
    return _positive_int_from_env(MAX_BYTES_ENV, _DEFAULT_MAX_FILE_BYTES)


def _max_large_file_bytes_from_env() -> int:
    """Per-file ceiling for the multipart path. Malformed or non-positive falls back."""
    return _positive_int_from_env(MAX_LARGE_FILE_BYTES_ENV, _DEFAULT_MAX_LARGE_FILE_BYTES)


def _multipart_part_bytes_from_env() -> int:
    """Baseline part size. Malformed or non-positive falls back."""
    return _positive_int_from_env(MULTIPART_PART_BYTES_ENV, _DEFAULT_MULTIPART_PART_BYTES)


def _multipart_ttl_hours_from_env() -> int:
    """Multipart session lifetime in hours. Malformed or non-positive falls back."""
    return _positive_int_from_env(MULTIPART_TTL_HOURS_ENV, _DEFAULT_MULTIPART_TTL_HOURS)


def part_size_for(size: int, *, base: int | None = None) -> int:
    """``max(base, ceil(size / 10000))``, rounded up to a whole MiB.

    The part grows only when the baseline would blow the 10000-part ceiling.
    Rounding up is safe: a bigger part means fewer parts, never more.

    ``base`` defaults to ``POCKETPAW_MULTIPART_PART_BYTES``; pass
    ``settings.multipart_part_bytes`` from a request path to avoid re-reading
    the environment per call.
    """
    if base is None:
        base = _multipart_part_bytes_from_env()
    needed = -(-max(int(size), 0) // MAX_MULTIPART_PARTS)  # ceil division
    raw = max(int(base), needed)
    return -(-raw // _MIB) * _MIB


def validate_part_number(part_number: object) -> int:
    """Return ``part_number`` as an int in 1..10000, or raise ``InvalidPart``.

    ``bool`` is rejected explicitly because it passes ``isinstance(int)``, and
    ``True`` would silently address part 1.
    """
    from pocketpaw.uploads.errors import InvalidPart

    if isinstance(part_number, bool) or not isinstance(part_number, int):
        raise InvalidPart(f"part number must be an int, got {type(part_number).__name__}")
    if not 1 <= part_number <= MAX_MULTIPART_PARTS:
        raise InvalidPart(f"part number {part_number} outside 1..{MAX_MULTIPART_PARTS}")
    return part_number


def part_count_for(size: int, part_size: int) -> int:
    """How many parts a file of ``size`` splits into at ``part_size``.

    A zero-byte file is one empty part, not zero — otherwise a caller
    iterating the count would complete having sent nothing.
    """
    if part_size <= 0:
        raise ValueError("part_size must be positive")
    return max(1, -(-max(int(size), 0) // int(part_size)))


@dataclass
class UploadSettings:
    """Static configuration for the upload pipeline."""

    max_file_bytes: int = field(default_factory=_max_file_bytes_from_env)
    max_files_per_batch: int = 50
    allowed_mimes: frozenset[str] = field(default_factory=allowed_mimes_from_env)
    local_root: Path = field(default_factory=lambda: Path.home() / ".pocketpaw" / "uploads")
    # Multipart path only — body_limit derives the global request ceiling from
    # max_file_bytes and must not learn about 5 GiB.
    max_large_file_bytes: int = field(default_factory=_max_large_file_bytes_from_env)
    multipart_part_bytes: int = field(default_factory=_multipart_part_bytes_from_env)
    multipart_ttl_hours: int = field(default_factory=_multipart_ttl_hours_from_env)


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


# Extensions the stdlib ``mimetypes`` registry does not know. Not a gate — the
# policy accepts unknown types regardless; this only decides what a file is
# RECORDED as, so extending it is always safe.
_EXT_TO_MIME_EXTRAS: dict[str, str] = {
    # Creative suites
    ".blend": "application/x-blender",
    ".blend1": "application/x-blender",
    ".blend2": "application/x-blender",
    ".psd": "image/vnd.adobe.photoshop",
    ".psb": "image/vnd.adobe.photoshop",
    ".ai": "application/vnd.adobe.illustrator",
    ".xd": "application/vnd.adobe.xd",
    ".sketch": "application/x-sketch",
    ".fig": "application/x-figma",
    ".afdesign": "application/x-affinity-designer",
    ".afphoto": "application/x-affinity-photo",
    ".procreate": "application/x-procreate",
    ".kra": "application/x-krita",
    ".xcf": "image/x-xcf",
    ".aep": "application/x-after-effects",
    ".prproj": "application/x-premiere-project",
    # 3D / CAD interchange
    ".fbx": "model/fbx",
    ".obj": "model/obj",
    ".stl": "model/stl",
    ".dae": "model/vnd.collada+xml",
    ".3ds": "application/x-3ds",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".usd": "model/vnd.usd",
    ".usda": "model/vnd.usda",
    ".usdc": "model/vnd.usdc",
    ".usdz": "model/vnd.usdz+zip",
    ".step": "model/step",
    ".stp": "model/step",
    ".iges": "model/iges",
    ".igs": "model/iges",
    ".ifc": "application/x-step",
    ".dwg": "image/vnd.dwg",
    ".dxf": "image/vnd.dxf",
    ".skp": "application/vnd.sketchup.skp",
    # Archives and data
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".zst": "application/zstd",
    ".parquet": "application/vnd.apache.parquet",
    ".sqlite": "application/vnd.sqlite3",
    ".db": "application/vnd.sqlite3",
    ".ipynb": "application/x-ipynb+json",
    ".epub": "application/epub+zip",
    # Media containers the registry misses on some platforms
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
}


def mime_for_filename(filename: str | None) -> str:
    """Content type from a filename. ``""`` when there is no clue."""
    if not filename:
        return ""
    suffix = Path(filename).suffix.lower()
    if not suffix:
        return ""
    extra = _EXT_TO_MIME_EXTRAS.get(suffix)
    if extra:
        return extra
    guessed, _ = mimetypes.guess_type(filename)
    norm = normalize_mime(guessed)
    return "" if norm in _GENERIC_MIMES else norm


def extension_for(mime: str, filename: str | None = None) -> str:
    """A storage-key extension for this upload. ``""`` when nothing is known.

    Our map first (the mime may come from magic bytes that disagree with the
    filename), then the filename's suffix — the only source for a format nobody
    registered — then the stdlib registry, skipped for generic types because it
    answers ``application/octet-stream`` with ``.bin`` and would mask the name.
    """
    norm = normalize_mime(mime)
    mapped = _MIME_TO_EXT.get(norm, "")
    if mapped:
        return mapped
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix.lower()
    if norm and norm not in _GENERIC_MIMES:
        return mimetypes.guess_extension(norm) or ""
    return ""
