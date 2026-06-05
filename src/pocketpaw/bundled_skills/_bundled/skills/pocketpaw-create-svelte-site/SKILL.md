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
`type="site"` + `pattern="landing"` + `engine="svelte"` with your map as
`source` — directly, with no rippleSpec and no specialist.

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
  Pass the `source` map you authored; the tool persists the pocket stamped
  `type="site"` + `pattern="landing"` + `engine="svelte"`. Returns
  `{ok, pocket_id, pocket}`.
- `mcp__pocketpaw_sites_manager__publish` — publish the pocket as a live
  site; show the user the `url`.
- `mcp__pocketpaw_pocket__list_pockets` — find an existing pocket if the
  user named one rather than describing a new site.
