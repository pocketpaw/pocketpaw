# test_svg_safety.py — unit tests for the SVG detection + sanitization helpers.
# Created: 2026-06-16 — covers looks_like_svg (the narrow accept gate),
#   sanitize_svg (strips script/handlers/foreignObject/javascript:/DOCTYPE),
#   and svg_response_headers (attachment + nosniff + locked-down CSP).
from __future__ import annotations

from pocketpaw.uploads.svg_safety import (
    looks_like_svg,
    sanitize_svg,
    svg_response_headers,
)

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
SVG_PROLOG = b'<?xml version="1.0" encoding="UTF-8"?>\n<svg><circle/></svg>'
SVG_BOM = b"\xef\xbb\xbf<svg><rect/></svg>"


class TestLooksLikeSvg:
    def test_plain_svg_accepted(self):
        assert looks_like_svg(SVG, "image/svg+xml", "logo.svg") is True

    def test_xml_prolog_accepted(self):
        assert looks_like_svg(SVG_PROLOG, "image/svg+xml", "logo.svg") is True

    def test_bom_prefix_accepted(self):
        assert looks_like_svg(SVG_BOM, "image/svg+xml", "logo.svg") is True

    def test_content_type_with_charset_accepted(self):
        assert looks_like_svg(SVG, "image/svg+xml; charset=utf-8", "logo.svg") is True

    def test_uppercase_extension_accepted(self):
        assert looks_like_svg(SVG, "image/svg+xml", "LOGO.SVG") is True

    def test_wrong_content_type_rejected(self):
        # Bare text/xml must never be promoted to SVG.
        assert looks_like_svg(SVG, "text/xml", "logo.svg") is False
        assert looks_like_svg(SVG, "text/plain", "logo.svg") is False

    def test_wrong_extension_rejected(self):
        assert looks_like_svg(SVG, "image/svg+xml", "logo.txt") is False

    def test_missing_filename_rejected(self):
        assert looks_like_svg(SVG, "image/svg+xml", None) is False

    def test_non_svg_bytes_rejected(self):
        assert looks_like_svg(b"GIF89a junk", "image/svg+xml", "logo.svg") is False
        assert looks_like_svg(b"\x89PNG\r\n", "image/svg+xml", "logo.svg") is False


class TestSanitizeSvg:
    def test_strips_script_block(self):
        out = sanitize_svg(b"<svg><script>alert(1)</script><rect/></svg>")
        assert b"<script" not in out.lower()
        assert b"alert" not in out
        assert b"<rect" in out.lower()

    def test_strips_self_closing_script(self):
        out = sanitize_svg(b'<svg><script src="x.js"/><rect/></svg>')
        assert b"<script" not in out.lower()

    def test_strips_event_handlers(self):
        out = sanitize_svg(b"<svg onload=\"alert(1)\"><rect onclick='evil()'/></svg>")
        low = out.lower()
        assert b"onload" not in low
        assert b"onclick" not in low

    def test_strips_javascript_urls(self):
        out = sanitize_svg(b'<svg><a href="javascript:alert(1)">x</a></svg>')
        assert b"javascript:" not in out.lower()

    def test_strips_foreign_object(self):
        out = sanitize_svg(b"<svg><foreignObject><body>html</body></foreignObject><rect/></svg>")
        assert b"<foreignobject" not in out.lower()
        assert b"<body" not in out.lower()

    def test_strips_doctype_and_entity(self):
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<svg><rect/></svg>"
        )
        out = sanitize_svg(payload)
        low = out.lower()
        assert b"<!doctype" not in low
        assert b"<!entity" not in low

    def test_clean_svg_passes_through(self):
        out = sanitize_svg(SVG)
        assert out == SVG

    def test_idempotent(self):
        dirty = b'<svg onload="x()"><script>y()</script><rect/></svg>'
        once = sanitize_svg(dirty)
        twice = sanitize_svg(once)
        assert once == twice


class TestSvgResponseHeaders:
    def test_forces_attachment(self):
        h = svg_response_headers("logo.svg")
        assert h["Content-Disposition"].startswith("attachment")
        assert "logo.svg" in h["Content-Disposition"]

    def test_blocks_sniffing(self):
        assert svg_response_headers("logo.svg")["X-Content-Type-Options"] == "nosniff"

    def test_csp_blocks_script_and_sandboxes(self):
        csp = svg_response_headers("logo.svg")["Content-Security-Policy"]
        # No script source allowed, and a sandbox with no escape tokens.
        assert "default-src 'none'" in csp
        assert "sandbox" in csp
        assert "script-src" not in csp or "script-src 'none'" in csp
