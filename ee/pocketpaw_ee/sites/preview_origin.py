# ee/pocketpaw_ee/sites/preview_origin.py — the draft preview origin.
#
# A Paw Site draft is previewed from a real, cookieless origin instead of a srcdoc:
# ``https://<token>.<PAW_SITES_PREVIEW_BASE_URL host>/index.html``. The token is an
# unguessable capability (random, 128 bits) minted per (pocket, content hash), so an
# edit gets a new URL and an old URL keeps serving its own immutable build until the
# artifact store evicts it (then it is a 404).
#
# The token lives in the SUBDOMAIN, never the path: built sites emit root-absolute
# refs (``/assets/x.js``, ``/_app/...``) and those must resolve exactly as they do
# once published. One host per draft is what makes that true.
#
# What this module owns:
#   * the base-URL setting and the token <-> host mapping;
#   * packing a draft's files into the tgz the artifact store keeps per content hash;
#   * html drafts: the declared-package import map and (for ``?paw_edit=1``) the
#     vendored edit-bridge (``edit_bridge.js``), since html drafts never build;
#   * ``preview_app`` — the ASGI app that serves a draft — and
#     ``PreviewHostDispatch``, the middleware that routes preview-host requests to it
#     inside the main API process.
#
# Invariants: ``preview_app`` never reads cookies or auth, never sets a cookie, and
# answers an unknown token with a bare 404. It serves bytes only from the artifact
# store; nothing in a request can name a path outside a draft's own file set.

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import re
import secrets
import tarfile
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

logger = logging.getLogger(__name__)

PREVIEW_BASE_ENV = "PAW_SITES_PREVIEW_BASE_URL"

#: A preview token: 32 lowercase hex chars. Lowercase because hostnames are
#: case-insensitive; 32 fits comfortably in one 63-char DNS label.
TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")

#: Where the ``?paw_edit=1`` variant of an html page lives inside a draft's file set.
#: A dot-segment, so no request path can address it directly.
EDIT_VARIANT_DIR = ".paw-edit"

ENTRY = "index.html"
NOT_FOUND_PAGE = "404.html"

#: Ceilings on one unpacked draft (same scale as the artifact preview lane).
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 128 * 1024 * 1024

#: Drafts kept unpacked in memory per process (LRU, bounded by count AND bytes).
_CACHE_ENTRIES = 8
_CACHE_BYTES = 64 * 1024 * 1024

_JS = "application/javascript; charset=utf-8"

_HASHED_NAME_RE = re.compile(r"[-.][A-Za-z0-9_]{6,}\.[a-z0-9]+$")

