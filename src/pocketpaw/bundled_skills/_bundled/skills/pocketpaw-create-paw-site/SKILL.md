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

## STEP 0 — Match the brief to the landing-page skeleton (the fast-path)

**Do this FIRST, before drafting anything.** PocketPaw ships a pre-baked
``landing-page`` template — a complete, conversion-ordered rippleSpec
skeleton built from the real marketing widgets, with ``[bracketed]``
placeholder copy. When the brief is a landing/marketing/sales site, you
**instantiate that skeleton** instead of cold-drafting the whole tree.
This is the difference between a ~4-minute cold draft and a near-instant
fill.

The match is a simple, case-insensitive substring check — the same one
the bundled template registry uses:

1. Lower-case the user's brief.
2. Read the keyword rows in
   ``src/pocketpaw/bundled_templates/_bundled/index.json``. The
   ``landing-page`` row registers:
   ``landing page``, ``landing site``, ``marketing page``,
   ``marketing site``, ``sales page``, ``website for``, ``site for``.
3. If **any** of those keywords is a substring of the lowered brief, it's
   a landing match.

On a landing match: **set ``hints.template_id = "landing-page"`` on the
create call (STEP 4) and STOP cold-drafting.** The specialist's shared
template splice then loads the skeleton and injects it into the build
prompt under an "INSTANTIATE AND CUSTOMIZE, DO NOT REDESIGN" banner — the
skeleton already encodes the conversion order, every marketing widget, and
the five SSR rules below **by construction.** Your job collapses to STEP 1:
replace the ``[bracketed]`` copy. You do **not** re-pick widgets, re-derive
the section order, or hand-compose the tree.

(If the brief is genuinely not a landing site — no keyword matches — fall
back to the full compose path: confirm widget props with
``get_widget_spec`` and build by conversion role using the section grammar
and the worked example further down as your reference.)

## The static-render fact (drives every SSR rule below)

The single most important fact: **the site is prerendered static HTML.**
Client-side JS does not run for the visitor on first paint (and for many
visitors, never). Everything that must work — the lead form submit, every
call-to-action link, the pricing cards, the FAQ answers — must work as
plain HTML. This drives every hard rule below. A widget that needs client
JS to function is **dead on a Paw Site.**

The site template wraps the whole page in one outer
``<form method="POST">`` so a native form submit posts the lead back to
the platform. That single fact creates the nested-form trap in rule 1.

## STEP 1 — Fill the spliced skeleton (replace the [bracketed] copy)

On a landing match (STEP 0) you are handed the ``landing-page`` skeleton
already spliced into your build prompt. **Do NOT call ``get_widget_spec``
per widget and do NOT hand-compose the tree — the skeleton already did
both correctly.** Instantiate it:

- Replace every ``[bracketed]`` placeholder with concrete, on-domain copy:
  the business name, the real services, real testimonial quotes with
  names, real tier names + prices, the address/phone in the footer. A
  dentist brief gets New Patient Exam / Whitening / Invisalign — never
  "TBD" or "Service one".
- **Preserve the structure.** The node tree, the conversion order, the
  marketing widget at each section, the anchor ``id``s on the wrapping
  ``section`` / ``card``, the flat lead form, the ``tiers`` shape — all
  correct already. Do not swap a marketing widget for ``grid`` + ``card``,
  do not reorder the funnel, do not add a ``form`` / ``accordion`` widget.
- Keep the lead form flat and named (rule 1), keep ``pricing-table`` on
  ``tiers`` (rule 2), keep every CTA an anchor ``href`` (rule 4).
- Drop the ``_placeholder_note`` and any ``_``-prefixed key before persist.

Then go straight to STEP 4 (persist) and STEP 5 (publish). The SSR rules
in STEP 3 are your **review checklist** over the filled skeleton — verify
them, don't re-derive the page from them.

<details>
<summary><b>Fallback only — the full compose path (no landing keyword matched)</b></summary>

When STEP 0 found no landing match you build the page by hand. First
confirm the widget props, then compose by conversion role.

