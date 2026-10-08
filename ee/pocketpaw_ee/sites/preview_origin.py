# ee/pocketpaw_ee/sites/preview_origin.py — the draft preview origin.
#
# A Paw Site draft is previewed from a real, cookieless origin instead of a srcdoc:
# ``https://<token>.<PAW_SITES_PREVIEW_BASE_URL host>/`` (the site root, which serves
# ``index.html``; client routers treat ``/index.html`` as an unknown route). The token is an
# unguessable capability (random, 128 bits) minted per (pocket, content hash), so an
# edit gets a new URL and an old URL keeps serving its own immutable build until the
# artifact store evicts it (then it is a 404).
#
# The token lives in the SUBDOMAIN, never the path: built sites emit root-absolute
# refs (``/assets/x.js``, ``/_app/...``) and those must resolve exactly as they do
# once published. One host per draft is what makes that true.
#
# What this module owns:
#   * the base-URL setting (validated: a malformed base, or one that equals or
#     contains the app's own host, turns previews off) and the token <-> host map;
#   * packing a draft's files into the tgz the artifact store keeps per content hash;
#   * html drafts (they never build): the declared-package import map and the
#     vendored runtime-error reporter (``runtime_reporter.js``) in every page, plus an
#     ``?paw_edit=1`` variant stamped with data-uid (paw-sites ``arm-html``, else the
#     ``html_uid_stamp`` port) carrying the vendored edit-bridge (``edit_bridge.js``).
#     Both scripts are pinned to a paw-sites commit (paw-sites-edit-bridge.pin.json).
#   * ``preview_app`` — the ASGI app that serves a draft (a full-mode project draft
#     with a live draft Worker is reverse-proxied to it by ``preview_proxy``) — and
#     ``PreviewHostDispatch``, the middleware that routes preview-host requests to it
#     inside the main API process. It must be the OUTERMOST layer (``install_cors``
#     adds it last via ``app.state.outermost_middleware``), or auth and rate limits
#     answer preview requests.
#
# Invariants: ``preview_app`` never reads cookies or auth for its OWN decisions, never
# mints a cookie, and answers an unknown token with a bare 404. Only a proxied
# full-mode draft (``preview_proxy``) passes its own host's Cookie / Set-Cookie
# through, host-only; static drafts and every other response strip Cookie and never
# send Set-Cookie. Static bytes come only from the artifact store; nothing in a
# request can name a path outside a draft's own file set or choose a proxy upstream.
# A preview URL is handed out only when the draft's files (or its draft Worker) AND
# token are both stored.
# Per-process caches (token lookups, unpacked drafts, single-flight loads) keep an
# asset request off the store; every servable draft fits the draft cache.

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
import time
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

#: Ceilings on one unpacked draft. ``MAX_TOTAL_BYTES`` stays at or under
#: ``_CACHE_BYTES`` so every servable draft fits the cache: a draft too big to cache
#: would be re-read and re-unpacked on every asset request.
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 64 * 1024 * 1024

#: Drafts kept unpacked in memory per process (LRU, bounded by count AND bytes).
_CACHE_ENTRIES = 8
_CACHE_BYTES = 128 * 1024 * 1024

#: token -> (pocket, hash) resolutions kept per process, so an asset request does not
#: hit the store to resolve its token. A miss is remembered briefly too: random tokens
#: would otherwise cost a store read each. A hit expires so an evicted draft stops
#: serving within ``_TOKEN_HIT_TTL``.
_TOKEN_CACHE_ENTRIES = 4096
_TOKEN_HIT_TTL = 60.0
_TOKEN_MISS_TTL = 30.0

_JS = "application/javascript; charset=utf-8"

#: A bundler content hash in a filename: ``index-BvK3x9_a.js``, ``chunk.3f9a1c2e.css``.
#: 8+ chars with a digit or capital, so a plain word (``hero-banner.png``) is not one.
_HASHED_NAME_RE = re.compile(r"[-.](?=[A-Za-z0-9_-]*[0-9A-Z])[A-Za-z0-9_-]{8,}\.[a-z0-9]+$")

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


#: Env vars naming the hosts the API and dashboard answer on. The preview base must
#: never equal or contain one of them: ``PreviewHostDispatch`` would route that host's
#: whole API to ``preview_app``.
_APP_URL_ENVS = (
    "POCKETPAW_PUBLIC_BASE_URL",
    "POCKETPAW_FRONTEND_BASE_URL",
    "PAW_SITES_BUILDER_ORIGIN",
)


