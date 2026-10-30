# tests/ee/sites/test_audit.py — unit tests for the deterministic site-audit
# engine (sites.audit.audit_pocket_site), BP-7 backend half (the Branch-primitive
# producer 2). Created: 2026-06-18 (feat/branch-primitive-audit).
#
# The audit engine is a PURE function over a site's content (a {path: contents}
# svelte source map, or a rippleSpec dict) — no DB, no network, no LLM — so every
# deterministic check is exercised here over sample content, with a POSITIVE case
# (issue present → exactly one finding for that check, with a usable fix_prompt)
# and a NEGATIVE case (clean content → that check produces no finding). A fully
# clean svelte site returns zero findings.
#
# Checks covered:
#   a11y — img_alt, button_name, link_name, input_label, h1_missing, h1_multiple
#   links — placeholder/malformed href
#   seo  — title, meta_description, open_graph

from __future__ import annotations

from pocketpaw_ee.sites.audit import audit_pocket_site

# A clean, §-complete svelte source map: a single <h1>, every <img> has alt, the
# button/link have accessible text, real hrefs, and a full SEO head (title, meta
# description, og:title + og:image). Audits to ZERO findings — the baseline every
# positive case mutates one issue into.
_CLEAN = {
    "src/app.html": (
        "<!doctype html><html><head>"
        "<title>Bright Smile Dental — Whiter Teeth in 30 Days</title>"
        "<meta name='description' content='Bright Smile Dental gives you a whiter, "
        "healthier smile with gentle, modern dentistry. Book a free consult today.'>"
        "<meta property='og:title' content='Bright Smile Dental'>"
        "<meta property='og:image' content='https://brightsmile.example/og.png'>"
        "</head><body>%sveltekit.body%</body></html>"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css'</script><slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": ":root{--brand:#0A84FF}",
    "src/lib/components/Hero.svelte": (
        "<section class='hero'>"
        "<h1>Brighter Smiles, Whiter Teeth</h1>"
        "<img src='/hero.jpg' alt='A patient smiling after treatment'/>"
        "<a href='/book'>Book a consult</a>"
        "<button>Get started</button>"
        "</section>"
    ),
}


def _by_check(findings, check):
    return [f for f in findings if f["check"] == check]


def test_clean_site_has_no_findings():
    findings = audit_pocket_site(engine="svelte", content=_CLEAN)
    assert findings == [], findings


# ── a11y ────────────────────────────────────────────────────────────────────────
def test_img_without_alt_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = "<section><h1>Hi</h1><img src='/hero.jpg'/></section>"
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "a11y.img_alt")
    assert len(hits) == 1
    f = hits[0]
    assert f["tier"] == "deterministic"
    assert f["severity"] == "error"
    assert f["location"]["file"] == "src/lib/components/Hero.svelte"
    assert "alt" in f["fix_prompt"].lower()
    assert "Hero.svelte" in f["fix_prompt"]


def test_img_with_alt_is_not_flagged():
    # The clean baseline's Hero img carries alt — no img_alt finding.
    findings = audit_pocket_site(engine="svelte", content=_CLEAN)
    assert _by_check(findings, "a11y.img_alt") == []


def test_button_without_accessible_name_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = "<section><h1>Hi</h1><button><svg/></button></section>"
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "a11y.button_name")
    assert len(hits) == 1
    assert hits[0]["severity"] == "error"
    assert "aria-label" in hits[0]["fix_prompt"]


def test_button_with_aria_label_is_not_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = (
        "<section><h1>Hi</h1><button aria-label='Close'><svg/></button></section>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    assert _by_check(findings, "a11y.button_name") == []


def test_icon_only_link_without_name_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = "<section><h1>Hi</h1><a href='/x'><svg/></a></section>"
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "a11y.link_name")
    assert len(hits) == 1
    assert hits[0]["severity"] == "error"


def test_link_with_text_is_not_flagged():
    findings = audit_pocket_site(engine="svelte", content=_CLEAN)
    assert _by_check(findings, "a11y.link_name") == []


def test_input_without_label_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Form.svelte"] = "<form><input name='email' type='email'/></form>"
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "a11y.input_label")
    assert len(hits) == 1
    assert hits[0]["severity"] == "warning"
    assert "label" in hits[0]["fix_prompt"].lower()


def test_input_with_label_is_not_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Form.svelte"] = (
        "<form><label for='email'>Email</label><input id='email' type='email'/></form>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    assert _by_check(findings, "a11y.input_label") == []


