---
name: pocketpaw-create-paw-site
description: |
  Build a marketing landing page as a Paw Site — a real, standalone
  website composed by conversion role (navbar, hero, services, social
  proof, pricing, CTA, lead form, footer) and rendered statically to the
  edge. Invoke when the user describes a BRAND-NEW marketing / landing
  site: "build a dentist landing site", "make a marketing page for my
  bakery", "a landing page for my SaaS", or when the create-site flow
  routes a new-site request here. This is the marketing-first authoring
  brain — it composes a sales page, NOT a dashboard. It stamps the pocket
  with type="site" + pattern="landing" so the published page renders as a
  landing page. For publishing an EXISTING pocket as a site, use
  pocketpaw-create-site (Path A). Loading this skill keeps the chat
  agent's always-on system prompt small while still delivering the full
  marketing brain when a landing site is actually requested.
---

# Build a Paw Site — the marketing landing brain

You're building a **Paw Site**: a real, standalone marketing website that
gets rendered **statically** (server-side, `csr=false`) and deployed to
the edge from a PocketPaw pocket's ``rippleSpec``. The pocket is the
source of truth; the generator turns its spec into a SvelteKit site.

This is **not** a dashboard. A landing page sells. It reads top to bottom
as a conversion funnel: grab attention, explain the offer, prove it,
price it, and capture the lead. Your job is to compose that page **by
conversion role** out of Ripple's marketing widgets, persist it as a
pocket stamped ``type="site"`` + ``pattern="landing"``, and hand the
pocket id to the publish step.

## STEP 0 — This page renders with NO JavaScript

The single most important fact: **the site is prerendered static HTML.**
Client-side JS does not run for the visitor on first paint (and for many
visitors, never). Everything that must work — the lead form submit, every
call-to-action link, the pricing cards, the FAQ answers — must work as
plain HTML. This drives every hard rule below. A widget that needs client
JS to function is **dead on a Paw Site.**

The site template wraps the whole page in one outer
``<form method="POST">`` so a native form submit posts the lead back to
the platform. That single fact creates the nested-form trap in rule 1.

## STEP 1 — Confirm the widget props before you draft

Before writing the spec, call ``mcp__pocketpaw_pocket__get_widget_spec``
for each marketing widget you'll use — at minimum ``hero``,
``feature-grid``, ``testimonial``, ``pricing-table``, ``cta``,
``navbar``, ``footer``. Copy prop names verbatim from the spec. The
renderer has a **closed registry**: an invented widget ``type`` or an
invented prop name renders as a red "Unknown widget type" box or silently
drops. Never guess ``pricing-table``'s shape — confirm it.

## STEP 2 — Compose by conversion role (the section grammar)

Lay the page out in this order. Each row is a conversion job, then the
widget that does it. Top to bottom, this is the funnel.