**Confirm the widget props.** Call ``mcp__pocketpaw_pocket__get_widget_spec``
for **every** marketing widget you'll use:

> ``navbar``, ``hero``, ``feature-grid``, ``testimonial``,
> ``logo-cloud``, ``pricing-table``, ``cta``, ``newsletter``, ``footer``

Copy prop names **verbatim** from the spec. The renderer has a **closed
registry**: an invented widget ``type`` renders as a red "Unknown widget
type" box, and an invented or mis-shaped prop **silently drops** — the
widget renders empty. That empty-render is the trap that makes a brain
abandon the widget and hand-roll the section from ``grid`` + ``card``
instead. Don't. Confirm the prop shape, fill it correctly, and the widget
renders polished.

**Compose by conversion role.** Build every section with its purpose-built
marketing widget. Do NOT hand-roll services / proof / nav / footer / CTA /
logos / email as generic ``grid`` + ``card`` + ``text`` + ``flex``. Each
conversion job below has ONE mandated widget. Use it. The only section
built from flat primitives is the lead form (rule 1). Lay the page out in
this order. Top to bottom, this is the funnel.

| # | Conversion job | MANDATED widget | NEVER hand-roll as |
|---|---|---|---|
| 1 | **Navigation** — brand + anchor links | ``navbar`` | ~~``flex`` + ``text`` + ``button``~~ |
| 2 | **Hook** — above-the-fold promise | ``hero`` | ~~``page-header`` + ``stat`` grid~~ |
| 3 | **Offer** — services / value props | ``feature-grid`` | ~~``grid`` + ``card``~~ |
| 4 | **Proof** — testimonials | ``testimonial`` (one per quote) | ~~``grid`` + ``card``~~ |
| 5 | **Trust** — client / partner logos | ``logo-cloud`` | ~~``flex`` + broken ``img``~~ |
| 6 | **Price** — plans | ``pricing-table`` (``tiers``) | ~~``grid`` + ``card``~~ |
| 7 | **Mid-page nudge** — 2nd conversion band | ``cta`` | ~~``flex`` + ``text`` + ``button``~~ |
| 8 | **Email** (optional) — list signup | ``newsletter`` | ~~``flex`` + ``input`` + ``button``~~ |
| 9 | **Capture** — the lead form | **flat** ``input`` / ``textarea`` / ``button{type:"submit"}`` in a ``card`` (rule 1 — UNCHANGED) | — |
| 10 | **Close** — footer | ``footer`` | ~~``flex`` + ``text``~~ |

Wrap the whole thing in a ``flex`` (``direction: column``) root. The
marketing widgets carry **no ``id`` prop of their own**, so where the
navbar / CTA anchors need a landing target (``#services``, ``#pricing``,
``#reviews``, ``#book``), wrap that widget — or place the lead form — in a
``section`` / ``card`` that carries the ``id``. Anchors point at the
wrapper's ``id``; the widget renders inside it.

### The mandated widgets — real prop shapes (paste-ready)

Confirm each with ``get_widget_spec`` (live source of truth), but these
are the shapes you'll fill. **Watch the shapes marked ⚠ — they are the
ones a brain most often gets wrong, which is why the widget renders empty
and tempts a fall back to ``grid`` + ``card``.**

- **``navbar``** — ``brand`` (string), ``links`` (``Array<{label, href}>``),
  ``cta`` (string label ⚠ — a **string**, not an object),
  ``ctaHref`` (string ⚠ — the CTA destination is a **separate** prop, not
  nested in ``cta``), ``sticky`` (boolean).
- **``hero``** — ``title`` *(required)*, ``subtitle``, ``eyebrow``,
  ``align`` (``"left"|"center"``). ⚠ **``hero`` has no CTA prop.** Put the
  primary call-to-action in the ``navbar``'s ``cta`` or in a following
  ``cta`` band — do not invent a ``hero`` button.
- **``feature-grid``** — ``features`` *(required)*,
  ``Array<{title, description?, icon?}>`` — each item's optional ``icon``
  is a **lucide icon name** (e.g. ``"tooth"``, ``"sparkles"``,
  ``"shield"``); ``columns`` (``2|3|4``, default 3).
- **``testimonial``** — ``quote`` *(required)*, ``author``, ``role``,
  ``avatar`` (image URL). ⚠ **One testimonial per widget** — no ``items``
  array; for three quotes, emit three ``testimonial`` nodes. There is **no
  ``rating`` prop and no ``id`` prop** — don't add them (they silently
  drop).
