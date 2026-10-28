# ee/pocketpaw_ee/sites/site_data/files_kv.py — the rules the Sites Data dashboard
# applies to a site's R2 files and KV keys, kept pure so each one is tested alone.
#
#   * File keys (``check_file_key``): 1-1024 UTF-8 bytes, no leading "/", no empty,
#     "." or ".." segment, no backslash, no control character. Anything else is a 422
#     ``sites.data_bad_file_key`` before any Cloudflare call. List prefixes are looser
#     (``check_prefix``: may be empty or end in "/").
#   * Content types are an ALLOWLIST (``SAFE_TYPES`` plus ``audio/*`` / ``video/*``
#     without "xml" or "script"; parameters and case ignored). An upload keeps the type
#     the request declared, else the one its extension implies, only when that type is
#     allowed AND the key's extension is harmless (not in ``RISKY_EXTENSIONS``, and any
#     type it implies is allowed too); everything else is stored as
#     ``application/octet-stream``, so a bucket served publicly can't host a page or a
#     script (``stored_content_type``). A download applies the same rule to the stored
#     type (``download_content_type``); the router always adds ``attachment``,
#     ``nosniff`` and a sandboxing CSP. ``download_filename`` is the key's last
#     segment, made header-safe like the CSV export's name.
#   * KV keys (``check_kv_key``): 1-512 UTF-8 bytes, no "." / ".." segment. A key named
#     like a credential or a session (``KV_CREDENTIAL``: the shared credential words
#     plus credentials, access key, session, jwt, cookie, passphrase, auth) is masked:
#     its value reads ``MASK``, its size and metadata are hidden, and it can't be
#     written here (it can still be deleted). Key names themselves are shown (only to
#     callers with write access), but an audit row keeps a name only when it isn't
#     token-like: a masked or over-64-character name is stored as ``sha256:<16 hex>``
#     (``audit_kv_name``).
from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
from urllib.parse import quote

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.project_data import CREDENTIAL, MASK

# Read at call time so tests can shrink them.
MAX_FILE_BYTES = 50 * 1024 * 1024  # one upload or move through the API (v1)
MAX_KV_EDIT_BYTES = 1024 * 1024  # a value the dashboard shows and edits
MAX_KV_METADATA_BYTES = 1024  # Cloudflare's own cap on a key's metadata (as JSON)
MIN_TTL = 60  # Cloudflare's minimum expiration_ttl
MAX_FILE_KEY_BYTES = 1024
MAX_KV_KEY_BYTES = 512

OCTET = "application/octet-stream"
# KV names: the shared credential words plus what sites keep sessions and tokens
# under (better-auth's secondaryStorage writes ``session:<token>``). Matched on the
# snake_case, lowercased name.
KV_CREDENTIAL = re.compile(
    CREDENTIAL.pattern + r"|credentials?|access_?key|session|jwt|cookie|passphrase|auth"
)
_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")
MAX_AUDIT_NAME = 64
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
# The only types an object is stored or served as (besides audio/* and video/*);
# every other one, known or not, is octet-stream.
SAFE_TYPES = frozenset(
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
        "text/csv",
        "application/json",
        "font/woff",
        "font/woff2",
        "font/ttf",
        "font/otf",
        "application/zip",
        "application/gzip",
        "text/css",
    }
)
# A fixed table, never the OS one (Windows reads the registry), plus what it lacks.
_GUESS = mimetypes.MimeTypes()
_EXTRA_TYPES = {
    "webp": "image/webp",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "gz": "application/gzip",
    "ogg": "audio/ogg",
}


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


def safe_type(mime: str) -> bool:
    """Whether ``mime`` (a base type, lowercased) is on the allowlist."""
    if mime in SAFE_TYPES:
        return True
    if not _MIME.match(mime):
        return False
    main, sub = mime.split("/", 1)
    return main in ("audio", "video") and "xml" not in sub and "script" not in sub


def guessed_type(key: str) -> str:
    """The type the key's extension implies ("" when it implies none)."""
    name = key.rsplit("/", 1)[-1]
    extra = _EXTRA_TYPES.get(_extension(name))
    if extra:
        return extra
    mime, encoding = _GUESS.guess_type(name, strict=False)
    if encoding == "gzip" and not mime:
        return "application/gzip"
    return _base_type(mime)


def harmless_extension(key: str) -> bool:
    """Not a page / script extension, and any type it implies is allowed."""
    if _extension(key) in RISKY_EXTENSIONS:
        return False
    guess = guessed_type(key)
    return not guess or guess == OCTET or safe_type(guess)


def stored_content_type(declared: str | None, key: str) -> str:
    """The Content-Type to store an upload under: the declared one, else the
    extension's guess, when that is allowed and the extension is harmless; else
    octet-stream."""
    if not harmless_extension(key):
        return OCTET
    mime = _base_type(declared)
    if not mime or mime == OCTET:
        mime = guessed_type(key) or OCTET
    return mime if safe_type(mime) else OCTET


def download_content_type(stored: str | None, key: str) -> str:
    """The Content-Type a download is served with: the stored one under the same
    allowlist, else octet-stream."""
    mime = _base_type(stored)
    if safe_type(mime) and harmless_extension(key):
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
    if not isinstance(key, str) or not key or any(s in (".", "..") for s in key.split("/")):
        raise ValidationError("sites.data_bad_kv_key", "That key name can't be used.")
    if len(key.encode("utf-8")) > MAX_KV_KEY_BYTES:
        raise ValidationError("sites.data_bad_kv_key", "A key name is at most 512 bytes.")
    return key


def masked_kv_key(name: str) -> bool:
    """Whether a KV key's value is hidden (its name looks like a credential's or a
    session's)."""
    return bool(KV_CREDENTIAL.search(_CAMEL.sub(r"_\1", name).lower()))


def audit_kv_name(name: str) -> str:
    """``name`` as an audit row may keep it: as is, unless it is masked or longer
    than 64 characters (likely a token), then ``sha256:`` + 16 hex digits."""
    if masked_kv_key(name) or len(name) > MAX_AUDIT_NAME:
        return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return name


__all__ = [
    "MASK",
    "MAX_FILE_BYTES",
    "MAX_KV_EDIT_BYTES",
    "MAX_KV_METADATA_BYTES",
    "MIN_TTL",
    "KV_CREDENTIAL",
    "OCTET",
    "RISKY_EXTENSIONS",
    "SAFE_TYPES",
    "audit_kv_name",
    "check_file_key",
    "check_kv_key",
    "check_prefix",
    "content_disposition",
    "download_content_type",
    "download_filename",
    "masked_kv_key",
    "harmless_extension",
    "safe_type",
    "stored_content_type",
]
