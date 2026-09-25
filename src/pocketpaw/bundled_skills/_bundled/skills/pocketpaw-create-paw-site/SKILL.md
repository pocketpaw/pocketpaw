---
name: pocketpaw-create-paw-site
description: |
  Build a marketing landing page as a Paw Site — a real, standalone
  website composed by conversion role (navbar, hero, services, social
  proof, pricing, CTA, lead form, footer) and rendered statically to the
  edge. This is the DEFAULT landing-site brain and the right choice for the
  COMMON case — a static, content-first marketing / landing page: "build a
  dentist landing site", "make a marketing page for my bakery", "a landing
  page for my SaaS", or when the create-site flow routes a new-site request
  here. Reach for pocketpaw-create-svelte-site INSTEAD only when the site
  genuinely needs framework-level interactivity or the user explicitly asks
  for Svelte. This is the marketing-first authoring brain — it composes a sales
  page, NOT a dashboard. You provide the COPY only; a deterministic tool
  assembles the page structure and stamps the pocket with type="site" +
  pattern="landing" so the published page renders as a landing page. (Need a
  bespoke hand-written HTML/CSS page instead of the composed structure — e.g.
  the user asks for plain HTML or a highly custom one-off layout? This skill's
  raw-HTML track covers that; see the body.) For publishing an EXISTING pocket
  as a site, use pocketpaw-create-site (Path A). Loading this skill keeps the
  chat agent's always-on system prompt small while still delivering the full
  marketing brain when a landing site is actually requested.
---

# Build a Paw Site — the marketing landing brain

<!--
  Updated: 2026-09-24 (docs/sites-packages-and-verify-guidance): the raw-HTML
  track can declare npm packages (`dependencies` / set_site_dependencies),
  served through a generated importmap and imported by bare name in a module
  script, and gains a "Verify loop" (only `verification.status == passed`
  means ready; fix → verify_site, max 3 rounds; unverified said plainly). The
  html edit section points at the same loop.

  Updated: 2026-09-06 (feat/fx-skill-amendments): added the "Sections from
  paw-fx" loop to the raw-HTML track (search_effects / list_effect_categories →
  get_effect → write `files` verbatim into `source`, place `snippet`, follow
  `usage`), plus the two things an agent gets wrong: the snippet is a finished
  section that is tuned through `--fx-*` and `options` rather than re-styled,
  and its `/_fx/...` paths are root-absolute so they must be copied as given.
  The Related tools entry for the fx server already existed; this is the
  working loop behind it. Nothing on the copy-only path changed.

  Updated: 2026-09-17 (docs/sites-multipage-skills): the raw-HTML track is now
  also the answer for a site that needs more than one page — `create_landing_site`
  composes one anchored document and cannot express a second URL. Adds the
  directory-index page shape (`about/index.html` → `/about/`), the root-absolute
  link rule, a heuristic for when a brief actually warrants a second page, and
  the author-before-you-link rule: no layer checks that a nav href resolves, so a
  dangling one ships as a live 404 with every layer reporting success.
-->

You're building a **Paw Site**: a real, standalone marketing website that
gets rendered **statically** (server-side, `csr=false`) and deployed to
the edge.

**The one rule that changes everything: you do NOT compose a rippleSpec.**
You write the **copy** — the business name, the headline, the services,
the testimonials, the prices — and hand it to a deterministic tool
(`mcp__pocketpaw_sites_manager__create_landing_site`). **CODE** assembles
the page structure (every marketing widget, the section order, the SSR
rules) from your copy. The structure is fixed and cannot be downgraded —
the copy is your only input.

## Opt-in: the raw-HTML track

The copy-only path above is the **default** and what you should use for a
normal "build me a landing site" request. Two things send you here instead —
author the markup yourself and call **`create_html_site`**:

1. The user **explicitly** asks for a plain / raw / single-file **HTML** site
   — "just give me an `index.html`", "no framework", "hand-written HTML/CSS",
   "no Svelte".
2. **The site needs more than one page** (see below). `create_landing_site`
   composes ONE document with an anchor nav — `#services`, `#reviews`,
   `#pricing`, `#book` — and has no way to express a second URL. A brief that
   names real pages has to come here.

```
mcp__pocketpaw_sites_manager__create_html_site(
  source = { "index.html": "<!doctype html>…", "styles.css": "…" },
  name   = "…"                       // optional; defaults to "HTML site"
)
```

