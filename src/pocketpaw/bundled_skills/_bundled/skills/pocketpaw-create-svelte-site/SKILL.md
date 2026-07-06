---
name: pocketpaw-create-svelte-site
description: |
  Build a marketing landing page as a Paw Site on the SVELTE TRACK — a
  real, standalone website you author as premium hand-written SvelteKit
  components, prerendered statically to the edge. Invoke when the user
  asks for a BRAND-NEW marketing / landing site AND the create flow is on
  the Svelte engine ("Use Svelte pages" toggle → engine="svelte"): "build
  a dentist landing site", "make a marketing page for my bakery", "a
  landing page for my SaaS". This is the component-authoring brain — YOU
  write the Svelte sections (Hero, Pricing, Faq, ...) at the quality bar of
  the proven spike, assemble them into a source map, and a deterministic
  tool persists the pocket stamped type="site" + pattern="landing" +
  engine="svelte" so the published page renders from your components. You
  do NOT compose a rippleSpec, do NOT call get_widget_spec, do NOT use the
  pocket specialist. For the ripple track (the default engine) use
  pocketpaw-create-paw-site; for publishing an EXISTING pocket use
  pocketpaw-create-site. Loading this skill keeps the chat agent's
  always-on system prompt small while still delivering the full Svelte
  authoring brain when a svelte-track site is actually requested.
---

# Build a Paw Site — the Svelte-track authoring brain

You're building a **Paw Site** on the **Svelte track**: a real, standalone
marketing website that you **author as hand-written SvelteKit components**
and that gets **prerendered to static HTML** (server-side, no client JS on
first paint) and deployed to the edge.

This is the sibling of `pocketpaw-create-paw-site`. The difference is the
**payload**:

| | `create-paw-site` (ripple track) | **`create-svelte-site` (this skill)** |
|---|---|---|
| What you provide | a **copy** object | **the Svelte components themselves** |
| Who builds the page | a deterministic assembler → rippleSpec | **you** — you write the markup, styles, motion |
| Persisted as | `engine="ripple"`, `rippleSpec` | `engine="svelte"`, `source` map |

**The one rule that changes everything: there is NO rippleSpec and NO
catalog.** You are not picking widgets from a 150-widget catalog or
drafting a spec a validator gates. You write premium Svelte — a `Hero`
section, a `Pricing` section, a `Faq` — exactly as a senior frontend
engineer would, and hand the files to a deterministic tool. The component
files ARE the page. Nothing downgrades them; nothing validates them
against a widget manifest.

So: **do NOT call `get_widget_spec`. Do NOT draft a `rippleSpec`. Do NOT
call `pocket_specialist__create`. Do NOT delegate to a subagent.** Author
the components, assemble the `source` map, and call `create_svelte_site`.

**Two kinds of svelte site — static and dynamic.** Most of this skill builds
a **STATIC** marketing page (prerendered HTML, no live data). But a svelte
site can also be **DYNAMIC** — backed by the customer's **own live data** (a
per-tenant database), with reads AND writes: a guestbook that lists entries
and lets visitors add one, a public booking list, a submissions board. You
declare the data layer as bindings on the same `source` envelope and author
components that read/write it through generated helpers. The full recipe is in
**[Dynamic svelte sites](#dynamic-svelte-sites--live-data-on-the-svelte-track)**
below — read it when the user wants live data, not a brochure. Everything up
to STEP 4 (authoring, the prerender rule, the source map) applies to both;
the dynamic section layers the data bindings on top. (The ripple track has a
sibling brain, `pocketpaw-create-dynamic-site`, for the same live-data idea —
this skill is the svelte-track equivalent; keep the two data-authoring models
in step.)

## The quality bar

A landing page **sells**. It reads top to bottom as a conversion funnel:
grab attention, explain the offer, prove it, price it, capture the lead.
The proven reference is a hand-crafted Tally invoicing landing page —
distinctive type, a bespoke animated hero graphic, real testimonials, a
priced pricing table, a native FAQ, a lead form. **Match that bar.** Use
the design skills to get there:

- Invoke **`frontend-design`** (or **`taste-skill`**) for the look: a real
  type system, a considered palette, generous spacing, a hero with a
  bespoke visual — not a centered headline on a gradient. Avoid generic
  "AI landing page" aesthetics.
- **Also apply the `design-taste-svelte` skill** while authoring the
  components — it's the Svelte-specific, static-safe taste layer (layout
  variance, materiality, CSS-first motion, and the anti-slop "AI tells"
  list) that sits ON TOP of the chosen design system's tokens. It mirrors
  the prerender resting-state rule below, so its taste never fights the
  static render.