def _parse_base(raw: str) -> tuple[tuple[str, str, int | None] | None, str | None]:
    """(scheme, host, port) of a base URL, or ``(None, why it is refused)``."""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        return None, f"{PREVIEW_BASE_ENV}={raw!r} needs an http:// or https:// scheme"
    try:
        port = parts.port
    except ValueError:
        return None, f"{PREVIEW_BASE_ENV}={raw!r} has an invalid port"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host or host.startswith("["):
        return None, f"{PREVIEW_BASE_ENV}={raw!r} needs a dotted host name"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None, f"{PREVIEW_BASE_ENV}={raw!r} must be scheme://host[:port] only"
    for env in _APP_URL_ENVS:
        app_host = (urlsplit(os.environ.get(env, "").strip()).hostname or "").lower()
        if app_host and (app_host == host or app_host.endswith("." + host)):
            return None, (
                f"{PREVIEW_BASE_ENV} host {host!r} equals or contains the app host "
                f"{app_host!r} ({env}); every request to it would be served as a draft"
            )
    return (parts.scheme, host, port), None


def preview_base_problem() -> str | None:
    """Why the configured preview base is refused, or ``None`` when it is usable. A
    refused base turns the preview origin OFF (no URL is minted, no host is routed
    to the preview app) rather than taking the API down with it."""
    return _parse_base(preview_base_url())[1]


def check_preview_base() -> bool:
    """Startup check: log loudly (ERROR) when the base is refused. True when usable."""
    problem = preview_base_problem()
    if problem is not None:
        logger.error("sites.preview_origin: draft previews are DISABLED: %s", problem)
    return problem is None


def _base_parts() -> tuple[str, str, int | None] | None:
    return _parse_base(preview_base_url())[0]


def preview_base_host() -> str:
    parts = _base_parts()
    return parts[1] if parts else ""


def preview_url_for(token: str) -> str | None:
    """The absolute URL of a draft's site root on the preview origin, or ``None``
    when the configured base is refused (see :func:`preview_base_problem`). The root,
    not ``/index.html``: client routers (TanStack, React Router) treat that path as an
    unmatched route, and the root resolves to the same ``index.html`` file."""
    parts = _base_parts()
    if parts is None:
        return None
    scheme, host, port = parts
    netloc = f"{token}.{host}" + (f":{port}" if port else "")
    return f"{scheme}://{netloc}/"


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
    if preview_base_problem() is not None:
        return None
    token = store.read_preview_token(pocket_id, content_hash)
    return preview_url_for(token) if token and TOKEN_RE.fullmatch(token) else None


def _mint_token(store: Any, pocket_id: str, content_hash: str) -> str | None:
    """The draft's token, minting and storing one when it has none. ``None`` when the
    store could not record a new token (a URL for it would 404)."""
    token = store.read_preview_token(pocket_id, content_hash)
    if token and TOKEN_RE.fullmatch(token):
        return token
    token = new_token()
    return token if store.write_preview_token(pocket_id, content_hash, token) else None


def publish_draft(store: Any, pocket_id: str, content_hash: str, files_tgz: bytes) -> str | None:
    """Store a draft's files and mint (or reuse) its token. Returns the preview URL
    only when BOTH the files and the token are stored; ``None`` when the store cannot
    hold drafts, refused or failed either write, or the preview base is refused. A
    URL is never handed out for a draft the origin cannot serve."""
    if not store_supports_preview(store) or preview_base_problem() is not None:
        return None
    if not store.write_dist(pocket_id, content_hash, files_tgz):
        return None
    token = _mint_token(store, pocket_id, content_hash)
    return preview_url_for(token) if token else None


def repair_preview_url(store: Any, pocket_id: str, content_hash: str) -> str | None:
    """A URL for a draft whose files are stored but whose token write failed: mint the
    token now, no rebuild. ``None`` when the files are not there either."""
    if not store_supports_preview(store) or preview_base_problem() is not None:
        return None
    if store.read_dist(pocket_id, content_hash) is None:
        return None
    token = _mint_token(store, pocket_id, content_hash)
    return preview_url_for(token) if token else None


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
_REPORTER_SCRIPT_ID = "paw-runtime-reporter"
_vendored_cache: dict[str, str] = {}
_DOCTYPE_RE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)


def _vendored(name: str) -> str:
    """A vendored paw-sites script (``edit_bridge.js`` / ``runtime_reporter.js``)
    without its leading ``//`` header block."""
    if name not in _vendored_cache:
        text = Path(__file__).with_name(name).read_text(encoding="utf-8")
        lines = text.splitlines()
        while lines and (lines[0].startswith("//") or not lines[0].strip()):
            lines.pop(0)
        _vendored_cache[name] = "\n".join(lines)
    return _vendored_cache[name]


