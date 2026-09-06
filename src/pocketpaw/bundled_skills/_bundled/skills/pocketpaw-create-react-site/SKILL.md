---
name: pocketpaw-create-react-site
description: |
  Build a marketing landing page as a Paw Site on the REACT TRACK — a real,
  standalone website you author as hand-written React components, PRERENDERED
  to static HTML at build time and deployed to the edge. Decide by the
  REQUIREMENT: reach for this when the user EXPLICITLY asks for React ("build
  it in React", "use React", "a React landing page") or when the page genuinely
  needs React-shaped client interactivity. Do NOT default to it for a plain
  static marketing page ("build a dentist landing page", "a marketing page for
  my bakery") — that is the common case and belongs to the html track
  (create_html_site) or pocketpaw-create-paw-site. When you DO use it: YOU
  write the React components (Hero, Pricing, Faq, ...) at the quality bar set
  by pocketpaw-design-taste, assemble them into a source map rooted at
  src/App.tsx, and a deterministic tool persists the pocket stamped
  type="site" + pattern="landing" + engine="react". You do NOT compose a
  rippleSpec, do NOT call get_widget_spec, do NOT use the pocket specialist.
  Sites ship their client JavaScript by default, so a React page with client
  state (a menu toggle, tabs, a counter) hydrates and works; still pass
  interactive=true to record the intent, and interactive=false to opt a purely
  static page out of shipping a bundle it never uses.
  Loading this skill keeps the chat agent's always-on system prompt
  small while still delivering the full React authoring brain when a site
  actually needs React.
---

# Build a Paw Site — the React-track authoring brain

You're building a **Paw Site** on the **React track**: a real, standalone
marketing website that you **author as hand-written React components** and that
gets **prerendered to static HTML at build time** and deployed to the edge.

This is the sibling of `pocketpaw-create-svelte-site` and the html track. The
difference is the **payload**:

| | `create_html_site` (html) | `create-svelte-site` (svelte) | **`create-react-site` (this skill)** |
|---|---|---|---|
| What you provide | a raw `{path: contents}` HTML/CSS/JS tree | SvelteKit components | **React components** |
| Composition root | `index.html` | `src/routes/+page.svelte` | **`src/App.tsx`** |
| Build step | none | SvelteKit build | **Vite + a prerender pass** |
| Persisted as | `engine="html"` | `engine="svelte"` | `engine="react"` |

**The one rule that changes everything: there is NO rippleSpec and NO
catalog.** You are not picking widgets from a catalog or drafting a spec a
validator gates. You write premium React — a `Hero`, a `Pricing`, an `Faq` —
exactly as a senior frontend engineer would, and hand the files to a
deterministic tool. The component files ARE the page.

So: **do NOT call `get_widget_spec`. Do NOT draft a `rippleSpec`. Do NOT call
`pocket_specialist__create`. Do NOT delegate to a subagent.** Author the
components, assemble the `source` map, and call `create_react_site`.

## The quality bar

**Run `pocketpaw-design-taste` FIRST — before you author a line.** It is the
engine-agnostic creative-director system: the Vision Ledger and the one-line
Design Read, the Trend Engine identity, the aesthetic direction family, the
three dials, the layout-variance and materiality rules, the CSS-first motion
vocabulary, and the anti-slop copy discipline. Everything it says about taste
applies here unchanged — this skill does not restate it and does not override
it. Author the sections honouring the direction it produces.

The only thing THIS skill owns is what React does differently: the prerender
contract, the source-map shape, and the interactivity flag below.

Write **real, concrete copy** — never "TBD" or "Lorem ipsum". A dentist gets
real service names, real testimonial quotes with names, real tier prices.

## ⚠️ THE PRERENDER AUTHORING RULE (read this before you write a line)

The page is **prerendered**: at build time your `<App />` is rendered to HTML
by `react-dom/server`, **before any browser JavaScript runs**. `useEffect` does
**NOT** run at prerender time — it only runs later, in the browser, and only if
the site keeps its client bundle.

**Therefore: every component MUST render its resting / final state in its
RETURNED MARKUP. Never produce the resting state only in an effect.**

The classic trap is a count-up:

```tsx
// ❌ WRONG — the prerendered HTML bakes "0"
export default function Backers() {
  const [n, setN] = useState(0);              // start state
  useEffect(() => { /* animate up to 128 */ }, []);  // never runs at prerender
  return <span>Backers: {n}</span>;           // baked as "Backers: 0"
}
```

```tsx
// ✅ RIGHT — the prerendered HTML bakes the real "128"
const TOTAL = 128;
export default function Backers() {
  const [n, setN] = useState(TOTAL);          // RESTING state in markup
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    setN(0);                                  // restart on the client only
    /* animate up to TOTAL */
  }, []);
  return <span>Backers: {n}</span>;           // baked as "128", animates on hydrate
}
```

The same rule covers every animated/interactive default:

- **Scroll-reveal:** the revealed element and its text live in the returned
  markup; the observer only adds a class. Never gate the *content* behind the
  observer. Respect `prefers-reduced-motion` by revealing immediately.
- **Accordions / tabs / carousels:** the open/active item is the `useState`
  *initial value*, so the first panel renders in the prerendered HTML.
- **Anything `window`/`document`:** those globals do not exist during the
  server render. Touch them inside `useEffect`, or behind a
  `typeof window !== 'undefined'` check — never at module top level and never
  in a component body.

If you're unsure whether the resting frame is right, ask: *"with all JavaScript
disabled, does this section look finished?"* If not, move the final state into
the returned markup.

## ⚠️ THE INTERACTIVITY FLAG — read this before you ship a menu toggle

**Sites ship their client JavaScript by DEFAULT.** React hydrates on top of the
prerendered markup, so a menu toggle, tabs or a counter work without you passing
anything. (This is a deployment setting — `sites_keep_client_bundle_default` —
so it can be turned off, which is why the rest of this section still matters.)

**Pass `interactive=true` explicitly whenever any component you authored has
behaviour that must run in the browser.** It is no longer the difference between
working and inert on a default deployment, but it RECORDS the intent, and an
explicit value beats the setting — so your interactive page keeps working even
where the default is off. Concretely, if any of these appear anywhere in your
source map, the site is interactive and you should say so:

- `useState` / `useReducer` whose value **changes** after first render
- any `onClick` / `onChange` / `onSubmit` handler that does something
- `useEffect` (a scroll-reveal observer, an animation, a media-query listener)
- a `<canvas>` you draw into, or any third-party widget you mount

**Pass `interactive=false` when the page is purely static** — copy, images, CSS
hover/keyframe motion, anchor links, and a native `<form method="POST">`. Those
work with no JavaScript at all, and an explicit `false` is now the ONLY way to
ship the smaller, faster, bundle-free page: omitting the argument gets you the
default, which ships the bundle. This is the one direction that changed — it
used to be the do-nothing case and is now a decision worth making.

CSS-only motion (the design system's Tier-0 default: hover states, keyframe
drift, marquees, `@media (prefers-reduced-motion)`) does **NOT** need the flag —
it runs at paint with no JavaScript.

**The resting-visibility trade.** A build that ships JavaScript is no longer
refused for hiding content at rest, because the gate assumes the JS can reveal
it. That check was a real safety net against a page that renders blank until an
effect fires, and on a default deployment it no longer fires for you. The
prerender rule below is now the only thing standing between you and a
blank-looking page — treat it as load-bearing, not advisory.

**The flag never replaces the prerender rule.** With `interactive=true` the page
is still prerendered and React *hydrates* on top of the baked markup. A
component whose resting state is only set in an effect is still wrong — it just
flashes instead of staying broken. Both rules apply, always.

## STEP 1 — Author the components

Write the page as React components under `src/components/`, one per section, in
conversion order. A strong default funnel:

`Nav` → `Hero` → `Features` → `HowItWorks` → `Pricing` → `Testimonial` →
`Faq` → `FinalCta` → `Footer`

`src/App.tsx` is the **composition root** — it imports the sections, imports
your stylesheet, and returns them in order. It is the one file the generator
requires, because both generated entries (client and server) import it.

Each component is a normal function component: props typed inline, the markup
returned, and its styling from your stylesheet (plain CSS — there is no CSS
framework installed; see "What the project has" below).

Conversion essentials the page should carry:

- A **hero** with a real headline promise + subtitle + a primary CTA that is an
  **anchor** (`href="#pricing"`), not a bare `onClick` button.
- A **pricing** section with real tiers and a highlighted recommended tier.
- A **lead form** — see "Lead capture" below; it is a native form POST, not an
  `onSubmit` handler.
- Every CTA is an anchor (`#book`, `tel:`, `mailto:`).

### Sourcing photography (real images, not placeholders)

Same rule as every track: do **not** invent image `src` paths and do **not**
ship a photo-free wireframe when the page calls for imagery.

0. Call **`mcp__pocketpaw_sites_manager__list_site_assets`** with the
   `pocket_id` FIRST. These are the images the owner uploaded for this site —
   their logo, their product, their team. A real logo beats any stock photo,
   and shipping a stock face when the owner supplied their own is the single
   most obvious way a generated site reads as generic. Its `url` is absolute,
   public and permanent: put it in `src` verbatim, never copy the file into the
   source map. If the user mentions "my logo" or "the image I uploaded", this
   is where it is. An empty list just means they uploaded nothing — carry on.
1. Call **`mcp__pocketpaw_stock__search_stock_images`** for whatever the owner's
   own assets did not cover, with a **generic, descriptive** query — `"modern
   dental office"`, not `"dentist in Akron"`.
   Pass `orientation` (`landscape` for heroes) and a small `count`.
2. It returns `{url, alt, credit, credit_url, provider}` per photo. **Embed the
   `url` directly** as the `src` of a plain `<img>` (or a CSS
   `background-image`). It is a provider CDN link that survives the static
   build — no upload step.
3. **Always set `alt`** from the returned `alt`, and **render the `credit`**
   line near the image or in the footer. The providers' terms require it.
4. If it returns an **empty list**, fall back to a tasteful gradient/solid
   treatment rather than a broken `<img>`. Never fabricate a photo URL.
5. Set `width`/`height` (or `aspect-ratio`) on every image so the page does not
   jump as assets load.

**Video and other media are allowed.** If the brief's asset manifest carries a
`video` asset (or the user hands you one), use it at its native medium - a
`<video autoplay muted loop playsinline>` hero backdrop over a poster or CSS
fallback is a first-class choice, not something to skip or flatten to a still.
There is no approved-media list here. The only asset rule is that the URL has to
be one you were actually given rather than one you invented.


### Lead capture (there is no `/api/submit` on this track)

The React track deploys as a **purely static asset tree** — there is no server
route, so the SvelteKit skeleton's `/api/submit` endpoint does not exist here.
This matches the html track.

There **is** a capture endpoint, and it is the shared one. Post to it natively:

```jsx
<form method="POST" action="__CAPTURE_API_BASE__/capture/form">
  <input type="hidden" name="paw_site_id" value="__SITE_ID__" />
  <input type="hidden" name="paw_key" value="__CAPTURE_SIGNED_KEY__" />
  <input type="hidden" name="paw_redirect" value="/thank-you" />

  <label>Your name<input name="full_name" required /></label>
  <label>Email<input type="email" name="email" required /></label>
  <label>Phone<input type="tel" name="phone" /></label>
  <label>How can we help?<textarea name="message" /></label>

  <button type="submit">Send</button>
</form>
```

**Write the three `__TOKENS__` exactly as shown.** They are placeholders —
publish substitutes the real capture URL, site id and signed key. You do not
have those values while authoring (on a create the site does not exist yet, and
the key is minted at publish), so never invent one or leave the action empty.

**The visible field names are fixed**: `full_name`, `email`, `phone`, `message`.
They are the names the lead pipeline maps; a field named anything else is stored
empty and the business never sees what the visitor typed.

**`paw_redirect` must be a relative path on this site** — an absolute URL is
rejected with a 400 — so author the page it points at (a small `thank-you`
route or `thank-you.html` confirming the message was sent).

Do **not** wire an `onSubmit` handler that fetches. A static page should capture
the lead whether or not JavaScript ran, and this is a plain native browser POST.

### What the project has (and what it does not)

The generator writes a deliberately tiny project: **react**, **react-dom**,
**vite**, and **@vitejs/plugin-react**. That is all.

There is **no router**, **no CSS framework**, **no state library**, **no
animation library** installed, and you cannot add dependencies — the package
manifest is generator-owned and dependency-allowlisted. Write plain components
and plain CSS. The site is **one page**; multi-route React is not supported on
this track.

## STEP 2 — Assemble the `source` map

`source` is a flat object: **`{ relative_path: file_contents }`**, paths
relative to the project root, values are file contents as strings.

**Required key:**

```
src/App.tsx                  the composition root — imports the stylesheet + renders the sections
```

Add as needed:

```
src/components/*.tsx         your section components (Hero.tsx, Pricing.tsx, Faq.tsx, ...)
src/index.css                the design system — tokens (CSS vars), @font-face / font imports, base reset
public/*                     static assets served at the site root
```

**Paths you may NOT write** — the generator owns them, and writing one fails the
create:

```
index.html                   the HTML template (carries the prerender outlet)
package.json                 the dependency manifest (allowlisted)
vite.config.ts               the build config
paw-prerender.mjs            the prerender pass
src/paw/**                   the generated client + server entries
```

That reservation is what guarantees the page cannot silently become a
blank-without-JavaScript SPA shell. Import your stylesheet from `src/App.tsx`
(`import './index.css';`) — there is no `index.html` of yours to link it from.

A minimal valid map:

```json
{
  "src/App.tsx": "import './index.css';\nimport Hero from './components/Hero';\n\nexport default function App() {\n  return (\n    <main>\n      <Hero />\n    </main>\n  );\n}\n",
  "src/components/Hero.tsx": "export default function Hero() {\n  return (\n    <section className=\"hero\">\n      <h1>...</h1>\n    </section>\n  );\n}\n",
  "src/index.css": ":root { --ink: #17130f; ... }\n/* fonts, reset, base type */\n"
}
```

Notes that the build enforces, so get them right:

- Every component you import from `src/App.tsx` must exist as a key in the map
  — a missing import fails the build.
- Values are **strings**. Keep real newlines and indentation; this is source.
- Use `className`, not `class`. This is JSX, not HTML.

## STEP 3 — Call `create_react_site`

Hand the source map to the tool. It persists the pocket stamped `type="site"` +
`pattern="landing"` + `engine="react"` with your map as `source` — directly,
with no rippleSpec and no specialist.

```
mcp__pocketpaw_sites_manager__create_react_site(
  source      = <the source map from STEP 2>,
  name        = "Bright Smile Dental",   // optional; defaults to "React site"
  interactive = true                     // declare it: true when any component
                                         // needs the browser, false to opt a
                                         // purely static page out of the bundle
)
```

**`interactive`** is the one argument you must think about — see the
interactivity flag section above. Set it to `true` when any component has client
behaviour; leave it off for a purely static page. Getting it wrong is silent:
the site builds, deploys and looks right, and the menu just never opens.

It returns `{ ok, pocket_id, pocket }`. Keep `pocket_id` for STEP 4. If `ok` is
false, **relay the error** — do **not** claim a phantom create and do **not**
fall back to another engine. The tool fails closed when the map is missing
`src/App.tsx` or writes a reserved path, and names which one; fix it and retry.

## STEP 4 — Stop at the draft (publish only when asked)

**Default: create the draft, do NOT publish.** After `create_react_site`
returns, the pocket exists as a reviewable **draft** under **/sites**.
Publishing deploys it to the public edge (and, on a paid tier, can open a
checkout), so taking it live is the user's call.

⚠️ **On the react track the draft shows its SOURCE, not the page.** The in-app
Preview has a client-side render lane for svelte and for html, and none for
react — a react draft falls through to the code viewer, so what the user sees
under /sites is your `.tsx` files. The rendered page needs the Vite build, which
only a publish runs. **Do not tell the user they can look at their page.** Say
the draft is ready, be specific that /sites shows the source until it is built,
and offer to build and publish it.

So **do NOT call `publish` by default.** e.g. *"Your Bright Smile site is ready
as a draft — you'll find its code under /sites; React sites render once they're
built, so say **publish** when you want me to build it and put it live."* Then
stop.

**Publish in this same turn ONLY if the user's request already asked to go
live** — "publish it", "make it live", "ship it", "put it online":

```
mcp__pocketpaw_sites_manager__publish(pocket_id = <the id from STEP 3>)
```

The generator materializes your `source` onto the React skeleton, runs the Vite
build, prerenders `<App />` into the HTML, and deploys the static output. Relay
any `ok: false` error — never claim a phantom publish.

⚠️ **A react publish does NOT return a live site, and its `url` is not one.**
React is the only engine whose build runs **off-request**: `publish` queues the
build in an ephemeral sandbox, returns immediately, and a worker deploys it
later. The response's `url` / `deployed` describe the site as it stands *before*
the new build lands, and they lie in both directions:

- **First publish** — `deployed: false` and `url: ""`, because nothing is
  serving yet. There is no link to give. Do **not** show an empty url, and do
  **not** invent one.
- **Re-publish** — `url` and `deployed` keep the **previous** deploy's values, so
  the live site is never reported as down mid-rebuild. That url serves the
  **old** content. Presenting it as the new page is the more damaging mistake,
  because it looks like it worked.

So report the build as **queued**, say that react builds finish after the
publish call returns, and point the user at **/sites**, where the build status
and the final url appear. Never present a url from a publish response as live
without a build status that says it is. ("Your site's build is queued — it'll
appear under /sites once it finishes, usually a minute or two.")

**If the build fails on the prerender pass**, the message names the cause. The
common ones are a `window`/`document` touched during render (guard it) and a
component that returns nothing at rest. Both are the prerender rule above.

### If the user then asks for a CHANGE — edit, never re-create

"Shorten the headline", "make the nav sticky", "add a testimonials section" is
an **edit of the site you just made**, not a new one. `create_react_site` has no
update mode: calling it again mints a **second** site pocket and leaves the one
the user is looking at untouched. Edit the existing pocket instead:

```
mcp__pocketpaw_sites_manager__edit_react_component(
  pocket_id      = <the id from STEP 3>,
  component_path = "src/components/Hero.tsx",
  edits          = [{"old_string": "<copied verbatim, must match exactly once>",
                     "new_string": "<the replacement>"}]
)
```

- Send a targeted **`edits`** diff, not the whole file. Pass `new_source`
  instead only for a genuine rewrite — **exactly one** of the two per call.
- Each `old_string` must match that file **exactly once**; include surrounding
  context so it is unique.
- **Adding a section is TWO calls**: `create=True` + `new_source` to write
  `src/components/Testimonials.tsx`, then a second call with `edits` on
  `src/App.tsx` to import and render it. Stop after the first and you have
  shipped a component nothing renders.
- The generator-owned paths above stay refused, so an edit **cannot** add a
  dependency.
- Every rule in this skill still binds — above all the **prerender rule**: an
  edit that swaps a static value for a `useState(0)` + count-up effect bakes
  "0" into the shipped HTML.
- **The edit stages a DRAFT**; it does not publish. Do not send the user to look
  at the change — /sites shows a react draft's source, not the rendered page.
  Say what changed, and publish only when they ask.

The full edit brain is `pocketpaw-edit-react-site` — load it when the user is on
the site's own refine chat. The essentials are inlined here because this create
surface loads only the skill you are reading.

## Quality bar — done right when

- The Design Read and direction came from `pocketpaw-design-taste`, and the page
  honours them — not a default clean house style.
- With **all JavaScript disabled**, every section looks finished: real copy,
  real images, the first accordion panel open, counters at their real values.
- `interactive` is declared either way — `true` when something actually needs
  the browser, `false` for a purely static page — rather than left to the
  default. Every interactive component still rests correctly in markup.
- Real copy throughout. No "TBD", no "Lorem ipsum", no invented testimonials or
  fabricated statistics.
- Every CTA is an anchor; the lead form is a native `<form>` with flat named
  fields.
- The source map writes only `src/**` and `public/**` — never a generator-owned
  path.

## Related tools (via MCP)

- `mcp__pocketpaw_sites_manager__create_react_site` — persist the source map (STEP 3)
- `mcp__pocketpaw_sites_manager__edit_react_component` — CHANGE a component of an
  existing react site (STEP 4). The tool for every follow-up edit; never
  re-create.
- `mcp__pocketpaw_sites_manager__publish` — deploy, on explicit request (STEP 4)
- `mcp__pocketpaw_palette__scale_from_color` / `extract_palette` — brand colour
- `mcp__pocketpaw_sites_manager__list_site_assets` — the owner's own uploaded images
- `mcp__pocketpaw_stock__search_stock_images` — real photography
- `mcp__pocketpaw_icons__search_icons` — feature icons
- `mcp__pocketpaw_fx__search_effects` / `get_effect` — drop-in visual effects.
  On this engine only dependency-free effects (empty `needs`) are served; pass
  `needs_js=false` to `search_effects`.
