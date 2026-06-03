---
{
  "title": "Marketing Landing Page — conversion-ordered Paw Site recipe",
  "summary": "Canonical recipe for a brand-new marketing/landing site published as a static Paw Site. Composes by conversion role (navbar, hero, feature-grid, testimonial, pricing-table, cta, flat lead form, footer) and stamps the pocket type=site + pattern=landing. Use whenever the user describes a landing page or marketing site for a business (dentist, salon, bakery, agency, SaaS, gym). This is the marketing alternative to the dashboard pattern.",
  "concepts": [
    "landing",
    "marketing-site",
    "lead-capture",
    "hero",
    "feature-grid",
    "testimonial",
    "pricing-table",
    "paw-site",
    "conversion-funnel",
    "static-render",
    "tiers",
    "cta",
    "navbar",
    "footer"
  ],
  "categories": [
    "marketing",
    "landing-page",
    "recipe",
    "paw-sites",
    "lead-capture"
  ],
  "source_path": "landing-marketing.md",
  "source_docs": [
    "3773259965aa3619"
  ],
  "backlinks": null,
  "word_count": 813,
  "compiled_at": "2026-06-03T16:57:21Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

# When to use

Use this recipe for a brand-new **marketing / landing site** brief — "build a dentist landing page", "a marketing site for my bakery", "a landing page for my SaaS", "an agency site that captures leads". The output is published as a **Paw Site**: rendered statically (server-side, `csr=false`) and deployed to the edge. The pocket is stamped `type="site"` + `pattern="landing"`.

This is NOT a dashboard. A landing page is a conversion funnel read top to bottom: nav → hero → services → social proof → pricing → CTA → lead form → footer. Compose by **conversion role**, not by metric. Use it AS-IS for any single-business landing page and adapt the copy, service names, and tier prices for the domain.

# Why the rules are hard (the static-render contract)

The site is prerendered HTML and works with **no JavaScript**. The site template wraps the whole page in one outer `<form method="POST">` so a native submit posts the lead. Five rules follow from that, and each was a real failure on a real render (the A-vs-C bake-off):

1. **Lead form = flat native inputs.** Build it from flat `input` / `textarea` / `button{type:"submit"}` with real `name=`, placed directly in a `card` / `section`. NEVER the `form` or `newsletter` widget — they emit their own nested `<form>`, which is invalid inside the template's outer form, so the browser drops it and the page captures zero leads. This is the exact failure that broke Option A.
2. **`pricing-table` uses `tiers`** (not `plans`, not `columns`). The wrong key renders an empty table. Each tier is `{id, name, price, period, features[], popular?, cta}` and the `cta` is an anchor href.
3. **FAQ = flat `heading` + `text` pairs.** NEVER `accordion` — its panels only open with client JS, so on a static site the answers never expand.
4. **Every CTA is an anchor `href`** (`#book`, `tel:`, `mailto:`). An `on_click` CTA needs JS and is a dead button. The lead form's submit is the only native action.
5. **`hero` is the marketing Hero widget**, never the dashboard `hero+grid` (a `page-header` + KPI `stat` grid). No KPI tiles, no charts on a sales page.

Animated polish is allowed but **Tier-0 (CSS-only, static-safe) only**: `aurora`, `marquee`, `border-beam`, `shimmer`, `animated-beam`, `text-effect`, `bento-grid`. NEVER `reveal` / `parallax` / `spotlight` — they need client JS; reveal-on-scroll hides the section on a static render.

# Composition

Root is a `flex` column. Each conversion role is a section with an `id` the navbar and CTA anchors target (`#services`, `#pricing`, `#reviews`, `#book`):

- `navbar` — brand + anchor links + a `Book` CTA (href `#book`).
- `hero` — eyebrow, title, subtitle, and a CTA child whose href is `#book`.
- `feature-grid` (`id: services`) — the services / value props as scannable benefits.
- `testimonial` (`id: reviews`) — social proof; one or more, optionally a `logo-cloud`.
- `pricing-table` (`id: pricing`) — `tiers` with one marked `popular: true`; each tier cta is an anchor.
- `cta` — a mid-page conversion band whose button href is `#book`.
- `card` (`id: book`) — the lead form: flat `input name="name"`, `input name="email"`, `input name="phone"`, `textarea name="message"`, and a `button type="submit"`.
- `footer` — brand, contact line, anchor links.

For an animated hero, give it an `aurora` backdrop — pure CSS, survives a JS-off render. A `marquee` of client logos and a `border-beam` on the popular tier are also safe.

# Anti-patterns to avoid

- ❌ Lead form built with the `form` or `newsletter` widget → nested invalid `<form>`; submit silently no-ops. Use flat `input` + `button{type:"submit"}` with real `name=`.
- ❌ `pricing-table` with `plans` / `columns` → empty table. Use `tiers`.
- ❌ FAQ with `accordion` → answers never open with JS off. Use `heading` + `text` pairs.
- ❌ CTA with `on_click` → dead button. Use anchor `href`.
- ❌ A `page-header` + KPI `stat` grid at the top (the dashboard `hero+grid`) → reads as an internal tool. Use the `hero` widget.
- ❌ `reveal` / `parallax` / `spotlight` animation → needs client JS. Stay Tier-0.

# Variations

- **Salon / barber**: services become Cut / Color / Treatment; tiers are service prices.
- **SaaS**: `feature-grid` is product capabilities; tiers are Free / Pro / Team; add a `logo-cloud` under the hero.
- **Agency**: `feature-grid` is service lines; the lead form swaps `phone` for `company`; CTA → "Book a discovery call".
- **Restaurant / bakery**: `feature-grid` is menu highlights; footer carries hours + address; CTA is "Reserve a table".
- **Gym / studio**: tiers are membership plans (Drop-in / Monthly / Annual), mark Monthly `popular`.

# Known Gaps

The FAQ-as-accordion, `plans`-vs-`tiers`, and nested-form traps are enforced here by convention only. The Paw Sites build does not yet reject a spec that uses a non-static-safe animated widget at generation time (a tier check against the motion engine is a planned follow-up), so the brain must hold these rules itself.