`source` is a `{ relative_path: file_contents }` map of raw HTML/CSS/JS.
It **must** include `index.html` (the edge serves it at the root); add
stylesheets, scripts, and assets as sibling entries — every value is a
content string. Publishing an html site skips the build step entirely, so
the page must be complete on its own (inline or linked CSS/JS, real copy —
never "TBD"/"Lorem ipsum"). It returns `{ ok, pocket_id, pocket,
verification }`; hand `pocket_id` to `publish` exactly like the copy path
(STEP 3). If `ok` is false, relay the error.

**npm packages on this track.** Pass `dependencies=[{"name": "gsap"}]` (or
call `set_site_dependencies(pocket_id, add=[...])` later). Each is
policy-checked, pinned, and served from jsDelivr with SRI through a generated
importmap, so import it by bare name inside a module script:
`<script type="module">import { gsap } from 'gsap';</script>`. A package in
`rejected` must not be imported.

### Verify loop — never call it ready unless it passed

Every create and edit result carries `verification`: `{status, reason?,
layers, errors, warnings}`, from a static check, a real build and a
headless-browser load of the built pages. It is your only evidence the draft
works.

- **`passed`** — only now tell the user the site (or the change) is ready.
- **`failed`** — fix the listed `errors` (each names `file`, `line`,
  `message`), then call `mcp__pocketpaw_sites_manager__verify_site(pocket_id)`.
  At most **3** fix-and-verify rounds; if errors remain, tell the user plainly
  which ones are left. Never call it ready.
- **`unverified`** — it could not be checked (`reason`, e.g.
  `sandbox_unavailable`, `timeout`). Say the draft is saved but unchecked, and
  why. On `timeout` the build is still running, so one more `verify_site` is
  worth it. Never report `unverified` as a pass.
- **`warnings`** don't block a pass, but move a `top_level_client_import`
  client-side anyway: it is how a prerender build breaks.

### When a site needs more than one page

One page is still right for a landing page: one story, one nav of `#anchors`,
one CTA. Author a SECOND page when the brief names something a visitor would go
looking for on its own — a service line with real copy behind it, a menu or
price list, a catalogue, a portfolio, a genuine About / Team / Contact story, or
anything the user asked for by name ("and a page for our team"). A section that
runs three sentences is a section; don't split a thin page into four thin ones.

Each extra page is a **directory with its own `index.html`**, which is what
gives it a clean URL:

```
index.html            →  /
about/index.html      →  /about/
menu/index.html       →  /menu/
styles.css               shared by all of them
```

Link them **root-absolute with the trailing slash** — `href="/about/"`,
`href="/styles.css"`. A relative `about/` means something different depending on
which page wrote it: on `/menu/` it points at `/menu/about/`, which does not
exist. Every page is a whole document — its own `<head>`, its own stylesheet
link, the same nav and footer markup as the rest.

**Author the page before you link it.** Nothing checks that a nav `href`
resolves — not the create tool, not the build, not the deploy. A nav linking
`/about/` with no `about/index.html` in the `source` map ships as a live 404 on
the visitor's first click while every layer reports success. Put every page in
the same `source` map as the nav that points at it.

### Sections from paw-fx

Before you hand-write a hero, an animated background or a scroll effect, check
the registry. It serves finished sections: WebGL and shader backgrounds,
three.js heroes, particle fields, scroll reveals, kinetic text, cursor effects,
page transitions. On this engine every one of them is available, vendored
dependency and all.

1. `mcp__pocketpaw_fx__search_effects("<what you want>")`, or
   `list_effect_categories()` to browse what exists.
2. `mcp__pocketpaw_fx__get_effect(name)` returns `files`, `snippet` and `usage`.
3. Write **every** entry of `files` into the `source` map at its given `path`,
   verbatim. The paths already carry their `_fx/` prefix. Do not rename them, do
   not reformat the code, do not "improve" it.
4. Place `snippet` where that section goes in the page markup.
5. Follow `usage` for the stylesheet `<link>` and the `<script type="module">`
   mount call.

**The snippet already carries its own resting state.** It is a finished section,
not a fragment. Do not wrap it in your own hero markup, and do not restyle it
into your palette by editing the effect's CSS. Tune it through the documented
`--fx-*` custom properties and the effect's `options` instead.

**The `_fx/` paths are root-absolute (`/_fx/...`) on purpose. Copy them as
given.** An html site is served from the origin root, so the same section works
on `/index.html` and on `/blog/post.html` without changing anything. Rewriting
them to `./_fx/...` is what breaks the nested page.

