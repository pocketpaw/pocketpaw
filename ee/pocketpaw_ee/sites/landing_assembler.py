# ee/pocketpaw_ee/sites/landing_assembler.py — the DETERMINISTIC Paw Site
# landing-page assembler. A pure function that turns an LLM-provided COPY object
# into the fixed marketing-widget rippleSpec. The LLM provides words; CODE owns
# the structure, so the page can NEVER be downgraded to generic
# hero+grid+card+quote widgets the way the agent-mode pocket_specialist path was.
#
# Created: 2026-06-04 (feat/sites-deterministic-fastpath). This is the keystone
# of the deterministic fast-path: the create-paw-site SKILL no longer hand-
# composes a rippleSpec or routes through ``pocket_specialist__create`` (whose
# agent-mode draft/redraft/subagent-delegation loop kept dropping the marketing
# widgets). Instead the skill produces ONLY the ``content`` copy object and calls
# ``mcp__pocketpaw_sites_manager__create_landing_site``, which runs this
# assembler and persists the result directly.
#
# Updated 2026-06-09 (feat/landing-assembler-enrich — premium ripple marketing
# pack, ripple PR #67):
#   * The hero is now the bespoke ``marketing-hero`` widget (was the borrowed
#     dashboard ``hero``). marketing-hero bakes in its OWN premium CSS visual — an
#     asymmetric panel with a dot-grid texture, a slow CSS-@keyframes glow drift,
#     and a static spec chip — all pure CSS, so it renders fully under csr=false
#     (the resting state is complete with JS off; the drift is enhancement, not
#     structure, and reduced-motion disables it). The borrowed ``hero`` dropped
#     its CTA on the floor; marketing-hero carries a primary CTA (→ #book) plus an
#     optional secondary "see services" ghost CTA (→ #services).
#   * NEW optional FAQ section (the ``faq`` widget) between pricing and the CTA
#     band — native <details>/<summary> disclosures that expand with ZERO client
#     JS (JS-off safe), fed from a new ``faqs`` copy field. Omitted entirely when
#     no faqs are supplied (the funnel stays lean).
#   * csr/motion stance: these sites prerender with ``csr=false`` (verified in the
#     generator's templates/src/routes/+layout.ts.tmpl). So JS-driven motion
#     (ripple ``reveal``/``parallax``/``spotlight``) NEVER attaches and is NOT
#     wired — it would be dead weight. All premium richness here is CSS-only
#     (baked into marketing-hero) and therefore static-safe. Enabling hydration
#     (csr=true) is what would unlock scroll-reveal motion; that is out of scope.
#
# The emitted structure mirrors the renderer-VALID bundled landing skeleton
# (``src/pocketpaw/bundled_templates/_bundled/landing-page/ripple_spec.json``)
# section-for-section — same SSR-safe props — but the hero is upgraded to
# marketing-hero, an optional FAQ is added, every copy value comes from
# ``content``, and the variable-length collections (services / testimonials /
# tiers / faqs) drive real loops. The fixed conversion order is:
#
#   navbar → marketing-hero → section#services[feature-grid] →
#   section#reviews[testimonial* (+ optional logo-cloud)] →
#   section#pricing[pricing-table.tiers] → section#faq[faq] (optional) →
#   cta band → card#book[flat input/textarea/button lead form] → footer
#
# Hard-coded SSR contract (all by construction, never the LLM's choice):
#   * section/card ``id`` anchors (#services/#reviews/#pricing/#faq/#book) —
#     marketing widgets carry no id of their own, so anchor targets live on the
#     wrappers.
#   * input/textarea ``name`` POST fields so the native outer-form submit maps a
#     lead (rule 1: FLAT inputs, never a nested ``form``/``newsletter`` widget).
#   * anchor ``href`` CTAs everywhere (navbar.ctaHref, marketing-hero.ctaHref +
#     secondaryCtaHref, cta.href, footer/nav link hrefs) — never ``on_click``
#     (rule 4: a click handler is dead on a static page).
#   * pricing-table uses ``tiers`` (rule 2), currency is the ``$`` symbol, tier
#     ``cta`` is a string label.
#   * FAQ uses the ``faq`` widget (native <details>), NEVER ``accordion`` (rule 3:
#     a bits-ui accordion won't open with JS off).

from __future__ import annotations

from typing import Any

# Anchor ids the navbar / CTAs / footer link to. The wrapping section/card
# carries the id because marketing widgets have none of their own.
_ANCHOR_SERVICES = "services"
_ANCHOR_REVIEWS = "reviews"
_ANCHOR_PRICING = "pricing"
_ANCHOR_FAQ = "faq"
_ANCHOR_BOOK = "book"