def test_missing_h1_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = (
        "<section><h2>No top heading here</h2>"
        "<img src='/h.jpg' alt='x'/><a href='/b'>Book</a><button>Go</button></section>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "a11y.h1_missing")
    assert len(hits) == 1
    assert hits[0]["severity"] == "warning"


def test_multiple_h1_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = (
        "<section><h1>One</h1><h1>Two</h1>"
        "<img src='/h.jpg' alt='x'/><a href='/b'>Book</a><button>Go</button></section>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "a11y.h1_multiple")
    assert len(hits) == 1
    assert "2" in hits[0]["message"]


# ── links ────────────────────────────────────────────────────────────────────────
def test_empty_href_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = "<section><h1>Hi</h1><a href=''>Dead</a></section>"
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "links.placeholder")
    assert len(hits) == 1
    assert "href" in hits[0]["fix_prompt"].lower()


def test_hash_href_is_flagged():
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = "<section><h1>Hi</h1><a href='#'>Top</a></section>"
    findings = audit_pocket_site(engine="svelte", content=src)
    assert len(_by_check(findings, "links.placeholder")) == 1


def test_real_href_is_not_flagged():
    findings = audit_pocket_site(engine="svelte", content=_CLEAN)
    assert _by_check(findings, "links.placeholder") == []


def test_svelte_dynamic_href_is_not_flagged():
    # href={url} is resolved at render — not a placeholder.
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = (
        "<section><h1>Hi</h1><a href={url}>Dynamic</a></section>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    assert _by_check(findings, "links.placeholder") == []


# ── SEO ──────────────────────────────────────────────────────────────────────────
def test_missing_title_is_flagged():
    src = dict(_CLEAN)
    src["src/app.html"] = (
        "<!doctype html><html><head>"
        "<meta name='description' content='A real description that is present.'>"
        "<meta property='og:title' content='x'><meta property='og:image' content='y'>"
        "</head><body></body></html>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "seo.title")
    assert len(hits) == 1
    assert hits[0]["severity"] == "error"


def test_missing_meta_description_is_flagged():
    src = dict(_CLEAN)
    src["src/app.html"] = (
        "<!doctype html><html><head><title>Has a title</title>"
        "<meta property='og:title' content='x'><meta property='og:image' content='y'>"
        "</head><body></body></html>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    assert len(_by_check(findings, "seo.meta_description")) == 1


def test_missing_open_graph_is_flagged():
    src = dict(_CLEAN)
    src["src/app.html"] = (
        "<!doctype html><html><head><title>Has a title</title>"
        "<meta name='description' content='A real description that is present here.'>"
        "</head><body></body></html>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    hits = _by_check(findings, "seo.open_graph")
    assert len(hits) == 1
    # Both og:title and og:image are missing → named in the message.
    assert "og:title" in hits[0]["message"]
    assert "og:image" in hits[0]["message"]


def test_full_seo_head_is_not_flagged():
    findings = audit_pocket_site(engine="svelte", content=_CLEAN)
    assert _by_check(findings, "seo.title") == []
    assert _by_check(findings, "seo.meta_description") == []
    assert _by_check(findings, "seo.open_graph") == []


# ── engine handling ──────────────────────────────────────────────────────────────
def test_ripple_engine_scans_flattened_html_for_placeholder_links():
    # A ripple site has no hand-written component markup, so the markup-SHAPE a11y
    # checks (img alt, h1, button name) don't run. The text-level link check is
    # attribute-based: it fires when a ripple node carries literal HTML with an
    # href= attribute (e.g. an html/embed widget), which the flatten surfaces.
    spec = {"type": "html", "content": '<a href="#">Click</a>'}
    findings = audit_pocket_site(engine="ripple", content=spec)
    assert _by_check(findings, "links.placeholder")
    # The svelte-only markup-shape a11y checks did NOT fire on a ripple spec.
    assert _by_check(findings, "a11y.img_alt") == []
    assert _by_check(findings, "a11y.h1_missing") == []


def test_ripple_structured_url_field_is_not_a_link_finding():
    # A URL stored as a STRUCTURED value ("href": "#"), not as an HTML attribute,
    # is not markup — the deterministic link check (attribute-based) does not flag
    # it. Documents the boundary: structured-spec URL validation is out of the
    # deterministic core's scope (a future judgment/spec-aware tier could add it).
    spec = {"type": "container", "children": [{"label": "Click", "href": "#"}]}
    findings = audit_pocket_site(engine="ripple", content=spec)
    assert _by_check(findings, "links.placeholder") == []


def test_none_content_is_safe():
    assert audit_pocket_site(engine="svelte", content=None) == []
    assert audit_pocket_site(engine="ripple", content=None) == []


def test_finding_ids_are_unique_per_run():
    # Two imgs without alt → two distinct finding ids.
    src = dict(_CLEAN)
    src["src/lib/components/Hero.svelte"] = (
        "<section><h1>Hi</h1><img src='/a.jpg'/><img src='/b.jpg'/></section>"
    )
    findings = audit_pocket_site(engine="svelte", content=src)
    img_hits = _by_check(findings, "a11y.img_alt")
    assert len(img_hits) == 2
    assert len({f["id"] for f in findings}) == len(findings)