### The lead form on this track

There is no `/api/submit` here — an html site deploys as a static asset tree
with no server route — so a `<form>` with no `action` posts to the page itself
and the lead is lost with no error. Author the form exactly like this:

```html
<form method="POST" action="__CAPTURE_API_BASE__/capture/form">
  <input type="hidden" name="paw_site_id" value="__SITE_ID__">
  <input type="hidden" name="paw_key" value="__CAPTURE_SIGNED_KEY__">
  <input type="hidden" name="paw_redirect" value="/thank-you.html">

  <input name="full_name" required>
  <input type="email" name="email" required>
  <input type="tel" name="phone">
  <textarea name="message"></textarea>

  <button type="submit">Send</button>
</form>
```

**Write the three `__TOKENS__` exactly as shown** — they are placeholders that
publish substitutes with the real capture URL, site id and signed key. You have
none of those values while authoring, so never invent one or leave `action`
empty.

**The field names are fixed**: `full_name`, `email`, `phone`, `message`. A
field named anything else is stored empty, and the business never sees what the
visitor typed. The NAMES are the contract — labels, grouping, order, wording and
styling are yours to decide.

**`paw_redirect` exists because this form does.** It must be a relative path on
this site (an absolute URL is rejected with a 400), so whatever page it names has
to be another entry in the `source` map. A site that has no form of this kind has
no submission to acknowledge, so it needs neither the token nor the extra page.

No JavaScript: this is a plain native browser POST, so never add an onSubmit
handler or a `fetch`.

### Changing an html site that already exists

When the user asks for a CHANGE to a site they already have — "shorten the
headline", "fix the phone number in the footer", "add an about page" — call
`edit_html_file`, not `create_html_site`. Calling create again mints a SECOND
site pocket at a SECOND url and leaves the site they are looking at untouched,
which reads to them as the change silently not working.

```
edit_html_file(
  pocket_id = "<the existing site's pocket id>",
  file_path = "index.html",
  edits     = [{ "old_string": "555-0100", "new_string": "555-0199" }]
)
```

Prefer `edits` (a list of `{old_string, new_string}` blocks, like the built-in
Edit tool) over `new_source`. Each `old_string` must match the current file
EXACTLY ONCE, so read the file first and copy the text verbatim; include enough
surrounding context to be unique. On this track the saving is at its largest —
an html page is one flat document, so "rewrite the file" means re-emitting the
whole page to change a phone number.

To ADD a page, call it twice: once with `create=true` and `new_source` for the
new file, then once with `edits` on `index.html` to link to it.

The tool tells you when that second call is still outstanding. After a `create`
the response carries `unreferenced: true` if nothing in the site links to the
file you just wrote — the page is written and will deploy, but no visitor can
navigate to it, and every page they can reach looks exactly as it did. Add the
link before you report the page as added. It is not an error: `ok` is still true
and the file really was written, so do not retry the create.

**Paths are the same ones you authored** — `index.html`, `styles.css`,
`about.html`, `img/logo.svg`. Do not prefix with `src/`; that is the react
track. The only unwritable path is the generated `_paw/` namespace.

**Leave the form plumbing alone** unless the user is asking to change the form
itself. The `action` and the hidden `paw_site_id` / `paw_key` / `paw_redirect`
inputs are what make a submission arrive as a lead; a rewrite that drops them
still renders and still submits, and every future enquiry goes nowhere.

The edit is saved to the site's DRAFT — it is not published. Its result
carries `verification`, and the verify loop above applies. Tell the user the
change is in the draft they can preview under /sites and offer to publish it;
only call `publish` when they ask.

**Do not reach for this by default.** Unless the user explicitly wants raw
HTML, or the site genuinely needs more than one page, use the copy-only
`create_landing_site` path below.

## Why copy-only (and why this is the reliable path)

