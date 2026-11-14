# ee/pocketpaw_ee/sites/paw_client.py — paw.js (the integrations gateway client) for
# project sites whose pages pocketpaw serves or deploys without the generator
# touching them: a no-build draft's preview and every project publish.
#
# A Python mirror of paw-sites ``src/paw-client.ts`` (``pawClientConfig``,
# ``usesPawClient``, ``injectPawClient``) and of ``project-build.ts``'s source scan
# (``projectSourceTexts``), so a page tagged here is byte-identical to one the
# generator tagged, and a page the generator already tagged is left alone (the
# injection is idempotent). The rule is the generator's: the client ships only when
# the site has capture config (api base, site id, signed key) AND its authored source
# uses it. Sites that never touch integrations stay byte-identical.
#
# The client bytes are read from the vendored generator's ``templates/_paw/paw.js``
# (``project_build.generator_dir``). Without them nothing is injected: a tag pointing
# at a missing file would only 404 in the visitor's browser.
#
# Pages a Worker renders on request are never touched here; those import the client
# through the ``paw-integrations`` recipe. Attribute values are HTML-escaped; the site
# key is public (the same signed key the lead-capture form already ships).
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

#: Where the client is served from, and where it sits in a static output tree.
CLIENT_SRC = "/_paw/paw.js"
CLIENT_REL = "_paw/paw.js"
#: The prebuilt client inside the vendored generator.
CLIENT_TEMPLATE_REL = "templates/_paw/paw.js"

_USES_RE = re.compile(
    r"data-paw-(?:checkout|subscribe)\b|\bPaw\s*\??\.\s*(?:call|checkout|subscribe)\b"
    r"|paw:result|_paw/paw\.js"
)
_EXISTING_TAG_RE = re.compile(
    r"""<script\b[^>]*\bsrc\s*=\s*["']?(?:\.?/)?_paw/paw\.js["'\s>][^>]*>""", re.IGNORECASE
)
_DATA_API_RE = re.compile(r"\bdata-api\s*=", re.IGNORECASE)
_HTML_RE = re.compile(r"\.html?$", re.IGNORECASE)

#: ``projectSourceTexts``: authored page, script and component files ...
_SOURCE_EXT_RE = re.compile(r"\.(?:html?|[cm]?[jt]sx?|svelte|astro|vue|mdx?)$", re.IGNORECASE)
#: ... minus docs that describe the client without using it ...
_DOC_NAMES = frozenset({"AGENTS.md", "README.md", "CLAUDE.md"})
#: ... and anything under installs or build output (paw-sites ``IGNORED_DIRS``).
_IGNORED_DIRS = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        ".astro",
        ".svelte-kit",
        ".output",
        ".open-next",
        ".wrangler",
        ".vinxi",
        ".paw",
        ".git",
    }
)
_MAX_SCANNED_BYTES = 1_000_000


def client_config(site_config: Mapping[str, Any] | None) -> dict[str, str] | None:
    """``pawClientConfig``: the tag config from a generator ``siteConfig``
    (``captureApiBase``, ``siteId``, ``captureSignedKey``, ``turnstileSiteKey``), or
    ``None`` when the site has no capture config to call the gateway with."""
    if not isinstance(site_config, Mapping):
        return None

    def _s(key: str) -> str:
        value = site_config.get(key)
        return value.strip() if isinstance(value, str) else ""

    api = _s("captureApiBase").rstrip("/")
    site_id, site_key = _s("siteId"), _s("captureSignedKey")
    if not api or not site_id or not site_key:
        return None
    cfg = {"api": api, "siteId": site_id, "siteKey": site_key}
    if turnstile := _s("turnstileSiteKey"):
        cfg["turnstileSiteKey"] = turnstile
    return cfg


def uses_client(texts: Iterable[str]) -> bool:
    """True if any of the given source texts uses the client."""
    return any(_USES_RE.search(t) for t in texts)


def source_texts(files: Mapping[str, Any]) -> list[str]:
    """The authored sources of a project file map the usage scan reads."""
    out: list[str] = []
    for rel, data in files.items():
        segments = rel.split("/")
        if any(seg in _IGNORED_DIRS for seg in segments[:-1]):
            continue
        name = segments[-1]
        if not _SOURCE_EXT_RE.search(name) or name in _DOC_NAMES or rel.endswith(CLIENT_REL):
            continue
        if isinstance(data, bytes):
            if len(data) > _MAX_SCANNED_BYTES:
                continue
            data = data.decode("utf-8", "replace")
        if isinstance(data, str) and len(data.encode("utf-8")) <= _MAX_SCANNED_BYTES:
            out.append(data)
    return out


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _config_attrs(cfg: Mapping[str, str]) -> str:
    pairs = (
        ("data-api", cfg.get("api")),
        ("data-site-id", cfg.get("siteId")),
        ("data-site-key", cfg.get("siteKey")),
        ("data-turnstile-sitekey", cfg.get("turnstileSiteKey")),
    )
    return "".join(f' {k}="{_escape_attr(v)}"' for k, v in pairs if v)


def client_tag(cfg: Mapping[str, str]) -> str:
    return f'<script src="{CLIENT_SRC}"{_config_attrs(cfg)} defer></script>'


def inject(html: str, cfg: Mapping[str, str]) -> str:
    """``injectPawClient``: add the tag before the LAST ``</body>`` (or append it). A
    page that already loads the client keeps its tag; one without ``data-api`` gets
    the config spliced in after ``<script``."""
    existing = _EXISTING_TAG_RE.search(html)
    if existing:
        if _DATA_API_RE.search(existing.group(0)):
            return html
        at = existing.start() + len("<script")
        return html[:at] + _config_attrs(cfg) + html[at:]
    tag = client_tag(cfg)
    at = html.rfind("</body>")
    return f"{html[:at]}{tag}\n{html[at:]}" if at >= 0 else f"{html}\n{tag}\n"


def client_bytes() -> bytes | None:
    """The prebuilt client from the vendored generator, or ``None`` without one."""
    from pocketpaw_ee.sites.project_build import generator_dir

    root = generator_dir()
    path = root / CLIENT_TEMPLATE_REL if root is not None else None
    if path is None or not path.is_file():
        return None
    return path.read_bytes()


def apply_to_assets(
    assets: MutableMapping[str, bytes],
    cfg: Mapping[str, str] | None,
    texts: Iterable[str],
    *,
    client: bytes | None = None,
) -> bool:
    """``applyPawClientToAssets`` over an in-memory assets tree (``{rel: bytes}``,
    rooted at the served dir): tag every .html page and add ``_paw/paw.js`` when the
    site qualifies. ``client`` defaults to :func:`client_bytes`. Returns whether the
    client shipped."""
    if not cfg or not uses_client(texts):
        return False
    body = client if client is not None else client_bytes()
    if body is None:
        logger.warning("sites.paw_client: no vendored paw.js; the site ships without it")
        return False
    for rel, data in list(assets.items()):
        if not _HTML_RE.search(rel):
            continue
        html = data.decode("utf-8", "replace")
        tagged = inject(html, cfg)
        if tagged != html:
            assets[rel] = tagged.encode("utf-8")
    assets[CLIENT_REL] = body
    return True


__all__ = [
    "CLIENT_REL",
    "CLIENT_SRC",
    "CLIENT_TEMPLATE_REL",
    "apply_to_assets",
    "client_bytes",
    "client_config",
    "client_tag",
    "inject",
    "source_texts",
    "uses_client",
]