- One **component per section**. The page is an ordered list of sections;
  each section owns its own copy, data, styles, and motion. This is the
  whole edit-model payoff: to refine the hero later you edit one file.

Write **real, concrete copy** — never "TBD" or "Lorem ipsum". A dentist
gets real service names, real testimonial quotes with names, real tier
prices. The page must read like a finished business site.

## ⚠️ THE PRERENDER AUTHORING RULE (read this before you write a line)

The page is **prerendered**: SvelteKit renders each component to static
HTML at build time, **before any JavaScript runs**. `onMount` does **NOT**
run at prerender time — it only runs later, on the client, after hydration.

**Therefore: every component MUST render its resting / final state in
MARKUP. Never set the resting state only in `onMount`.**

If you initialize an animated value to its *start* state and only reach the
*final* state inside `onMount`, the prerendered HTML bakes the **start**
state — the version crawlers and JS-off visitors see is wrong.

The classic trap is a count-up:

```svelte
<!-- ❌ WRONG — prerendered HTML bakes "$0.00" -->
<script>
  let displayed = $state(0);              // start state
  onMount(() => { /* animate up to total */ });  // never runs at prerender
</script>
<span>{fmt(displayed)}</span>             <!-- baked as $0.00 -->
```

```svelte
<!-- ✅ RIGHT — prerendered HTML bakes the real "$3,850.00" -->
<script>
  const total = 3850;
  let displayed = $state(total);          // RESTING state in markup
  onMount(() => {
    if (prefersReducedMotion) return;
    displayed = 0;                        // restart from 0 on the client only
    /* animate up to total */
  });
</script>
<span>{fmt(displayed)}</span>             <!-- baked as $3,850.00, animates on hydrate -->
```

The same rule covers every animated/interactive default:

- **Scroll-reveal:** put the reveal target in the DOM with its content
  already present. The reveal action adds an `.in` class on scroll, but the
  element and its text live in markup either way — never gate the *content*
  behind the observer. (Respect `prefers-reduced-motion` by revealing
  immediately.)
- **Accordions / tabs / carousels:** the open/active item is set in markup
  (`<details open={i === 0}>`, the active tab's panel rendered), not chosen
  in `onMount`.
- **Anything `window`/`document`:** guard it — those globals don't exist at
  prerender. Read them inside `onMount` or behind a `typeof window` check,
  never at module top level.

If you're unsure whether the resting frame is right, ask: *"with all
JavaScript disabled, does this section look finished?"* If not, move the
final state into markup.

## STEP 1 — Author the components (the design skills set the bar)

Write the page as SvelteKit components under `src/lib/components/`, one per
section, in conversion order. A strong default funnel:

`Nav` → `Hero` → `TrustMarquee`/`Features` → `HowItWorks` → `Pricing` →
`Testimonial` → `Faq` → `FinalCta` → `Footer`

Each component is a normal Svelte 5 file: a `<script>` (runes — `$state`,
`$props`, `$derived`), the markup, and a scoped `<style>` block. Lean on
data-driven loops (`{#each services as s}`) so the section's content is one
array to edit, not hand-repeated markup.

Conversion essentials the page should carry:

- A **hero** with a real headline promise + subtitle + a primary CTA that
  is an **anchor** (`href="#book"` / `#pricing`) — not an `on:click`
  button (a dead button on a static page).
- A **pricing** section with real tiers and a highlighted recommended tier.
- A **lead form** so the published site captures leads out of the box. Use
  plain `<input>` / `<textarea>` / `<button type="submit">` with real
  `name`s, POSTing to the skeleton's `/api/submit` endpoint (e.g.
  `<form method="POST" action="/api/submit">`). The skeleton provides
  `api/submit` → Lead; it is track-agnostic and already wired.
- Every CTA is an anchor (`#book`, `tel:`, `mailto:`).

### Sourcing photography (real images, not placeholders)

A marketing page needs real photography — a hero photo, section imagery. Do
**not** invent image `src` paths (they render broken) and do **not** ship a
photo-free wireframe when the page calls for imagery. Instead pull real,
free stock photos:

1. Call **`search_stock_images`** (MCP tool `mcp__pocketpaw_stock__search_stock_images`)
   with a **generic, descriptive** query — `"modern dental office"`,
   `"artisan bakery bread"` — not a hyper-specific one (`"dentist in Akron"`
   returns weak matches). Pass `orientation` (`landscape` for heroes/banners,
   `portrait`/`square` where the layout wants it) and a small `count`.
2. It returns `{url, alt, credit, credit_url, provider}` per photo. **Embed the
   `url` directly** as the `src` of a plain `<img>` (or a CSS
   `background-image`). The URL is a provider CDN link that survives the static
   prerender build — no upload step.
3. **Always set `alt`** from the returned `alt`, and **render the `credit`**
   line somewhere near the image or in the footer (e.g. a small muted
   "Photos by … on Unsplash" line). This is required by the providers' terms.
4. If `search_stock_images` returns an **empty list** (no provider key
   configured, or no match), fall back gracefully to the copy-and-color
   treatment — a tasteful gradient/solid hero — rather than a broken `<img>`.
   Never fabricate a photo URL.

One hero photo plus a couple of section images is plenty — don't over-request.

## STEP 2 — Assemble the `source` map (the §4.3 contract)

`source` is a flat object: **`{ relative_path: file_contents }`**, paths
relative to the SvelteKit project root, values are the file contents as
strings. The generator writes these files onto the paw-sites skeleton and
prerenders.

**The skeleton provides** everything infrastructural — `package.json`,
`svelte.config.js`, `vite.config`, the adapter, `app.html`, and the
`api/submit` lead endpoint. **You provide only** the route + component +
style files. **Required keys:**

```
src/routes/+page.svelte      the composition root — imports + renders the sections in order
src/routes/+layout.svelte    <script>import '../app.css'; let { children } = $props();</script>{@render children()}
src/routes/+page.ts          export const prerender = true;
src/app.css                  the design system — tokens (CSS vars), @font-face / font imports, base reset
src/lib/components/*.svelte   your section components (Hero.svelte, Pricing.svelte, Faq.svelte, ...)
```

Add as needed:

```
src/lib/*.js                 helpers (e.g. reveal.js — a use:reveal scroll action)
```

A minimal valid map therefore looks like:

```json
{
  "src/routes/+page.svelte": "<script>\n  import Nav from '$lib/components/Nav.svelte';\n  import Hero from '$lib/components/Hero.svelte';\n  ...\n</script>\n\n<Nav />\n<main>\n  <Hero />\n  ...\n</main>\n<Footer />\n",
  "src/routes/+layout.svelte": "<script>\n  import '../app.css';\n  let { children } = $props();\n</script>\n\n{@render children()}\n",
  "src/routes/+page.ts": "export const prerender = true;\n",
  "src/app.css": ":root { --ink: #17130f; --green: #2ee08a; ... }\n/* fonts, reset, base type */\n",
  "src/lib/components/Hero.svelte": "<script> ... </script>\n<section class=\"hero\"> ... </section>\n<style> ... </style>\n",
  "src/lib/components/Pricing.svelte": "...",
  "src/lib/components/Faq.svelte": "...",
  "src/lib/components/Footer.svelte": "...",
  "src/lib/reveal.js": "export function reveal(node, options = {}) { ... }\n"
}
```

Notes that the tool enforces, so get them right:

- **`+layout.svelte` MUST import `../app.css`** — that's the single place
  the global token/reset stylesheet loads for every page and component.
- **`+page.ts` MUST set `prerender = true`** — that's what makes the page
  build to static HTML.
- Every component you `import` in `+page.svelte` must exist as a key in the
  map (a missing import breaks the build).
- Values are **strings**. Keep real newlines/indentation; this is source.

## STEP 3 — Call `create_svelte_site`

Hand the source map to the tool. It persists the pocket stamped
`type="site"` + `engine="svelte"` with your map as `source` — directly, with
no rippleSpec and no specialist. It stamps `pattern="landing"` for a static
site, or **`pattern="dynamic"` automatically when your `source` carries
live-data bindings** (`objects`/`sources`/`actions`/`auth` — see the dynamic
section). You don't pass `pattern`; the tool derives it from the bindings.

```
mcp__pocketpaw_sites_manager__create_svelte_site(
  source = <the source map from STEP 2>,
  name   = "Bright Smile Dental"      // optional; defaults to "Svelte site"
)
```