Earlier versions of this skill asked the agent to draft the rippleSpec
itself (or route it through the pocket specialist's create/redraft loop).
That path kept silently downgrading the page to generic
`hero + grid + card + quote` widgets — the marketing widgets got dropped
between drafting and persistence. The deterministic tool removes the whole
failure mode: **the LLM provides copy, code owns structure.** There is
nothing left to downgrade.

So: **do not call `get_widget_spec`. Do not draft a `rippleSpec`. Do not
call `pocket_specialist__create`. Do not delegate to a subagent.** Build
the `content` object and call `create_landing_site`.

## STEP 1 — Write the `content` copy object

`content` is COPY ONLY — words, never structure. Every field is optional;
the tool fills any gap with plausible copy, but a good page comes from you
giving it real, on-domain words. The shape:

```json
{
  "brand": "<the business name>",
  "hero": {
    "eyebrow": "<short category line>",
    "title": "<the headline promise>",
    "subtitle": "<one sentence on the offer>",
    "cta_label": "<primary button label>"
  },
  "services": [
    { "title": "<service name>", "desc": "<one line on what it covers>", "icon": "tooth" },
    { "title": "<service name>", "desc": "<one line on what it covers>", "icon": "sparkles" },
    { "title": "<service name>", "desc": "<one line on what it covers>", "icon": "smile" },
    { "title": "<service name>", "desc": "<one line on what it covers>", "icon": "shield" }
  ],
  "testimonials": [
    { "quote": "<a quote the user gave you, verbatim>", "author": "<who said it>", "role": "<who they are>" },
    { "quote": "<a quote the user gave you, verbatim>", "author": "<who said it>", "role": "<who they are>" }
  ],
  "tiers": [
    { "name": "<tier name>", "price": "<price, no currency symbol>", "period": "<billing period>", "features": ["<what is included>", "<what is included>", "<what is included>"], "cta_label": "<button label>" },
    { "name": "<tier name>", "price": "<price, no currency symbol>", "period": "<billing period>", "features": ["<what is included>", "<what is included>", "<what is included>"], "popular": true, "cta_label": "<button label>" },
    { "name": "<tier name>", "price": "<price, no currency symbol>", "period": "<billing period>", "features": ["<what is included>", "<what is included>", "<what is included>"], "cta_label": "<button label>" }
  ],
  "cta_band": {
    "headline": "<the mid-page nudge>",
    "subtext": "<one supporting line>",
    "button_label": "<button label>"
  },
  "contact": {
    "address": "<street address>",
    "phone": "<phone number>",
    "email": "<email address>"
  },
  "footer": { "copyright": "© <year> <the business name>" }
}
```

### Field reference

- **`brand`** — the business name. Shows in the navbar and footer.
- **`hero`** — `eyebrow` (short category line), `title` (the headline
  promise), `subtitle` (one sentence on the offer/location/why-easy),
  `cta_label` (the primary button label — the tool wires it to the lead
  form anchor).
- **`services`** — a list of `{title, desc, icon}`. `icon` is a **lucide
  icon name** (e.g. `tooth`, `sparkles`, `shield`, `smile`); omit it and
  the tool picks one. Variable length — give as many as the business has.
- **`testimonials`** — a list of `{quote, author, role}`. Use only what the
  user gave you. Variable length.
- **`tiers`** — pricing plans: `{name, price, period, features[],
  popular?, cta_label}`. `price` is a string or number **without** a
  currency symbol (the tool renders `$`); `features` is a list of strings;
  mark the recommended tier `"popular": true`. Variable length.
- **`cta_band`** — the mid-page conversion nudge: `{headline, subtext,
  button_label}`.
- **`contact`** — `{address, phone, email}`. Feeds the footer and the lead
  form placeholders.
- **`footer`** — `{copyright}` (the legal line).

### Copy rules — no placeholder text, no invented facts

**Placeholder text must never ship.** "TBD", "Lorem ipsum", `[insert here]`
and the like are defects: every word that reaches the page is a word a
visitor will read.

**A fact you were not given is not yours to supply.** Prices, addresses,
phone numbers, email addresses, hours, statistics, credentials, and words
quoted from a named person are claims about a real business that the user
is about to publish under their own name. A specific-looking value they
never told you is a defect, not a finishing touch — and it is worse than an
empty field, because it looks settled and ships unread.

So when the brief is missing something a field needs, write an obviously
generic stand-in (`Your street address`, `Price on request`) and say plainly
in your reply which fields you filled that way and what you need back. You
can also just omit the field — but the tool fills a gap with plausible copy
of its own, so for anything that carries a fact, prefer the stand-in you can
point at.

Whatever the user DID give you ships verbatim.

The JSON above shows the shape of each field. It is not content to reuse.

## STEP 2 — Call `create_landing_site`

Hand the copy to the tool. It assembles the fixed marketing structure and
persists the pocket stamped `type="site"` + `pattern="landing"`.

```
mcp__pocketpaw_sites_manager__create_landing_site(
  content = <the copy object from STEP 1>,
  name    = "<the business name>"      // optional; defaults to content.brand
)
```

It returns `{ ok, pocket_id, pocket }`. Keep `pocket_id` for STEP 3. If
`ok` is false, relay the error — do **not** claim a phantom create and do
**not** fall back to drafting a spec yourself.

## STEP 3 — Stop at the draft (publish only when asked)

**Default: create the draft, do NOT publish.** After `create_landing_site`
returns, the pocket exists as a reviewable **draft** the user can preview
in-app (open **/sites** → the site's **Preview** tab). Publishing deploys it to
the public edge (and, on a paid tier, can open a checkout), so taking it live
is the user's call, not an automatic step.

So **do NOT call `publish` by default.** Tell the user the draft is ready,
point them at the Preview, and offer to take it live — e.g. *"Your site is
ready as a draft. Preview it under /sites, and say **publish** (or 'make it
live') when you're happy with it."* Then stop.

**Publish in this same turn ONLY if the user's request already asked to go
live** — "publish it", "make it live", "ship it", "put it online":

```
mcp__pocketpaw_sites_manager__publish(pocket_id = <the id from STEP 2>)
```

Show the user the returned `url` plus a pointer to **/sites**. Relay any
`ok: false` error — never claim a phantom publish.

If you arrived here from the `pocketpaw-create-site` skill's Path B, the user
already asked to publish, so that skill owns the publish call — return the
created `pocket_id` to it instead of publishing here.

## What the tool builds (so you know what your copy becomes)

You don't assemble any of this — it's here so you understand how your copy
maps to the page. The tool emits, top to bottom:

| # | Section | Built from your copy |
|---|---------|----------------------|
| 1 | **navbar** (sticky, brand + anchor links + CTA) | `brand`, `hero.cta_label` |
| 2 | **hero** (eyebrow + title + subtitle) | `hero` |
| 3 | **feature-grid** under `#services` | `services[]` |
| 4 | **testimonial** per quote under `#reviews` | `testimonials[]` |
| 5 | **pricing-table** (`tiers`) under `#pricing` | `tiers[]` |
| 6 | **cta** band | `cta_band` |
| 7 | **flat lead form** in a `#book` card (input/textarea/submit) | `contact`, `cta_band.button_label` |
| 8 | **footer** (titled columns + copyright) | `contact`, `footer`, `brand` |

The page is **prerendered static HTML** (`csr=false`) — no client JS runs
for the visitor on first paint. The tool bakes in every SSR rule by
construction, so you never have to think about them:

- The lead form is **flat** `input` / `textarea` / `button{type:submit}`
  with real `name`s — never a `form`/`newsletter` widget (which would emit
  a nested `<form>` and capture nothing). It rides the page's outer form
  and POSTs natively.
- Every CTA is an **anchor `href`** (`#book`, `tel:`, `mailto:`), never an
  `on_click` (a dead button on a static page).
- `pricing-table` uses `tiers` with a `$` currency symbol; the popular
  tier is highlighted.
- Anchor targets (`#services` / `#reviews` / `#pricing` / `#book`) live on
  wrapping `section` / `card` nodes, because the marketing widgets carry
  no `id` of their own.
- No dashboard widgets — no KPI `stat` grid, no charts, no `accordion`. A
  marketing page, not an internal tool.

Because all of that is fixed in code, the page can't be downgraded, and
your copy is the only variable.

## Related tools (via MCP)

- `mcp__pocketpaw_sites_manager__create_landing_site` — **the create
  step.** Pass the `content` copy object; the tool assembles the marketing
  page and persists it stamped `type="site"` + `pattern="landing"`.
  Returns `{ok, pocket_id, pocket}`.
- `mcp__pocketpaw_sites_manager__publish` — publish the pocket as a live
  site; show the user the `url`. Call it only when the user asks to go live
  (draft-first — STEP 3); a plain "create a site" stops at the draft.
- `mcp__pocketpaw_sites_manager__set_site_dependencies` / `verify_site` —
  declare npm packages on an html site; re-run its checks after a fix.
- `mcp__pocketpaw_pocket__list_pockets` — find an existing pocket if the
  user named one rather than describing a new site.
- `mcp__pocketpaw_fx__search_effects` / `get_effect` / `list_effect_categories` —
  drop-in visual effects (animated backgrounds, particles, 3D heroes, scroll /
  text / cursor effects, transitions). `get_effect` returns `files` to write
  verbatim under `_fx/` plus a `snippet` to place; never hand-roll an effect
  the registry already has.