- **``logo-cloud``** — ``heading`` (e.g. "Trusted by"),
  ``logos`` (``Array<{src, alt, href?}>``).
- **``pricing-table``** — ``tiers`` *(required)*,
  ``Array<{id, name, price, period?, description?, features?, cta?, popular?}>``;
  ``currency`` (a **symbol** like ``"$"`` ⚠ — not ``"USD"``). ⚠ Inside a
  tier, ``cta`` is a **string** button label (not an object), ``price`` is
  a string **or** number, and ``features`` is ``Array<string>`` **or**
  ``Array<{label, included?}>`` (use the object form to show
  ✓/✗ rows). Mark one tier ``popular: true``.
- **``cta``** — ``headline`` *(required)* ⚠ (**not** ``title``),
  ``subtext`` ⚠ (**not** ``subtitle``), ``button`` (string label ⚠ — not
  an object), ``href`` (the destination ⚠ — a sibling prop, not nested in
  ``button``), ``align``.
- **``newsletter``** — ``heading``, ``subtext``, ``placeholder``
  (default ``"you@example.com"``), ``button`` (default ``"Subscribe"``).
  Emits the email via ``on_submit``. (For the **lead-capture** form, use
  flat inputs per rule 1 — ``newsletter`` is only for an optional
  list-signup band, and even then it emits its own ``<form>``; if SSR
  capture matters, prefer flat inputs.)
- **``footer``** — ``columns``
  (``Array<{title, links: Array<{label, href}>}>``), ``copyright`` (the
  legal line). ⚠ **No ``brand`` / ``tagline`` / flat ``links`` props** —
  group links into titled ``columns`` and put the business name in
  ``copyright``.

### WORKED EXAMPLE — a full landing spec with the real widgets

A dentist landing page. Every section is its purpose-built widget; only
the lead form is flat (rule 1). Copy this shape and adapt the copy.