#: Headers on every preview response. No framing restriction: the builder frames
#: this origin. No cookies, no credentials: ``*`` is the only ACAO a cookieless
#: origin needs. ``no-referrer`` keeps the capability host out of Referer headers
#: sent to third-party CDNs the draft loads from.
_BASE_HEADERS: tuple[tuple[str, str], ...] = (
    ("access-control-allow-origin", "*"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-robots-tag", "noindex, nofollow"),
    ("cross-origin-resource-policy", "cross-origin"),
)


# ---------------------------------------------------------------------------
# Base URL + token <-> host
# ---------------------------------------------------------------------------


def _default_base_url() -> str:
    try:
        from pocketpaw.config import get_settings

        port = int(get_settings().web_port)
    except Exception:  # pragma: no cover - settings unavailable
        port = 8888
    # Chromium resolves *.localhost to loopback, so this works with no DNS setup.
    return f"http://preview.localhost:{port}"


def preview_base_url() -> str:
    """The preview origin base (``PAW_SITES_PREVIEW_BASE_URL``), no trailing slash."""
    configured = os.environ.get(PREVIEW_BASE_ENV, "").strip().rstrip("/")
    return configured or _default_base_url()


def _base_parts() -> tuple[str, str, int | None]:
    parts = urlsplit(preview_base_url())
    host = (parts.hostname or "").lower()
    return parts.scheme or "http", host, parts.port


def preview_base_host() -> str:
    return _base_parts()[1]


def preview_url_for(token: str) -> str:
    """The absolute URL of a draft's index.html on the preview origin."""
    scheme, host, port = _base_parts()
    netloc = f"{token}.{host}" + (f":{port}" if port else "")
    return f"{scheme}://{netloc}/{ENTRY}"


def _host_without_port(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if host.startswith("["):  # IPv6 literal: never a preview host
        return host
    return host.rsplit(":", 1)[0] if ":" in host else host


def is_preview_host(host: str) -> bool:
    """True when ``host`` (a Host header) is a subdomain of the preview base host."""
    base = preview_base_host()
    return bool(base) and _host_without_port(host).endswith("." + base)


def token_from_host(host: str) -> str | None:
    """The token of a ``<token>.<base host>`` Host header, or ``None``."""
    base = preview_base_host()
    name = _host_without_port(host)
    if not base or not name.endswith("." + base):
        return None
    label = name[: -len(base) - 1]
    return label if TOKEN_RE.fullmatch(label) else None


def new_token() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

_STORE_METHODS = (
    "write_dist",
    "read_dist",
    "write_preview_token",
    "read_preview_token",
    "resolve_preview_token",
)


def store_supports_preview(store: Any) -> bool:
    """True when ``store`` can hold draft files + tokens (the real stores can; a
    body/css-only test fake cannot, and then no preview URL is minted)."""
    return all(callable(getattr(store, m, None)) for m in _STORE_METHODS)


def existing_preview_url(store: Any, pocket_id: str, content_hash: str) -> str | None:
    """The preview URL already minted for this draft, or ``None``."""
    if not store_supports_preview(store):
        return None
    token = store.read_preview_token(pocket_id, content_hash)
    return preview_url_for(token) if token and TOKEN_RE.fullmatch(token) else None


def publish_draft(store: Any, pocket_id: str, content_hash: str, files_tgz: bytes) -> str | None:
    """Store a draft's files and mint (or reuse) its token. Returns the preview URL,
    or ``None`` when the store cannot hold drafts."""
    if not store_supports_preview(store):
        return None
    store.write_dist(pocket_id, content_hash, files_tgz)
    token = store.read_preview_token(pocket_id, content_hash)
    if not token or not TOKEN_RE.fullmatch(token):
        token = new_token()
        store.write_preview_token(pocket_id, content_hash, token)
    return preview_url_for(token)


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------


def _safe_segments(name: str) -> tuple[str, ...] | None:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        return None
    if len(name) > 1 and name[1] == ":":
        return None
    out: list[str] = []
    for seg in name.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        out.append(seg)
    return tuple(out) or None


def pack_files(files: Mapping[str, bytes]) -> bytes:
    """A gzipped tar of ``files`` (relpath -> bytes), in sorted order."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name in sorted(files):
                segments = _safe_segments(name)
                if segments is None:
                    continue
                data = files[name]
                info = tarfile.TarInfo("/".join(segments))
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def pack_dir(root: Path) -> bytes:
    """Pack every regular file under ``root`` (already extracted through the
    guarded unpacker, so no links or escapes reach here)."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return pack_files(files)


def unpack_files(data: bytes) -> dict[str, bytes]:
    """The files of a packed draft. Unsafe names are dropped; past the ceilings the
    draft is refused (``ValueError``)."""
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            segments = _safe_segments(member.name)
            if segments is None:
                continue
            total += member.size
            if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise ValueError("draft exceeds the preview size ceilings")
            fh = tar.extractfile(member)
            if fh is None:
                continue
            files["/".join(segments)] = fh.read()
    return files


# ---------------------------------------------------------------------------
# html drafts: import map + edit bridge
# ---------------------------------------------------------------------------

_HTML_RE = re.compile(r"\.html?$", re.IGNORECASE)
_HEAD_OPEN_RE = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_IMPORTMAP_RE = re.compile(
    r"""<script\b[^>]*\btype\s*=\s*["']?importmap["']?[^>]*>(.*?)</script\s*>""",
    re.IGNORECASE | re.DOTALL,
)


def _jsdelivr_package_base(name: str, version: str) -> str:
    return f"https://cdn.jsdelivr.net/npm/{name}@{version}/"


def _script_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c")


def inject_import_map(html: str, packages: Mapping[str, Mapping[str, str]]) -> str:
    """Python twin of paw-sites ``injectImportMap``: put the declared packages' import
    map as the first child of ``<head>``, merged with any authored one (declared names
    win). A page with no ``<head>`` tag, or nothing declared, is returned unchanged.

    One deliberate difference: a package with no SRI ``integrity`` is still mapped (a
    draft should run what the author declared); integrity is added when present."""
    imports: dict[str, str] = {}
    integrity: dict[str, str] = {}
    for name, pkg in packages.items():
        esm, version = pkg.get("esm"), pkg.get("version")
        if not esm or not version:
            continue
        imports[name] = esm
        imports[f"{name}/"] = _jsdelivr_package_base(name, version)
        if pkg.get("integrity"):
            integrity[esm] = pkg["integrity"]
    if not imports:
        return html
    head = _HEAD_OPEN_RE.search(html)
    if head is None:
        return html

    merged_imports: dict[str, Any] = {}
    scopes: dict[str, Any] = {}
    merged_integrity: dict[str, Any] = {}
    for m in _IMPORTMAP_RE.finditer(html):
        try:
            authored = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(authored, dict):
            continue
        for key, target in (
            ("imports", merged_imports),
            ("scopes", scopes),
            ("integrity", merged_integrity),
        ):
            if isinstance(authored.get(key), dict):
                target.update(authored[key])
    names = list(packages)
    kept = {
        k: v
        for k, v in merged_imports.items()
        if not any(k == n or k.startswith(f"{n}/") for n in names)
    }
    kept.update(imports)
    out_map: dict[str, Any] = {"imports": kept}
    if scopes:
        out_map["scopes"] = scopes
    if merged_integrity or integrity:
        out_map["integrity"] = {**merged_integrity, **integrity}

    preloads = "".join(
        f'<link rel="modulepreload" href="{pkg["esm"]}" integrity="{pkg["integrity"]}" '
        'crossorigin="anonymous">'
        for pkg in packages.values()
        if pkg.get("esm") and pkg.get("integrity")
    )
    block = f'<script type="importmap">{_script_json(out_map)}</script>{preloads}'
    # Drop authored maps (a page may only have one that takes effect), then insert.
    stripped = _IMPORTMAP_RE.sub("", html)
    head = _HEAD_OPEN_RE.search(stripped)
    assert head is not None  # removing script tags cannot remove <head>
    return stripped[: head.end()] + block + stripped[head.end() :]


_BRIDGE_PLACEHOLDER = '"__PAW_BUILDER_ORIGIN__"'
_BRIDGE_SCRIPT_ID = "paw-edit-bridge"
_bridge_template: str | None = None


def _bridge_source() -> str:
    global _bridge_template
    if _bridge_template is None:
        text = Path(__file__).with_name("edit_bridge.js").read_text(encoding="utf-8")
        # Drop the leading // header comment block; the page gets the IIFE only.
        lines = text.splitlines()
        while lines and (lines[0].startswith("//") or not lines[0].strip()):
            lines.pop(0)
        _bridge_template = "\n".join(lines)
    return _bridge_template


def edit_bridge_script(builder_origin: str) -> str:
    """The edit-bridge IIFE posting only to ``builder_origin`` (inert without
    ``?paw_edit=1``, same as the armed svelte/react builds)."""
    return _bridge_source().replace(_BRIDGE_PLACEHOLDER, _script_json(builder_origin))


def inject_edit_bridge(html: str, builder_origin: str) -> str:
    """Insert the bridge before the LAST ``</body>`` (an earlier one may sit inside a
    script string), or append it when the page has none."""
    tag = f'<script id="{_BRIDGE_SCRIPT_ID}">\n{edit_bridge_script(builder_origin)}\n</script>'
    idx = html.lower().rfind("</body>")
    if idx < 0:
        return html + tag
    return html[:idx] + tag + html[idx:]


async def materialize_html_draft(
    source: Mapping[str, Any], builder_origin: str, *, arm: Any = None
) -> dict[str, bytes]:
    """The served file set of an html draft: its source files, the import map in
    every page, and an ``?paw_edit=1`` variant of each page (data-uid stamped through
    ``arm`` when it is available, plus the edit bridge) under ``.paw-edit/``."""
    from pocketpaw_ee.sites import dependency_manifest as dm

    try:
        packages = dm.parse_manifest(dm.manifest_text(source))
    except ValueError:
        packages = {}
    pages: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for rel, contents in source.items():
        if not isinstance(contents, str) or _safe_segments(rel) is None:
            continue
        if _HTML_RE.search(rel):
            pages[rel] = inject_import_map(contents, packages) if packages else contents
            files[rel] = pages[rel].encode("utf-8")
        else:
            files[rel] = contents.encode("utf-8")

    armed: Mapping[str, Any] = {}
    if pages and arm is not None:
        try:
            out = await arm(source=dict(pages))
            armed = out.get("source") or {}
        except Exception as exc:
            # No toolchain on this host, or a page the stamper refused: the bridge
            # still loads, it just has no data-uid leaves to report.
            logger.warning("sites.preview_origin: html arming unavailable (%s)", exc)
    for rel, page in pages.items():
        stamped = armed.get(rel) if isinstance(armed.get(rel), str) else page
        files[f"{EDIT_VARIANT_DIR}/{rel}"] = inject_edit_bridge(stamped, builder_origin).encode(
            "utf-8"
        )
    return files


# ---------------------------------------------------------------------------
# The ASGI app
# ---------------------------------------------------------------------------

_draft_cache: OrderedDict[tuple[str, str], dict[str, bytes]] = OrderedDict()
_draft_cache_lock = threading.Lock()  # _load_draft runs in worker threads


def _content_type(name: str) -> str:
    from pocketpaw_ee.sites.artifact_preview import content_type_for

    suffix = Path(name).suffix.lower()
    if suffix in (".js", ".mjs", ".cjs"):
        return _JS
    return content_type_for(name)


def _cache_control(rel: str, status: int) -> str:
    if status != 200:
        return "no-store"
    if rel.startswith(("assets/", "_app/immutable/")) or _HASHED_NAME_RE.search(rel):
        return "public, max-age=31536000, immutable"
    return "no-cache"


def _load_draft(token: str) -> dict[str, bytes] | None:
    from pocketpaw_ee.sites import service as sites_service

    store = sites_service._default_artifact_store()
    if not store_supports_preview(store):
        return None
    ref = store.resolve_preview_token(token)
    if ref is None:
        return None
    key = (ref[0], ref[1])
    with _draft_cache_lock:
        cached = _draft_cache.get(key)
        if cached is not None:
            _draft_cache.move_to_end(key)
            return cached
    data = store.read_dist(*key)
    if data is None:
        return None
    try:
        files = unpack_files(data)
    except (tarfile.TarError, OSError, EOFError, ValueError):
        logger.warning("sites.preview_origin: unreadable draft for token %s…", token[:6])
        return None
    size = sum(len(b) for b in files.values())
    if size > _CACHE_BYTES:
        return files  # served, not cached: one huge draft must not evict the rest
    with _draft_cache_lock:
        _draft_cache[key] = files
        total = sum(sum(len(b) for b in f.values()) for f in _draft_cache.values())
        while len(_draft_cache) > _CACHE_ENTRIES or total > _CACHE_BYTES:
            _old_key, old = _draft_cache.popitem(last=False)
            total -= sum(len(b) for b in old.values())
    return files


def _resolve(files: Mapping[str, bytes], path: str, edit: bool) -> tuple[int, str] | None:
    """(status, relpath) for a request path, or ``None`` for a 404 with no page."""
    decoded = unquote(path)
    if "\x00" in decoded or "\\" in decoded:
        return None
    segments = [s for s in decoded.split("/") if s not in ("", ".")]
    if any(s == ".." or s.startswith(".") for s in segments):
        return None
    rel = "/".join(segments)
    candidates = [rel] if rel else []
    candidates += [f"{rel}/{ENTRY}" if rel else ENTRY]
    if rel and not rel.lower().endswith((".html", ".htm")):
        candidates.append(f"{rel}.html")
    for cand in candidates:
        if cand in files:
            if edit and _HTML_RE.search(cand) and f"{EDIT_VARIANT_DIR}/{cand}" in files:
                return 200, f"{EDIT_VARIANT_DIR}/{cand}"
            return 200, cand
    if NOT_FOUND_PAGE in files:
        return 404, NOT_FOUND_PAGE
    # Client-routed SPA: an extensionless navigation falls back to the entry page.
    last = segments[-1] if segments else ""
    if ENTRY in files and "." not in last:
        return 200, ENTRY
    return None


async def _send(send: Any, status: int, body: bytes, headers: list[tuple[str, str]], head: bool):
    raw = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    raw.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": b"" if head else body})


async def preview_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Serve one draft file. The draft is chosen by the Host subdomain token only."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return  # websockets have nothing to talk to here

    method = scope.get("method", "GET").upper()
    base = list(_BASE_HEADERS)
    if method == "OPTIONS":
        await _send(
            send,
            204,
            b"",
            base + [("access-control-allow-methods", "GET, HEAD, OPTIONS")],
            True,
        )
        return
    plain = base + [("content-type", "text/plain; charset=utf-8"), ("cache-control", "no-store")]
    if method not in ("GET", "HEAD"):
        await _send(send, 405, b"Method not allowed", plain + [("allow", "GET, HEAD")], False)
        return
    head = method == "HEAD"

    host = ""
    for k, v in scope.get("headers") or []:
        if k == b"host":
            host = v.decode("latin-1")
            break
    token = token_from_host(host)
    files = await asyncio.to_thread(_load_draft, token) if token else None
    if files is None:
        await _send(send, 404, b"Not found", plain, head)
        return

    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    edit = query.get("paw_edit") == ["1"]
    resolved = _resolve(files, scope.get("path", "/"), edit)
    if resolved is None:
        await _send(send, 404, b"Not found", plain, head)
        return
    status, rel = resolved
    headers = base + [
        ("content-type", _content_type(rel)),
        ("cache-control", _cache_control(rel, status)),
    ]
    await _send(send, status, files[rel], headers, head)


class PreviewHostDispatch:
    """ASGI middleware: a request whose Host is ``<token>.<preview base host>`` goes
    to :func:`preview_app`; everything else to the wrapped app. Lets one self-hosted
    process serve both the API and the preview origin. The preview branch bypasses
    every inner middleware, so no auth, session or cookie layer ever sees it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            for k, v in scope.get("headers") or []:
                if k == b"host":
                    if is_preview_host(v.decode("latin-1")):
                        await preview_app(scope, receive, send)
                        return
                    break
        await self.app(scope, receive, send)


__all__ = [
    "PREVIEW_BASE_ENV",
    "PreviewHostDispatch",
    "existing_preview_url",
    "inject_edit_bridge",
    "inject_import_map",
    "materialize_html_draft",
    "pack_dir",
    "pack_files",
    "preview_app",
    "preview_base_url",
    "preview_url_for",
    "publish_draft",
    "store_supports_preview",
    "token_from_host",
    "unpack_files",
]