def edit_bridge_script(builder_origin: str) -> str:
    """The edit-bridge IIFE posting only to ``builder_origin`` (its commands and
    hover/click reports are inert without ``?paw_edit=1``, same as the armed
    svelte/react builds)."""
    return _vendored("edit_bridge.js").replace(_BRIDGE_PLACEHOLDER, _script_json(builder_origin))


def runtime_reporter_script(builder_origin: str) -> str:
    """The runtime-error reporter IIFE (``__pawRuntime`` messages to ``builder_origin``).
    Installs once per window, so it coexists with the copy inside the bridge."""
    return _vendored("runtime_reporter.js").replace(
        _BRIDGE_PLACEHOLDER, _script_json(builder_origin)
    )


def inject_edit_bridge(html: str, builder_origin: str) -> str:
    """Insert the bridge before the LAST ``</body>`` (an earlier one may sit inside a
    script string), or append it when the page has none."""
    tag = f'<script id="{_BRIDGE_SCRIPT_ID}">\n{edit_bridge_script(builder_origin)}\n</script>'
    idx = html.lower().rfind("</body>")
    if idx < 0:
        return html + tag
    return html[:idx] + tag + html[idx:]


def inject_runtime_reporter(html: str, builder_origin: str) -> str:
    """Put the reporter first in ``<head>`` so it sees errors from every later script.
    No ``<head>`` tag: right after the doctype (never before it, which would flip the
    page into quirks mode), else at the very start."""
    if f'id="{_REPORTER_SCRIPT_ID}"' in html:
        return html
    tag = f'<script id="{_REPORTER_SCRIPT_ID}">{runtime_reporter_script(builder_origin)}</script>'
    head = _HEAD_OPEN_RE.search(html)
    if head is not None:
        at = head.end()
    else:
        doctype = _DOCTYPE_RE.search(html)
        at = doctype.end() if doctype and not html[: doctype.start()].strip() else 0
    return html[:at] + tag + html[at:]


async def materialize_html_draft(
    source: Mapping[str, Any], builder_origin: str, *, arm: Any = None
) -> dict[str, bytes]:
    """The served file set of an html draft: its source files with the import map and
    the runtime-error reporter in every page, and an ``?paw_edit=1`` variant of each
    page under ``.paw-edit/`` that also carries data-uid stamps and the edit bridge.

    Stamping prefers ``arm`` (paw-sites ``arm-html``); a page it cannot stamp (no
    toolchain on this host, or a refusal) falls back to the Python port
    (``html_uid_stamp``), which produces the same uids or refuses the page. A page
    neither can stamp is served unstamped: picks work, they just carry no uid."""
    from pocketpaw_ee.sites import dependency_manifest as dm
    from pocketpaw_ee.sites.html_uid_stamp import stamp_html_data_uids

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
            files[rel] = inject_runtime_reporter(pages[rel], builder_origin).encode("utf-8")
        else:
            files[rel] = contents.encode("utf-8")

    armed: Mapping[str, Any] = {}
    if pages and arm is not None:
        try:
            out = await arm(source=dict(pages))
            armed = out.get("source") or {}
        except Exception as exc:
            logger.info("sites.preview_origin: arm-html unavailable, using the port (%s)", exc)
    for rel, page in pages.items():
        stamped = armed.get(rel)
        if not isinstance(stamped, str):
            stamped = stamp_html_data_uids(page, rel) or page
        edit_page = inject_runtime_reporter(stamped, builder_origin)
        files[f"{EDIT_VARIANT_DIR}/{rel}"] = inject_edit_bridge(edit_page, builder_origin).encode(
            "utf-8"
        )
    return files


# ---------------------------------------------------------------------------
# The ASGI app
# ---------------------------------------------------------------------------

_draft_cache: OrderedDict[tuple[str, str], dict[str, bytes]] = OrderedDict()
#: token -> (expires at, (pocket, hash) or None for a remembered miss)
_token_cache: OrderedDict[str, tuple[float, tuple[str, str] | None]] = OrderedDict()
_draft_cache_lock = threading.Lock()  # _load_draft runs in worker threads
#: token -> [lock, waiters]: one store read + unpack per token at a time; concurrent
#: requests for the same draft's assets wait for it and then hit the cache.
_inflight: dict[str, list[Any]] = {}


def _content_type(name: str) -> str:
    from pocketpaw_ee.sites.artifact_preview import content_type_for

    suffix = Path(name).suffix.lower()
    if suffix in (".js", ".mjs", ".cjs"):
        return _JS
    return content_type_for(name)


def _cache_control(rel: str, status: int) -> str:
    """Immutable only for content-addressed files (SvelteKit's ``_app/immutable/`` or a
    hashed filename); a plain ``assets/logo.png`` can change under the same URL when
    the draft is re-published. ``private``: a capability URL is not for shared caches."""
    if status != 200:
        return "no-store"
    if rel.startswith("_app/immutable/") or _HASHED_NAME_RE.search(rel.rsplit("/", 1)[-1]):
        return "public, max-age=31536000, immutable"
    return "private, no-cache"