| # | Conversion job | Widget(s) |
|---|---|---|
| 1 | **Navigation** — brand + anchor links to the sections below | ``navbar`` (links are anchor ``href`` to ``#services`` / ``#pricing`` / ``#book``) |
| 2 | **Hook** — above-the-fold promise + primary CTA | ``hero`` (eyebrow + title + subtitle; the CTA child is an anchor ``href="#book"``) |
| 3 | **Offer** — what you do, as scannable benefits | ``feature-grid`` (the services / value props) |
| 4 | **Proof** — social proof, trust | ``testimonial`` (one or more) and/or ``logo-cloud`` |
| 5 | **Price** — plans the visitor can choose | ``pricing-table`` with ``tiers`` |
| 6 | **Mid-page nudge** — a second conversion band | ``cta`` (its button is an anchor ``href="#book"``) |
| 7 | **Capture** — the lead form | a ``section``/``card`` of **flat** ``input`` / ``textarea`` / ``button{type:"submit"}`` (see rule 1) |
| 8 | **Close** — footer with contact + links | ``footer`` |

Wrap the whole thing in a ``flex`` (``direction: column``) root, or use
the ``section``/``card`` containers between roles for rhythm. Give each
section an ``id`` (``services``, ``pricing``, ``book``) so the navbar and
CTA anchors land.

## STEP 3 — The HARD SSR rules (non-negotiable; the page breaks without them)

These five rules are the difference between a shippable landing page and
a broken one. Each was a real failure on a real render. Do not soften
them.

### Rule 1 — Lead form = FLAT native inputs. NEVER a `form` or `newsletter` widget.

The lead-capture section is built from **flat** primitives placed
directly in a ``card`` or ``section``:

- ``input`` widgets, each with a real ``name`` (``name="name"``,
  ``name="email"``, ``name="phone"``) and a ``label`` / ``placeholder``.
- an optional ``textarea`` with ``name="message"``.
- a ``button`` with ``type: "submit"`` to post the form.

**NEVER use the ``form`` widget or the ``newsletter`` widget here.** They
emit their **own** nested ``<form>`` element. The site template already
wraps the page in an outer ``<form method="POST">`` — a nested ``<form>``
is invalid HTML, the browser drops it, and the visitor's submit silently
does nothing. Flat ``input``s with real ``name``s ride the template's
outer form and POST natively. This is the trap that broke the first
render: the ``form`` widget produced ``<form novalidate>`` inside the
outer form and the page captured zero leads.

```json
{
  "type": "card",
  "props": {"id": "book", "title": "Book your visit"},
  "children": [
    {"type": "input", "props": {"name": "name", "label": "Your name", "placeholder": "Jane Doe", "required": true}},
    {"type": "input", "props": {"name": "email", "label": "Email", "type": "email", "placeholder": "jane@email.com", "required": true}},
    {"type": "input", "props": {"name": "phone", "label": "Phone", "type": "tel", "placeholder": "(555) 010-1234"}},
    {"type": "textarea", "props": {"name": "message", "label": "What do you need?", "placeholder": "I'd like a checkup..."}},
    {"type": "button", "props": {"label": "Request appointment", "type": "submit", "variant": "primary"}}
  ]
}
```

### Rule 2 — `pricing-table` uses `tiers`, never `plans`/`columns`.

``pricing-table``'s required array prop is **``tiers``** (confirmed in the
catalog). ``plans`` and ``columns`` are **wrong** — they render an empty
table. Each tier:

```json
{"id": "checkup", "name": "New Patient Exam", "price": "$89", "period": "one-time",
 "features": ["Full exam", "X-rays", "Cleaning"], "popular": true,
 "cta": {"label": "Book", "href": "#book"}}
```

Set ``currency`` on the widget if the spec supports it. Mark one tier
``popular: true``. Each tier's ``cta`` is an **anchor ``href``** (rule 4).
**Call ``get_widget_spec`` for ``pricing-table`` before drafting** to
confirm the exact tier-object keys.

### Rule 3 — FAQ = flat `heading` + `text` pairs. NEVER `accordion`.

If you add an FAQ, build it as a stack of ``heading`` (the question) +
``text`` (the answer) pairs. **Never use the ``accordion`` widget** — it
is a bits-ui client primitive whose panels only open with JavaScript. On
a static site the answers never expand and the FAQ is unreadable.

### Rule 4 — Every CTA is an anchor `href`. NEVER `on_click`.

Buttons and CTAs link via ``href`` (e.g. ``href="#book"``, or a
``tel:`` / ``mailto:`` for "call us"). An ``on_click`` handler needs
client JS, which doesn't run — an ``on_click`` CTA is a **dead button**
on a static site. The only native action is the lead form's submit
(rule 1). Everything else navigates by anchor.

### Rule 5 — `hero` is the marketing Hero widget. NEVER the dashboard `hero+grid`.

Use the ``hero`` widget — eyebrow, headline, subhead, and a CTA child.
Do **not** build the dashboard "``hero+grid``" layout (a ``page-header``
followed by a grid of KPI ``stat`` tiles). That is a dashboard pattern; a
KPI grid at the top of a sales page screams "internal tool", not
"landing page". No ``stat`` tiles, no metric grid, no charts. This is
marketing, not analytics.

## Animated polish — Tier-0 widgets ONLY

You may add tasteful motion, but **only Tier-0 (CSS-only, static-safe)
animation widgets**, because they animate with pure CSS and need no
client JS:

> ``aurora``, ``marquee``, ``border-beam``, ``shimmer``,
> ``animated-beam``, ``text-effect``, ``bento-grid``

An ``aurora`` backdrop behind the hero, a ``marquee`` of client logos, or
a ``border-beam`` on the popular pricing tier all work statically.

**NEVER** use ``reveal``, ``parallax``, or ``spotlight`` — they are
scroll/pointer-driven and need client JS, so on a static site they either
hide content (reveal-on-scroll never fires → the section stays invisible)
or simply don't move. When in doubt, leave it static; a clean static page
beats a broken animated one.

## STEP 4 — Persist the pocket (stamp type=site + pattern=landing)

Draft the full rippleSpec, then create the pocket via the specialist
create path, stamping the site identity:

- ``type="site"`` — this pocket IS a site, not a dashboard pocket.
- ``pattern="landing"`` — records the landing/conversion intent as
  first-class metadata, so the generator and any later edit flow treat it
  as a marketing page.

Call ``mcp__pocketpaw_pocket_specialist__create`` with the brief, the
hints (set ``type: "site"`` and ``pattern: "landing"`` in the hints), and
the drafted ``spec``. If your deployment routes custom multi-section
builds through the merge endpoint, use
``POST /api/v1/pockets/<id>/spec/merge`` to assemble the sections — but
the create call must carry ``type="site"`` + ``pattern="landing"`` so the
identity lands on the pocket.

```json
{
  "brief": "<the user's original brief>",
  "hints": {
    "name": "Bright Smile Dental",
    "description": "Family dentist landing page",
    "type": "site",
    "pattern": "landing",
    "color": "#0ea5e9",
    "icon": "tooth",
    "purpose": "Capture new-patient appointment requests"
  },
  "spec": { ... your conversion-ordered rippleSpec ... }
}
```

## STEP 5 — Publish

Once the pocket exists, publish it as a site. Hand the pocket id to
``mcp__pocketpaw_sites_manager__publish`` (the same publish hop the
``pocketpaw-create-site`` skill uses) and show the user the returned
``url`` plus a pointer to **/sites**. Relay any ``ok: false`` error —
never claim a phantom publish.

If you arrived here from the ``pocketpaw-create-site`` skill's Path B,
that skill owns the publish call — return the created pocket id to it.

## Mock content is required

Vague briefs get **plausible, concrete copy** — never "TBD" or "Lorem
ipsum". A dentist gets real service names (New Patient Exam, Whitening,
Invisalign), real testimonial quotes with names (Maria G., James T.),
real tier prices. The page must read like a finished business site, not a
wireframe.

## Quality bar

The site is built right when:

1. **It reads as a funnel.** Top to bottom: nav → hero → services →
   proof → pricing → CTA → lead form → footer. A visitor can scan it and
   know what's sold and how to buy.
2. **The lead form is flat + named.** Real ``<input name>``s and a submit
   button — no ``form`` / ``newsletter`` widget — so the published page
   POSTs natively and captures leads on the first paint with zero JS.
3. **Pricing is populated.** ``pricing-table`` with real ``tiers`` — not
   ``plans``/``columns``, not an empty table.
4. **Every CTA navigates.** Anchor ``href``s (or ``tel:`` / ``mailto:``)
   — no dead ``on_click`` buttons, no ``accordion`` FAQ.
5. **No dashboard widgets.** No KPI ``stat`` grid, no ``hero+grid``, no
   charts. It's a marketing page, stamped ``pattern="landing"``.

## Related tools (via MCP)

- ``mcp__pocketpaw_pocket__get_widget_spec`` — **call this first** for
  every marketing widget; confirm ``pricing-table``'s ``tiers`` shape and
  the ``hero`` / ``feature-grid`` / ``testimonial`` props.
- ``mcp__pocketpaw_pocket_specialist__create`` — persist the pocket
  (stamp ``type="site"`` + ``pattern="landing"``).
- ``mcp__pocketpaw_sites_manager__publish`` — publish the pocket as a
  live site; show the user the ``url``.
- ``mcp__pocketpaw_pocket__list_pockets`` — find an existing pocket if the
  user named one rather than describing a new site.
