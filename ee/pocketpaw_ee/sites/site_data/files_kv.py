# ee/pocketpaw_ee/sites/site_data/files_kv.py — the rules the Sites Data dashboard
# applies to a site's R2 files and KV keys, kept pure so each one is tested alone.
#
#   * File keys (``check_file_key``): 1-1024 UTF-8 bytes, no leading "/", no empty,
#     "." or ".." segment, no backslash, no control character. Anything else is a 422
#     ``sites.data_bad_file_key`` before any Cloudflare call. List prefixes are looser
#     (``check_prefix``: may be empty or end in "/").
#   * Content types: an upload is stored with the type the request declared, else the
#     one its extension implies, unless either is script-capable (HTML, SVG, XML,
#     JavaScript, Flash, multipart …) or the key's extension is: those are stored as
#     ``application/octet-stream``, so a bucket served publicly can't host a page or a
#     script (``stored_content_type``). A download answers the stored type only for
#     images, PDF and plain text with a harmless extension, else octet-stream
#     (``download_content_type``); the router always adds ``attachment``, ``nosniff``
#     and a sandboxing CSP. ``download_filename`` is the key's last segment, made
#     header-safe like the CSV export's name.
#   * KV keys (``check_kv_key``): 1-512 UTF-8 bytes, not "." or "..". A key named like
#     a credential (``project_data.is_credential``: password, secret, token, api key
#     …) is masked: its value reads ``MASK``, its metadata is hidden, and it can't be
#     written here (it can still be deleted). Key names themselves are never masked.
from __future__ import annotations

import mimetypes
import posixpath
import re
from urllib.parse import quote

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.project_data import MASK, is_credential

# Read at call time so tests can shrink them.
MAX_FILE_BYTES = 50 * 1024 * 1024  # one upload or move through the API (v1)
MAX_KV_EDIT_BYTES = 1024 * 1024  # a value the dashboard shows and edits
MAX_KV_METADATA_BYTES = 1024  # Cloudflare's own cap on a key's metadata (as JSON)
MIN_TTL = 60  # Cloudflare's minimum expiration_ttl
MAX_FILE_KEY_BYTES = 1024
MAX_KV_KEY_BYTES = 512

OCTET = "application/octet-stream"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")

# Extensions a browser may render or run, whatever the stored type says.
RISKY_EXTENSIONS = frozenset(
    {
        "html", "htm", "xhtml", "xht", "shtml", "mht", "mhtml", "svg", "svgz", "xml",
        "xsl", "xslt", "rdf", "rss", "atom", "js", "mjs", "cjs", "jsx", "vbs", "hta",
        "htc", "swf", "php",
    }
)  # fmt: skip
_RISKY_TYPES = frozenset(
    {
        "application/x-shockwave-flash",
        "application/hta",
        "text/x-component",
        "text/jscript",
        "text/vbscript",
    }
)
# Types a download may keep (with a harmless extension); every other one is octet-stream.
INLINE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "application/pdf",
        "text/plain",
    }
)


def _bad_file_key(message: str) -> ValidationError:
    return ValidationError("sites.data_bad_file_key", message)


def check_file_key(key: str | None) -> str:
    """``key`` when it is a safe R2 object name, else 422 ``sites.data_bad_file_key``."""
    if not isinstance(key, str) or not key:
        raise _bad_file_key("Give the file a name.")
    if len(key.encode("utf-8")) > MAX_FILE_KEY_BYTES:
        raise _bad_file_key("That file name is too long (1,024 bytes at most).")
    if "\\" in key or _CONTROL.search(key):
        raise _bad_file_key("A file name can't contain “\\” or control characters.")
    for segment in key.split("/"):
        if segment in ("", ".", ".."):
            # Also refuses a leading "/", a trailing one and "//".
            raise _bad_file_key(
                "A file name can't start with “/” or have empty, “.” or “..” folders."
            )
    return key


def check_prefix(prefix: str | None) -> str:
    """A list prefix: "" or a folder path, possibly ending in "/"."""
    prefix = prefix or ""
    if not prefix:
        return ""
    check_file_key(prefix[:-1] if prefix.endswith("/") else prefix)
    return prefix


def _extension(key: str) -> str:
    return posixpath.splitext(key.rsplit("/", 1)[-1])[1].lstrip(".").lower()


def _base_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def risky_type(mime: str) -> bool:
    """Whether a browser could render ``mime`` as a page or run it as a script."""
    if not _MIME.match(mime):
        return True
    main, sub = mime.split("/", 1)
    return (
        mime in _RISKY_TYPES
        or main == "multipart"
        or "html" in sub
        or "javascript" in sub
        or "ecmascript" in sub
        or sub == "xml"
        or sub.endswith("+xml")
        or "svg" in sub
    )


def risky_extension(key: str) -> bool:
    return _extension(key) in RISKY_EXTENSIONS


def stored_content_type(declared: str | None, key: str) -> str:
    """The Content-Type to store an upload under: the declared one, else the
    extension's guess; octet-stream when either is risky or the key's extension is."""
    if risky_extension(key):
        return OCTET
    mime = _base_type(declared)
    if not mime or mime == OCTET:
        mime = _base_type(mimetypes.guess_type(key.rsplit("/", 1)[-1])[0]) or OCTET
    if mime == OCTET or risky_type(mime):
        return OCTET
    return mime


def download_content_type(stored: str | None, key: str) -> str:
    """The Content-Type a download is served with."""
    mime = _base_type(stored)
    if mime in INLINE_TYPES and not risky_extension(key):
        return mime
    return OCTET


def download_filename(key: str) -> tuple[str, str]:
    """``(ascii, utf8_quoted)`` names for Content-Disposition's ``filename`` and
    ``filename*``: the key's last segment, the ASCII one with anything but letters,
    digits, ``.``, ``-`` and ``_`` replaced."""
    base = key.rsplit("/", 1)[-1]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")[:120] or "file"
    return safe, quote(base[:200], safe="")


def content_disposition(key: str) -> str:
    ascii_name, utf8_name = download_filename(key)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


def check_kv_key(key: str | None) -> str:
    """``key`` when Cloudflare can address it, else 422 ``sites.data_bad_kv_key``."""
    if not isinstance(key, str) or key in ("", ".", ".."):
        raise ValidationError("sites.data_bad_kv_key", "That key name can't be used.")
    if len(key.encode("utf-8")) > MAX_KV_KEY_BYTES:
        raise ValidationError("sites.data_bad_kv_key", "A key name is at most 512 bytes.")
    return key


def masked_kv_key(name: str) -> bool:
    """Whether a KV key's value is hidden (its name looks like a credential's)."""
    return is_credential(name)


__all__ = [
    "INLINE_TYPES",
    "MASK",
    "MAX_FILE_BYTES",
    "MAX_KV_EDIT_BYTES",
    "MAX_KV_METADATA_BYTES",
    "MIN_TTL",
    "OCTET",
    "RISKY_EXTENSIONS",
    "check_file_key",
    "check_kv_key",
    "check_prefix",
    "content_disposition",
    "download_content_type",
    "download_filename",
    "masked_kv_key",
    "risky_extension",
    "risky_type",
    "stored_content_type",
]