def _clear_caches() -> None:
    """Drop every per-process preview cache (tests; an operator after a store swap)."""
    with _draft_cache_lock:
        _draft_cache.clear()
        _token_cache.clear()


def forget_pocket(pocket_id: str) -> None:
    """Drop this process's cached tokens and unpacked drafts of one pocket (its drafts
    were purged). Other processes stop serving them within ``_TOKEN_HIT_TTL``."""
    with _draft_cache_lock:
        for key in [k for k in _draft_cache if k[0] == pocket_id]:
            del _draft_cache[key]
        for token in [t for t, (_e, ref) in _token_cache.items() if ref and ref[0] == pocket_id]:
            del _token_cache[token]


def _resolve_token(store: Any, token: str) -> tuple[str, str] | None:
    now = time.monotonic()
    with _draft_cache_lock:
        hit = _token_cache.get(token)
        if hit is not None and hit[0] > now:
            _token_cache.move_to_end(token)
            return hit[1]
    ref = store.resolve_preview_token(token)
    ref = (ref[0], ref[1]) if ref is not None else None
    ttl = _TOKEN_HIT_TTL if ref is not None else _TOKEN_MISS_TTL
    with _draft_cache_lock:
        _token_cache[token] = (now + ttl, ref)
        _token_cache.move_to_end(token)
        while len(_token_cache) > _TOKEN_CACHE_ENTRIES:
            _token_cache.popitem(last=False)
    return ref


def _single_flight(token: str) -> threading.Lock:
    with _draft_cache_lock:
        slot = _inflight.setdefault(token, [threading.Lock(), 0])
        slot[1] += 1
        return slot[0]


def _release_flight(token: str) -> None:
    with _draft_cache_lock:
        slot = _inflight.get(token)
        if slot is not None:
            slot[1] -= 1
            if slot[1] <= 0:
                del _inflight[token]


def _load_draft(token: str) -> dict[str, bytes] | None:
    lock = _single_flight(token)
    try:
        with lock:
            return _load_draft_locked(token)
    finally:
        _release_flight(token)


def _load_draft_locked(token: str) -> dict[str, bytes] | None:
    from pocketpaw_ee.sites import service as sites_service

    store = sites_service._default_artifact_store()
    if not store_supports_preview(store):
        return None
    key = _resolve_token(store, token)
    if key is None:
        return None
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
    # unpack_files caps a draft at MAX_TOTAL_BYTES <= _CACHE_BYTES, so it always fits.
    with _draft_cache_lock:
        _draft_cache[key] = files
        total = sum(sum(len(b) for b in f.values()) for f in _draft_cache.values())
        while len(_draft_cache) > 1 and (
            len(_draft_cache) > _CACHE_ENTRIES or total > _CACHE_BYTES
        ):
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

    if await _maybe_proxy(scope, receive, send):
        return
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


def _resolve_ref(token: str) -> tuple[str, str] | None:
    from pocketpaw_ee.sites import service as sites_service

    store = sites_service._default_artifact_store()
    if not store_supports_preview(store):
        return None
    return _resolve_token(store, token)


async def _maybe_proxy(scope: dict[str, Any], receive: Any, send: Any) -> bool:
    """Answer the request from the token's draft Worker when it has one (True), or
    404 a superseded proxied draft (True). False: serve the static draft."""
    from pocketpaw_ee.sites import draft_worker

    if not draft_worker.enabled():
        return False
    host = ""
    for k, v in scope.get("headers") or []:
        if k == b"host":
            host = v.decode("latin-1")
            break
    token = token_from_host(host)
    if not token:
        return False
    ref = await asyncio.to_thread(_resolve_ref, token)
    if ref is None:
        return False
    try:
        target = await draft_worker.proxy_target(*ref)
    except Exception:  # noqa: BLE001 - a registry outage serves the static draft
        logger.warning("sites.preview_origin: draft registry unavailable", exc_info=True)
        return False
    if target is None:
        return False
    head = scope.get("method", "GET").upper() == "HEAD"
    if target is draft_worker.SUPERSEDED:
        plain = list(_BASE_HEADERS) + [
            ("content-type", "text/plain; charset=utf-8"),
            ("cache-control", "no-store"),
        ]
        await _send(send, 404, b"Not found", plain, head)
        return True
    from pocketpaw_ee.sites import preview_proxy

    await preview_proxy.forward(scope, receive, send, target=target, preview_host=host)
    return True


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
    "forget_pocket",
    "inject_edit_bridge",
    "inject_import_map",
    "inject_runtime_reporter",
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