```json
{
  "version": "1.0",
  "ui": {
    "type": "flex",
    "props": { "direction": "column", "gap": "0" },
    "children": [
      { "type": "navbar", "props": {
          "brand": "Bright Smile Dental",
          "links": [
            { "label": "Services", "href": "#services" },
            { "label": "Reviews",  "href": "#reviews" },
            { "label": "Pricing",  "href": "#pricing" },
            { "label": "Book",     "href": "#book" }
          ],
          "cta": "Book a visit",
          "ctaHref": "#book",
          "sticky": true
      }},

      { "type": "hero", "props": {
          "eyebrow": "Family & cosmetic dentistry",
          "title": "Care that fits your whole family",
          "subtitle": "Gentle, modern dentistry in downtown Austin. Same-week appointments, transparent pricing, no surprises.",
          "align": "center"
      }},

      { "type": "section", "props": { "id": "services" }, "children": [
        { "type": "feature-grid", "props": {
            "columns": 4,
            "features": [
              { "icon": "tooth",    "title": "New Patient Exams", "description": "Full exam, digital X-rays, and a cleaning in one visit." },
              { "icon": "sparkles", "title": "Teeth Whitening",   "description": "In-office whitening up to 8 shades brighter in an hour." },
              { "icon": "smile",    "title": "Invisalign",        "description": "Clear aligners with a custom plan and a free consult." },
              { "icon": "shield",   "title": "Emergency Care",    "description": "Same-day relief for pain, chips, and lost fillings." }
            ]
        }}
      ]},

      { "type": "section", "props": { "id": "reviews" }, "children": [
        { "type": "testimonial", "props": {
            "quote": "Best dental experience I've had. They explained every option and the cleaning was painless.",
            "author": "Maria G.", "role": "Patient since 2023"
        }},
        { "type": "testimonial", "props": {
            "quote": "Booking went from a phone-tag headache to one tap. The team is wonderful.",
            "author": "James T.", "role": "Patient since 2021"
        }},
        { "type": "logo-cloud", "props": {
            "heading": "Trusted by families across Austin",
            "logos": [
              { "src": "/logos/delta-dental.svg", "alt": "Delta Dental" },
              { "src": "/logos/cigna.svg",        "alt": "Cigna" },
              { "src": "/logos/metlife.svg",      "alt": "MetLife" }
            ]
        }}
      ]},

      { "type": "section", "props": { "id": "pricing" }, "children": [
        { "type": "pricing-table", "props": {
            "currency": "$",
            "tiers": [
              { "id": "exam",  "name": "New Patient Exam", "price": "89",  "period": "one-time",
                "features": ["Full exam", "Digital X-rays", "Cleaning"], "cta": "Book" },
              { "id": "white", "name": "Whitening",        "price": "299", "period": "one-time", "popular": true,
                "features": ["In-office session", "Up to 8 shades", "Take-home trays"], "cta": "Book" },
              { "id": "invis", "name": "Invisalign",       "price": "3,900", "period": "full plan",
                "features": ["Custom aligners", "All visits", "Retainers included"], "cta": "Free consult" }
            ]
        }}
      ]},

      { "type": "cta", "props": {
          "headline": "Ready for a healthier smile?",
          "subtext": "Same-week appointments are filling up.",
          "button": "Request an appointment",
          "href": "#book",
          "align": "center"
      }},

      { "type": "card", "props": { "id": "book", "title": "Book your visit" },
        "children": [
          { "type": "input",    "props": { "name": "name",  "label": "Your name", "placeholder": "Jane Doe", "required": true } },
          { "type": "input",    "props": { "name": "email", "label": "Email", "type": "email", "placeholder": "jane@email.com", "required": true } },
          { "type": "input",    "props": { "name": "phone", "label": "Phone", "type": "tel", "placeholder": "(555) 010-1234" } },
          { "type": "textarea", "props": { "name": "message", "label": "What do you need?", "placeholder": "I'd like a checkup and cleaning..." } },
          { "type": "button",   "props": { "label": "Request appointment", "type": "submit", "variant": "primary" } }
        ]
      },

      { "type": "footer", "props": {
          "columns": [
            { "title": "Visit",   "links": [ { "label": "421 Congress Ave, Austin TX", "href": "#book" }, { "label": "(555) 010-1234", "href": "tel:5550101234" } ] },
            { "title": "Explore", "links": [ { "label": "Services", "href": "#services" }, { "label": "Pricing", "href": "#pricing" }, { "label": "Book", "href": "#book" } ] }
          ],
          "copyright": "© 2026 Bright Smile Dental"
      }}
    ]
  }
}
```

Note in the example: ``navbar`` carries the only ``cta`` (the ``hero`` has
none); ``cta`` uses ``headline`` / ``subtext`` / ``button`` / ``href``;
each tier ``cta`` is a **string**; ``currency`` is ``"$"``; the footer is
titled ``columns`` + ``copyright`` (no ``brand`` / flat ``links``); two
quotes = two ``testimonial`` nodes; anchor ids live on wrapping
``section`` / ``card`` because the marketing widgets carry none. This is
the polished, Ripple-native page — not a ``grid`` + ``card`` wireframe.
(The ``landing-page`` skeleton you splice in STEP 0 is this exact shape
with ``[bracketed]`` copy — the fast-path is just "fill this in".)

</details>

## STEP 3 — The HARD SSR rules (your checklist over the filled skeleton)