It returns `{ ok, pocket_id, pocket }`. Keep `pocket_id` for STEP 4. If
`ok` is false, **relay the error** — do **not** claim a phantom create and
do **not** fall back to drafting a rippleSpec. The tool fails closed when
the map is missing a required §4.3 file and names which one; add it and
retry.

## STEP 4 — Publish

Publish the new pocket as a site:

```
mcp__pocketpaw_sites_manager__publish(pocket_id = <the id from STEP 3>)
```

The generator materializes your `source` onto the skeleton, prerenders to
static HTML, runs the smoke gate, and deploys. Show the user the returned
`url` plus a pointer to **/sites**. Relay any `ok: false` error — never
claim a phantom publish. (If a component sets resting state only in
`onMount`, the smoke build still passes but the baked HTML is wrong — which
is exactly why the prerender rule above is non-negotiable.)

## Dynamic svelte sites — live data on the svelte track

Everything above builds a **static** page. A **dynamic** svelte site is backed
by the customer's **own live database** (a per-tenant Cloudflare D1), with
**reads and writes**. You still author premium Svelte exactly as above — the
difference is you also **declare a data layer** and **wire components to it**.

### How it works (the contract)

You declare the data layer as **four sibling keys on the same `source`
envelope** that already holds your `{path: contents}` files:

| key | type | what it is |
|---|---|---|
| `objects` | array | the **tables** — each `{ name, fields, primaryKey }` |
| `sources` | array | the **reads** — each `{ name, kind: "data", object, ... }` |
| `actions` | array | the **writes** — each `{ name, object, op }` |
| `auth` | bool | gate the site behind sign-in (omit / `false` = public) |

These live **alongside** your file keys on `source` — not nested, not a
separate argument. `create_svelte_site` peels them off, stamps the pocket
`pattern="dynamic"`, and at publish the generator provisions the D1, runs a
migration from `objects`, compiles read/write **remote functions**, and emits
typed helpers into the **reserved `src/lib/paw/` namespace** (it owns that
folder — never author files under `src/lib/paw/` yourself; it's a build error).

The exact binding shapes (they're validated at generate time — a `source`/
`action` that names an undeclared `object` fails the build):

```jsonc
// objects[] — a table
{ "name": "entry",
  "fields": { "id": "text", "name": "text", "message": "text", "created": "timestamp" },
  "primaryKey": "id" }
// field types: "text" | "integer" | "real" | "boolean" | "timestamp"

// sources[] — a read binding
{ "name": "entries", "kind": "data", "object": "entry",
  "orderBy": "created desc", "limit": 100,
  "refresh": "pocket_open" }   // "pocket_open" | "interval" | "manual" | "live"

// actions[] — a write binding
{ "name": "sign", "object": "entry", "op": "insert" }   // op: "insert" | "update" | "delete"
```

### Authoring components against the data layer

The generator emits typed helpers your components import from `$lib/paw/`. You
do **not** write the D1 queries, the migration, or the remote functions — you
**consume** the generated handles:

- **Read** — `import { useSource } from '$lib/paw/sources';` then
  `const entries = useSource('entries');` gives a reactive handle with a stable
  shape: `entries.loading`, `entries.data` (the rows), `entries.error`, and
  `entries.refresh()`. Use `entries.data` directly in markup
  (`{#each entries.data as e}`). `useSource` is typed to the EXACT source names
  you declared — an unknown name is a compile error.
- **Write** — the simplest, most robust write is a **native form**: each write
  `action` compiles to a SvelteKit remote `form` you spread onto a `<form>` so
  the write works progressively (even with JS off) and re-runs the affected
  source on success. Import the action helper and spread it:
  `import { useAction } from '$lib/paw/actions';` then
  `const sign = useAction('sign');` and `<form {...sign}> ... </form>` with
  plain `name=`d inputs matching the object's non-primary-key fields. (Prefer
  the spread-onto-`<form>` shape over a detached RPC call — it's the
  progressively-enhanced path the generator guarantees.)

### The prerender rule still applies — with a twist

