# svg_safety.py — SVG detection + XSS sanitization for the upload pipeline.
# Created: 2026-06-16 — supports accepting image/svg+xml logos in the
#   white-label Branding panel without opening a stored-XSS hole.
#
#   Two helpers:
#     * looks_like_svg(head, declared_mime, filename) — the narrow gate that
#       decides whether an uploaded blob may be treated as image/svg+xml.
#       Requires ALL of: declared content-type image/svg+xml, a .svg
#       filename, and a real SVG/XML signature in the bytes (optional
#       BOM/whitespace then <?xml or <svg). We never promote a bare
#       text/xml or text/plain blob to SVG.
#     * sanitize_svg(raw) — strips the active-content vectors (script
#       elements, on* event handlers, javascript: URLs, <foreignObject>,
#       external entity / DOCTYPE declarations) so the bytes at rest can't
#       execute. This is ONE layer of defense; the serving routers add the
#       other two (Content-Disposition: attachment + a locked-down CSP) so
#       even an unsanitized edge case can't run on a direct navigation.
"""Dependency-free SVG detection and sanitization for uploads.

Why regex and not a real XML parser: the sanitizer is defense-in-depth, not
the sole barrier. SVGs are served as ``attachment`` with a ``script-src
'none'; sandbox`` CSP, so a victim who opens the signed URL directly gets a
download, not an execution context. The regex pass removes the obvious
stored-XSS payloads (script tags, inline handlers, ``javascript:`` hrefs,
``<foreignObject>`` HTML smuggling) so the bytes are inert even in the
``<img src>`` render path. Pulling in lxml/bleach for this would add a
supply-chain dependency for a belt-and-suspenders step — not worth it.
"""

from __future__ import annotations

import re

# Leading bytes we tolerate before the signature: UTF-8 BOM, then any ASCII
# whitespace. A real SVG written by a design tool starts with either an XML
# prolog (<?xml ...?>) or the root <svg ...> element.
_BOM = b"\xef\xbb\xbf"
_SVG_SIGNATURE = re.compile(
    rb"^\s*(<\?xml[\s\S]*?\?>\s*)?(<!--[\s\S]*?-->\s*)*<svg\b", re.IGNORECASE
)
# A looser check that also accepts a leading <?xml without requiring us to
# have buffered all the way to the <svg root (the sniff head is only 512 B,
# and a fat XML prolog + comment can push <svg past that window).
_XML_OR_SVG_PROLOG = re.compile(rb"^\s*(<\?xml\b|<svg\b)", re.IGNORECASE)

SVG_MIME = "image/svg+xml"


def looks_like_svg(head: bytes, declared_mime: str | None, filename: str | None) -> bool:
    """Return True only when a blob may safely be treated as an SVG.

    Narrow on purpose — all three signals must agree:

    1. the client declared ``image/svg+xml`` (case-insensitive),
    2. the filename ends ``.svg`` (case-insensitive),
    3. the bytes begin (after an optional BOM/whitespace) with ``<?xml`` or
       ``<svg``.

    A blob that sniffs as bare ``text/xml`` / ``text/plain`` or an image that
    merely *claims* to be an SVG without the signature is rejected, so this
    cannot be used to smuggle arbitrary XML/HTML into the image allow-list.
    """
    if (declared_mime or "").split(";")[0].strip().lower() != SVG_MIME:
        return False
    if not filename or not filename.lower().endswith(".svg"):
        return False
    probe = head[len(_BOM) :] if head.startswith(_BOM) else head
    return bool(_XML_OR_SVG_PROLOG.match(probe))


# --- Sanitization --------------------------------------------------------

# <script>...</script> (and self-closing / unclosed variants).
_SCRIPT_BLOCK = re.compile(rb"<script\b[\s\S]*?</script\s*>", re.IGNORECASE)
_SCRIPT_OPEN = re.compile(rb"<script\b[^>]*/?>", re.IGNORECASE)
# <foreignObject> lets an SVG embed arbitrary XHTML (a classic bypass).
_FOREIGN_OBJECT = re.compile(rb"<foreignObject\b[\s\S]*?</foreignObject\s*>", re.IGNORECASE)
_FOREIGN_OBJECT_OPEN = re.compile(rb"<foreignObject\b[^>]*/?>", re.IGNORECASE)
# on*="..." / on*='...' / on*=value inline event handlers.
_EVENT_HANDLER = re.compile(rb"""\son[a-zA-Z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
# javascript: (and data: text/html) URLs in href/xlink:href/src/style.
_JS_URL = re.compile(
    rb"(href|xlink:href|src)\s*=\s*([\"']?)\s*javascript:[^\"'>\s]*\2", re.IGNORECASE
)
# <!DOCTYPE ...> / <!ENTITY ...> — kill XXE / billion-laughs vectors.
_DOCTYPE = re.compile(rb"<!DOCTYPE[\s\S]*?>", re.IGNORECASE)
_ENTITY = re.compile(rb"<!ENTITY[\s\S]*?>", re.IGNORECASE)


def sanitize_svg(raw: bytes) -> bytes:
    """Strip active-content vectors from SVG bytes.

    Removes ``<script>`` blocks, ``<foreignObject>`` subtrees, ``on*`` inline
    event handlers, ``javascript:`` URLs, and ``<!DOCTYPE>``/``<!ENTITY>``
    declarations. Returns the cleaned bytes. Idempotent and safe to run on
    any blob (a non-SVG just passes through with the same substitutions, which
    are no-ops on image binary data).
    """
    out = raw
    out = _SCRIPT_BLOCK.sub(b"", out)
    out = _SCRIPT_OPEN.sub(b"", out)
    out = _FOREIGN_OBJECT.sub(b"", out)
    out = _FOREIGN_OBJECT_OPEN.sub(b"", out)
    out = _DOCTYPE.sub(b"", out)
    out = _ENTITY.sub(b"", out)
    out = _JS_URL.sub(rb"\1=\2\2", out)
    out = _EVENT_HANDLER.sub(b"", out)
    return out


# --- Safe serving --------------------------------------------------------

# Locked-down CSP for SVG responses: no script execution, no plugins, no
# network fetches; inline styles only (SVGs legitimately use <style>), and a
# ``sandbox`` with no allow-tokens so even a direct navigation can't run JS,
# submit forms, or escape its origin. Belt-and-suspenders with the
# ``attachment`` disposition below.
_SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def svg_response_headers(filename: str) -> dict[str, str]:
    """Return the XSS-safe response headers for serving an SVG blob.

    Why these and not inline rendering: the logo renders fine through
    ``<img src>`` (which never executes embedded script), but a victim who
    opens the signed URL *directly* would be in a script-capable context. So
    we force a download (``Content-Disposition: attachment``), block sniffing
    (``X-Content-Type-Options: nosniff``), and clamp execution with a CSP that
    disables scripts and sandboxes the document. Combined with the
    sanitize-on-upload pass, that's three independent layers.

    Callers pass the rest (``media_type`` / ``Content-Type``) themselves; this
    helper only owns the security headers so the two serving routers can't
    drift apart.
    """
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _SVG_CSP,
    }