# A small default icon rotation for services that omit an ``icon``, so a
# feature-grid never renders an empty glyph slot. Lucide icon names.
_DEFAULT_SERVICE_ICONS = ("sparkles", "shield", "smile", "star", "zap", "heart")


def _s(value: Any, default: str = "") -> str:
    """Coerce a copy field to a stripped string, falling back to ``default``.

    The assembler is defensive: the LLM provides copy and may omit or mistype a
    field. A missing headline becomes an empty string rather than ``None`` (which
    the renderer would print literally), so the page always renders.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    return str(value)


def _navbar(content: dict[str, Any]) -> dict[str, Any]:
    brand = _s(content.get("brand"), "Your Business")
    cta_label = _s((content.get("hero") or {}).get("cta_label"), "Get in touch")
    return {
        "type": "navbar",
        "props": {
            "brand": brand,
            "links": [
                {"label": "Services", "href": f"#{_ANCHOR_SERVICES}"},
                {"label": "Reviews", "href": f"#{_ANCHOR_REVIEWS}"},
                {"label": "Pricing", "href": f"#{_ANCHOR_PRICING}"},
                {"label": "Book", "href": f"#{_ANCHOR_BOOK}"},
            ],
            # ``cta`` is a STRING label; the destination is the SEPARATE
            # ``ctaHref`` prop (never nested in ``cta``).
            "cta": cta_label,
            "ctaHref": f"#{_ANCHOR_BOOK}",
            "sticky": True,
        },
    }


def _hero(content: dict[str, Any]) -> dict[str, Any]:
    """The page opener — the bespoke ``marketing-hero`` (NOT the borrowed
    dashboard ``hero``).

    marketing-hero carries the CTAs the old ``hero`` dropped: a primary CTA wired
    to the lead-form anchor (``#book``) and an optional secondary "see services"
    ghost CTA (``#services``). ``visual='grid'`` selects the premium asymmetric
    panel (CSS dot-grid + a slow CSS-keyframes glow drift + a static spec chip);
    it is pure CSS, so the panel renders as a finished composition under
    ``csr=false`` with no client JS. ``align='left'`` keeps the split layout (the
    widget forces center only when ``visual='plain'``). All four props are SSR-
    inert — the only motion is the declarative CSS drift, disabled under
    prefers-reduced-motion.
    """
    hero = content.get("hero") or {}
    cta_label = _s(hero.get("cta_label"), "Get in touch")
    props: dict[str, Any] = {
        "eyebrow": _s(hero.get("eyebrow")),
        "title": _s(hero.get("title"), "A headline that sells"),
        "subtitle": _s(hero.get("subtitle")),
        # Primary CTA → the lead form. ``cta`` is a STRING label; the destination
        # is the SEPARATE ``ctaHref`` (never nested) — same split as the navbar.
        "cta": cta_label,
        "ctaHref": f"#{_ANCHOR_BOOK}",
        # Secondary ghost CTA jumps to the services section, which always renders.
        "secondaryCta": "See our services",
        "secondaryCtaHref": f"#{_ANCHOR_SERVICES}",
        # Premium CSS visual panel; static-safe (no client JS needed to paint it).
        "visual": "grid",
        "align": "left",
    }
    return {"type": "marketing-hero", "props": props}


def _services_section(content: dict[str, Any]) -> dict[str, Any]:
    services = content.get("services") or []
    features: list[dict[str, Any]] = []
    for i, svc in enumerate(services):
        if not isinstance(svc, dict):
            continue
        icon = _s(svc.get("icon")) or _DEFAULT_SERVICE_ICONS[i % len(_DEFAULT_SERVICE_ICONS)]
        features.append(
            {
                "icon": icon,
                "title": _s(svc.get("title"), f"Service {i + 1}"),
                "description": _s(svc.get("desc") or svc.get("description")),
            }
        )
    # Column count tracks the item count (capped at 4) so a short list doesn't
    # leave gaping empty columns and a long one wraps cleanly.
    columns = max(2, min(4, len(features))) if features else 3
    return {
        "type": "section",
        "props": {"id": _ANCHOR_SERVICES},
        "children": [
            {
                "type": "feature-grid",
                "props": {"columns": columns, "features": features},
            }
        ],
    }


def _reviews_section(content: dict[str, Any]) -> dict[str, Any]:
    testimonials = content.get("testimonials") or []
    children: list[dict[str, Any]] = []
    # ONE testimonial widget per quote — there is no ``items`` array.
    for t in testimonials:
        if not isinstance(t, dict):
            continue
        children.append(
            {
                "type": "testimonial",
                "props": {
                    "quote": _s(t.get("quote")),
                    "author": _s(t.get("author")),
                    "role": _s(t.get("role")),
                },
            }
        )
    # Optional text-mode logo-cloud (rule 6): only when the copy gives a trust
    # heading, and ALWAYS with an empty ``logos`` list — never invented
    # ``src`` paths (they render as broken images on the live site).
    proof = content.get("proof") or {}
    trust_heading = _s(proof.get("logos_heading") or content.get("logos_heading"))
    if trust_heading:
        children.append(
            {
                "type": "logo-cloud",
                "props": {"heading": trust_heading, "logos": []},
            }
        )
    return {
        "type": "section",
        "props": {"id": _ANCHOR_REVIEWS},
        "children": children,
    }


def _pricing_section(content: dict[str, Any]) -> dict[str, Any]:
    tiers_in = content.get("tiers") or []
    tiers: list[dict[str, Any]] = []
    for i, tier in enumerate(tiers_in):
        if not isinstance(tier, dict):
            continue
        # ``features`` accepts Array<string>; pass strings straight through.
        raw_features = tier.get("features") or []
        features = [_s(f) for f in raw_features if _s(f)]
        out: dict[str, Any] = {
            "id": _s(tier.get("id")) or f"tier-{i + 1}",
            "name": _s(tier.get("name"), f"Plan {i + 1}"),
            # price is a string or number; keep the LLM's value, stringified.
            "price": _s(tier.get("price")),
            "period": _s(tier.get("period")),
            "features": features,
            # tier ``cta`` is a STRING button label (not an object).
            "cta": _s(tier.get("cta_label") or tier.get("cta"), "Get started"),
        }
        if tier.get("popular"):
            out["popular"] = True
        tiers.append(out)
    return {
        "type": "section",
        "props": {"id": _ANCHOR_PRICING},
        "children": [
            {
                "type": "pricing-table",
                # ``currency`` is the SYMBOL, not a code; the required array prop
                # is ``tiers`` (never ``plans``/``columns``).
                "props": {"currency": "$", "tiers": tiers},
            }
        ],
    }


def _faq_section(content: dict[str, Any]) -> dict[str, Any] | None:
    """The optional FAQ section — the ``faq`` widget wrapped in ``section#faq``.

    Built from ``content['faqs']`` (a list of ``{question, answer}``). Returns
    ``None`` when no usable Q/A pairs are supplied, so the funnel stays lean
    rather than rendering an empty disclosure list. The ``faq`` widget emits
    native ``<details>``/``<summary>`` — it expands with ZERO client JS, so the
    answers live in the markup and a JS-off visitor reads the full section (rule
    3: never an ``accordion``, which only opens under client JS). Placed between
    pricing and the CTA band: a visitor weighing the price gets their objections
    answered immediately before the final ask.
    """
    faqs_in = content.get("faqs") or []
    items: list[dict[str, str]] = []
    for faq in faqs_in:
        if not isinstance(faq, dict):
            continue
        question = _s(faq.get("question") or faq.get("q"))
        answer = _s(faq.get("answer") or faq.get("a"))
        # A disclosure with no question is meaningless; require at least the
        # question (an empty answer would still render an openable-but-blank row,
        # so require both for a useful entry).
        if not question or not answer:
            continue
        items.append({"question": question, "answer": answer})
    if not items:
        return None
    title = _s(content.get("faq_title"), "Questions, answered")
    return {
        "type": "section",
        "props": {"id": _ANCHOR_FAQ},
        "children": [
            {
                "type": "faq",
                "props": {"title": title, "items": items},
            }
        ],
    }


def _cta_band(content: dict[str, Any]) -> dict[str, Any]:
    band = content.get("cta_band") or {}
    return {
        "type": "cta",
        "props": {
            # ``cta`` uses ``headline`` (not title) / ``subtext`` (not subtitle) /
            # ``button`` string label / ``href`` destination (sibling, not nested).
            "headline": _s(band.get("headline"), "Ready to get started?"),
            "subtext": _s(band.get("subtext")),
            "button": _s(band.get("button_label") or band.get("button"), "Get in touch"),
            "href": f"#{_ANCHOR_BOOK}",
            "align": "center",
        },
    }


def _lead_form_card(content: dict[str, Any]) -> dict[str, Any]:
    """The FLAT lead-capture form (SSR rule 1).

    Flat ``input`` / ``textarea`` / ``button{type:submit}`` placed directly in a
    ``card`` — NEVER a ``form`` or ``newsletter`` widget (those emit a nested
    ``<form>`` that the browser drops inside the site template's outer form, so
    the visitor's submit silently captures nothing). Each input carries a real
    ``name`` so the native POST maps the lead. The default
    ``name``/``email``/``phone``/``message`` field set matches the Site service's
    seeded ``event_mapping``, so a lead lands with no manual config.
    """
    contact = content.get("contact") or {}
    title = _s(content.get("form_title"), "Get in touch")
    return {
        "type": "card",
        "props": {"id": _ANCHOR_BOOK, "title": title},
        "children": [
            {
                "type": "input",
                "props": {
                    "name": "name",
                    "label": "Your name",
                    "placeholder": _s(contact.get("name_placeholder"), "Jane Doe"),
                    "required": True,
                },
            },
            {
                "type": "input",
                "props": {
                    "name": "email",
                    "label": "Email",
                    "type": "email",
                    "placeholder": _s(contact.get("email"), "you@email.com"),
                    "required": True,
                },
            },
            {
                "type": "input",
                "props": {
                    "name": "phone",
                    "label": "Phone",
                    "type": "tel",
                    "placeholder": _s(contact.get("phone"), "(555) 010-1234"),
                },
            },
            {
                "type": "textarea",
                "props": {
                    "name": "message",
                    "label": "How can we help?",
                    "placeholder": _s(
                        content.get("message_placeholder"), "Tell us what you need..."
                    ),
                },
            },
            {
                "type": "button",
                "props": {
                    "label": _s((content.get("cta_band") or {}).get("button_label"), "Send"),
                    # type=submit so the native outer-form POST fires.
                    "type": "submit",
                    "variant": "primary",
                },
            },
        ],
    }


def _footer(content: dict[str, Any]) -> dict[str, Any]:
    footer = content.get("footer") or {}
    contact = content.get("contact") or {}
    brand = _s(content.get("brand"), "Your Business")

    visit_links: list[dict[str, str]] = []
    address = _s(contact.get("address"))
    if address:
        visit_links.append({"label": address, "href": f"#{_ANCHOR_BOOK}"})
    phone = _s(contact.get("phone"))
    if phone:
        tel = "tel:" + "".join(ch for ch in phone if ch.isdigit())
        visit_links.append({"label": phone, "href": tel})
    if not visit_links:
        visit_links.append({"label": "Book", "href": f"#{_ANCHOR_BOOK}"})

    return {
        "type": "footer",
        # ``footer`` groups links into titled ``columns`` + a ``copyright`` line;
        # it has no ``brand``/flat ``links`` props (those silently drop).
        "props": {
            "columns": [
                {"title": "Visit", "links": visit_links},
                {
                    "title": "Explore",
                    "links": [
                        {"label": "Services", "href": f"#{_ANCHOR_SERVICES}"},
                        {"label": "Pricing", "href": f"#{_ANCHOR_PRICING}"},
                        {"label": "Book", "href": f"#{_ANCHOR_BOOK}"},
                    ],
                },
            ],
            "copyright": _s(footer.get("copyright"), f"© {brand}"),
        },
    }


def assemble_landing_spec(content: dict[str, Any]) -> dict[str, Any]:
    """Assemble a renderer-valid landing-page rippleSpec from an LLM copy object.

    ``content`` carries COPY ONLY — the assembler decides every widget kind and
    the whole node tree, so the structure is a pure function of the input and can
    never be downgraded. Expected (all optional, defaults fill gaps)::

        {
          "brand": str,
          "hero": {"eyebrow", "title", "subtitle", "cta_label"},
          "services": [{"title", "desc", "icon"}],          # variable length
          "testimonials": [{"quote", "author", "role"}],     # variable length
          "tiers": [{"name", "price", "period", "features": [str],
                     "popular": bool, "cta_label": str}],     # variable length
          "faqs": [{"question", "answer"}],                  # optional; section
                                                             # omitted when empty
          "faq_title": str,                                  # optional heading
          "cta_band": {"headline", "subtext", "button_label"},
          "contact": {"address", "phone", "email"},
          "footer": {"copyright"},
        }

    Returns a ``{version, state, ui}`` rippleSpec whose ``ui`` is the fixed
    conversion-ordered marketing tree. The caller persists it straight (no
    validate/redraft loop, no subagent) via the pockets service.
    """
    if not isinstance(content, dict):
        raise TypeError("assemble_landing_spec expects a content dict (copy only)")

    # The fixed conversion funnel. The FAQ section is the one OPTIONAL node — it
    # sits between pricing and the CTA band, and is dropped entirely when no
    # usable Q/A pairs were supplied (``_faq_section`` returns None).
    children: list[dict[str, Any]] = [
        _navbar(content),
        _hero(content),
        _services_section(content),
        _reviews_section(content),
        _pricing_section(content),
    ]
    faq = _faq_section(content)
    if faq is not None:
        children.append(faq)
    children.extend(
        [
            _cta_band(content),
            _lead_form_card(content),
            _footer(content),
        ]
    )

    return {
        "version": "1.0",
        "state": {},
        "ui": {
            "type": "flex",
            "props": {"direction": "column", "gap": "0"},
            "children": children,
        },
    }


__all__ = ["assemble_landing_spec"]