Rules 1–5 are the **SSR contract** — the difference between a shippable
landing page and a broken one; each was a real failure on a real render.
Rule 6 is the matching **widget-integrity** rule for ``logo-cloud``. The
``landing-page`` skeleton already satisfies all six **by construction**, so
on the fast-path these are your **review checklist** — verify the filled
spec still honors them (and that your copy edits didn't reintroduce a
``form`` widget, an ``accordion``, a ``plans`` key, or an ``on_click``).
On the fallback compose path they are the rules you build to. Do not
soften any of them.

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
{"id": "checkup", "name": "New Patient Exam", "price": "89", "period": "one-time",
 "features": ["Full exam", "X-rays", "Cleaning"], "popular": true,
 "cta": "Book"}
```

The tier ``cta`` is a **string** button label, not an object — the
pricing-table renders the tier card and routes the click itself. Set
``currency`` to a **symbol** (``"$"``, ``"€"``), not a code like
``"USD"``. ``price`` is a string or number; ``features`` is
``Array<string>`` or ``Array<{label, included?}>`` (object form shows
✓/✗ rows). Mark one tier ``popular: true``. **Call
``get_widget_spec`` for ``pricing-table`` before drafting** to confirm the
exact tier-object keys.

### Rule 3 — FAQ = flat `heading` + `text` pairs. NEVER `accordion`.

If you add an FAQ, build it as a stack of ``heading`` (the question) +
``text`` (the answer) pairs. **Never use the ``accordion`` widget** — it
is a bits-ui client primitive whose panels only open with JavaScript. On
a static site the answers never expand and the FAQ is unreadable.

### Rule 4 — Every CTA is an anchor `href`. NEVER `on_click`.

CTAs link by **anchor destination**, never a click handler. An
``on_click`` handler needs client JS, which doesn't run — an ``on_click``
CTA is a **dead button** on a static site. The native action is the lead
form's submit (rule 1); everything else navigates by anchor:

- ``navbar`` CTA → set ``ctaHref`` (e.g. ``"#book"``).
- standalone ``cta`` band → set ``href`` (e.g. ``"#book"``, or
  ``tel:`` / ``mailto:`` for "call us").
- ``navbar`` / ``footer`` link items → each carries its own ``href``.
- ``pricing-table`` tiers → the tier ``cta`` is a string **label**; the
  pricing-table wires the click to the page's lead anchor itself. Don't
  bolt an ``on_click`` onto a tier.

### Rule 5 — `hero` is the marketing Hero widget. NEVER the dashboard `hero+grid`.

Use the ``hero`` widget — ``eyebrow`` + ``title`` + ``subtitle`` +
``align``. It carries **no CTA prop** (don't invent one); the primary
call-to-action lives in the ``navbar`` (``cta`` + ``ctaHref``) and in the
mid-page ``cta`` band. Do **not** build the dashboard "``hero+grid``"
layout (a ``page-header`` followed by a grid of KPI ``stat`` tiles). That
is a dashboard pattern; a KPI grid at the top of a sales page screams
"internal tool", not "landing page". No ``stat`` tiles, no metric grid, no
charts. This is marketing, not analytics.

### Rule 6 — `logo-cloud` with no real logos: text-mode or omit. NEVER broken `<img>`s.

``logo-cloud`` renders ``logos: Array<{src, alt, href?}>`` as ``<img>``
tags. If you don't have **real, resolvable** logo URLs, a made-up ``src``
(``/logos/acme.svg``) renders as a **broken-image icon** on the live site
— worse than no logo wall at all. So when the brief gives you no real
logos:

- **Prefer omit** — drop the ``logo-cloud`` entirely and lean on
  ``testimonial`` for social proof.
- **Or go text-mode** — keep the ``heading`` (e.g. "Trusted by 400+
  Austin families") and skip ``logos`` (empty array), so the trust line
  renders without any broken images.

Never ship invented ``src`` paths. A broken-image row reads as
"unfinished site".

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

Create the pocket via the specialist create path, stamping the site
identity:

- ``type="site"`` — this pocket IS a site, not a dashboard pocket.
- ``pattern="landing"`` — records the landing/conversion intent as
  first-class metadata, so the generator and any later edit flow treat it
  as a marketing page.
- ``template_id="landing-page"`` — **set this on the fast-path** (STEP 0
  matched a landing keyword). It tells the specialist to splice the
  pre-baked landing skeleton into the build prompt, so the model
  instantiates rather than cold-drafts. Omit it only on the fallback
  compose path (no keyword matched), where you supply a fully hand-built
  ``spec`` instead.

Call ``mcp__pocketpaw_pocket_specialist__create`` with the brief, the
hints (set ``type: "site"``, ``pattern: "landing"``, and on the fast-path
``template_id: "landing-page"``), and — on the fallback path — the drafted
``spec``. If your deployment routes custom multi-section builds through the
merge endpoint, use ``POST /api/v1/pockets/<id>/spec/merge`` to assemble
the sections — but the create call must carry ``type="site"`` +
``pattern="landing"`` so the identity lands on the pocket.

```json
{
  "brief": "<the user's original brief>",
  "hints": {
    "name": "Bright Smile Dental",
    "description": "Family dentist landing page",
    "type": "site",
    "pattern": "landing",
    "template_id": "landing-page",
    "color": "#0ea5e9",
    "icon": "tooth",
    "purpose": "Capture new-patient appointment requests"
  }
}
```

On the fast-path the spliced skeleton IS the starting spec; you fill its
``[bracketed]`` copy in the build prompt, so you don't pass a hand-drafted
``spec`` here. On the fallback path, add your conversion-ordered
``"spec": { ... }`` to the call.

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

1. **Every section is its purpose-built marketing widget.** ``navbar`` /
   ``hero`` / ``feature-grid`` / ``testimonial`` / ``logo-cloud`` /
   ``pricing-table`` / ``cta`` / ``footer`` — **not** hand-rolled from
   ``grid`` + ``card`` + ``text`` + ``flex``. (The lead form is the lone
   flat exception, per rule 1.) If services or proof or the nav/footer are
   built from generic primitives, the page reads as a wireframe and the
   build is wrong.
2. **It reads as a funnel.** Top to bottom: nav → hero → services →
   proof → pricing → CTA → lead form → footer. A visitor can scan it and
   know what's sold and how to buy.
3. **The lead form is flat + named.** Real ``<input name>``s and a submit
   button — no ``form`` / ``newsletter`` widget — so the published page
   POSTs natively and captures leads on the first paint with zero JS.
4. **Pricing is populated.** ``pricing-table`` with real ``tiers`` — not
   ``plans``/``columns``, not an empty table; tier ``cta`` a string,
   ``currency`` a symbol.
5. **Every CTA navigates.** Anchor destinations (``navbar.ctaHref`` /
   ``cta.href`` / link ``href``s, or ``tel:`` / ``mailto:``) — no dead
   ``on_click`` buttons, no ``accordion`` FAQ.
6. **No dashboard widgets.** No KPI ``stat`` grid, no ``hero+grid``, no
   charts. It's a marketing page, stamped ``pattern="landing"``.

## Related tools (via MCP)

- ``mcp__pocketpaw_pocket__get_widget_spec`` — **call this first** for
  every marketing widget by name: ``navbar`` / ``hero`` / ``feature-grid``
  / ``testimonial`` / ``logo-cloud`` / ``pricing-table`` / ``cta`` /
  ``newsletter`` / ``footer``. Confirm ``pricing-table``'s string-``cta``
  tier shape, ``cta``'s ``headline`` / ``button`` / ``href``, and
  ``footer``'s ``columns`` before drafting.
- ``mcp__pocketpaw_pocket_specialist__create`` — persist the pocket
  (stamp ``type="site"`` + ``pattern="landing"``).
- ``mcp__pocketpaw_sites_manager__publish`` — publish the pocket as a
  live site; show the user the ``url``.
- ``mcp__pocketpaw_pocket__list_pockets`` — find an existing pocket if the
  user named one rather than describing a new site.