A dynamic source's first paint is the **empty/loading** frame at prerender
time (D1 isn't queried until the client hydrates and `useSource` runs). So:
**render a graceful resting frame in markup** — an empty-state message, a
skeleton, or the form on its own — never gate the section's *structure* behind
`entries.loading`. With JS off, a dynamic section should still show its heading,
its form, and a "no entries yet" line — the live rows fill in on hydrate.

### Auth (optional)

Set `"auth": true` on the `source` envelope to gate the whole site behind
sign-in. The generator scaffolds the signup/login/logout flow + sessions and
guards the data remote functions so reads/writes require a signed-in user. Your
components consume the generated auth surface from `$lib/paw/` (the same
reserved namespace). Leave `auth` off (or `false`) for a public site — the
guestbook below is public.

### Worked example — a public guestbook (1 table, 1 read, 1 write)

A guestbook: visitors see past entries and add their own. One table (`entry`),
one read source (`entries`), one write action (`sign`), public (no `auth`).

**The bindings (siblings on `source`):**

```jsonc
{
  // ... the §4.3 file keys (+page.svelte, +layout.svelte, +page.ts, app.css) ...
  "src/lib/components/Guestbook.svelte": "<the component below>",

  // ── live-data bindings, siblings on the SAME source object ──
  "objects": [
    { "name": "entry",
      "fields": { "id": "text", "name": "text", "message": "text", "created": "timestamp" },
      "primaryKey": "id" }
  ],
  "sources": [
    { "name": "entries", "kind": "data", "object": "entry",
      "orderBy": "created desc", "limit": 100, "refresh": "pocket_open" }
  ],
  "actions": [
    { "name": "sign", "object": "entry", "op": "insert" }
  ]
}
```

**The component (`src/lib/components/Guestbook.svelte`)** — reads via
`useSource`, writes via the action form, resting frame in markup:

```svelte
<script>
  import { useSource } from '$lib/paw/sources';
  import { useAction } from '$lib/paw/actions';

  const entries = useSource('entries');   // { loading, data, error, refresh }
  const sign = useAction('sign');         // spread onto <form> below
</script>

<section class="guestbook" id="guestbook">
  <h2>Guestbook</h2>

  <!-- WRITE: a native form — works with JS off, re-runs `entries` on success -->
  <form {...sign} class="gb-form">
    <input name="name" placeholder="Your name" required />
    <textarea name="message" placeholder="Say hi" required></textarea>
    <button type="submit">Sign the guestbook</button>
  </form>

  <!-- READ: resting frame is in markup; rows fill in on hydrate -->
  {#if entries.error}
    <p class="gb-error">Couldn't load entries. Try again.</p>
  {:else if entries.data && entries.data.length}
    <ul class="gb-list">
      {#each entries.data as e}
        <li><strong>{e.name}</strong><span>{e.message}</span></li>
      {/each}
    </ul>
  {:else}
    <p class="gb-empty">No entries yet — be the first to sign.</p>
  {/if}
</section>

<style>
  /* real styles via the design skills — omitted here for brevity */
</style>
```

Then `+page.svelte` imports and renders `<Guestbook />` like any other section,
and you call `create_svelte_site(source = <the envelope with bindings>)`. The
tool sees the bindings, stamps `pattern="dynamic"`, and publish provisions the
D1 + wires the read/write layer. Done.

## Quality bar — done right when

1. **You authored real Svelte.** Premium hand-written components via the
   design skills, one per section, at the spike's quality — not a thin
   template, not a rippleSpec, not a downgrade.
2. **Resting state is in markup.** With JS disabled the page looks
   finished: the hero total reads the real number, the first FAQ is open,
   reveal content is present. Nothing important lives only in `onMount`.
3. **The source map is complete (§4.3).** `+page.svelte`, `+layout.svelte`
   (imports `app.css`), `+page.ts` (prerender), `app.css`, and the
   `src/lib/components/*.svelte` sections — every import resolvable.
4. **It converts + captures.** CTAs are anchors, there's a real priced
   pricing section, and a flat lead form POSTing to `/api/submit`.
5. **You showed the live URL.** The user got the `url` from publish and a
   pointer to /sites — not just "done". Errors were relayed, never masked.

## Related tools (via MCP)

- `mcp__pocketpaw_sites_manager__create_svelte_site` — **the create step.**
  Pass the `source` envelope you authored; the tool persists the pocket stamped
  `type="site"` + `engine="svelte"`, and `pattern="landing"` (static) or
  `pattern="dynamic"` (when `source` carries `objects`/`sources`/`actions`/
  `auth` bindings — see [Dynamic svelte sites](#dynamic-svelte-sites--live-data-on-the-svelte-track)).
  Returns `{ok, pocket_id, pocket}`.
- `mcp__pocketpaw_sites_manager__publish` — publish the pocket as a live
  site; show the user the `url`.
- `mcp__pocketpaw_pocket__list_pockets` — find an existing pocket if the
  user named one rather than describing a new site.
