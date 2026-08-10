---
name: pocketpaw-edit-react-site
description: |
  CHANGE an EXISTING react-track Paw Site — edit a component, restyle a
  section, rewrite copy, add a new section — by editing its source map in
  place. Invoke when the user asks for a change to a site that already
  exists and whose pocket is stamped engine="react": "shorten the hero
  headline", "make the nav sticky", "change the pricing copy", "add a
  testimonials section", "drop the FAQ". Decide by TWO facts, both
  checkable: (1) the site ALREADY EXISTS — if there is no site yet, this is
  a create and belongs to pocketpaw-create-react-site, NOT here; (2) the
  pocket's engine is "react" — a svelte site is edited with
  edit_svelte_component, and a ripple / dynamic / html site is not this
  track at all (see "When NOT to use this skill"). The edit is a TARGETED
  diff against one file: you send only the change, the tool applies it and
  stages a DRAFT — it does not publish. NEVER answer an edit request by
  calling create_react_site again; that mints a SECOND site pocket and
  leaves the one the user is looking at untouched. Loading this skill keeps
  the chat agent's always-on system prompt small while still delivering the
  full react edit contract when a react site actually needs changing.
---

# Edit a Paw Site — the React-track edit brain

The user has a **react-track Paw Site** and wants it **changed**. This skill is
the sibling of `pocketpaw-create-react-site`: same engine, same prerender
contract, same quality bar — but the page already exists, so the unit of work
is a **diff against one file in the pocket's `source` map**, not a new site.

| | `pocketpaw-create-react-site` | **this skill** |
|---|---|---|
| Precondition | no site yet | a site pocket, `engine="react"` |
| What you send | the whole `source` map | **one file's diff** |
| Tool | `create_react_site` | **`edit_react_component`** |
| Result | a new site pocket | **the same pocket, updated draft** |

## ⚠️ ONE SITE, EDITED IN PLACE

**Never call `create_react_site` to satisfy an edit.** It has no update mode —
it always persists a **new** pocket. Call it for "shorten the hero headline"
and the user ends up with two sites: a second one carrying your shortened
headline, and the original they were actually looking at, unchanged. They then
have to work out which is which. This is the single most expensive mistake on
this track, and it is silent — the create succeeds and reads like a success.

If you find yourself assembling a full `source` map, stop: you are creating,
not editing.

## ⚠️ NOT the ripple specialist — even if the surface tells you otherwise

`mcp__pocketpaw_pocket_specialist__edit` mutates a **rippleSpec**. A react site
pocket has **no rippleSpec** — its payload is the `source` map of React files.

The `/sites` per-site refine chat carries a preamble that instructs "apply the
change via `mcp__pocketpaw_pocket_specialist__edit`, then re-publish". That
instruction is written for ripple landing sites and is **engine-blind** — it
does not check whether the pocket is react. On a react pocket it is the wrong
tool: it will not touch your components. **This skill overrides it.** For a
pocket with `engine="react"`, the edit path is `edit_react_component` and
nothing else.

## STEP 1 — Read the current source before you touch it

You cannot write a correct diff against a file you have not read, and
`old_string` has to match the file **verbatim** — including indentation.

```
mcp__pocketpaw_pocket__get_pocket(pocket_id = <the site pocket's id>)
```

Read three things off the returned pocket:

- **`engine`** — must be `"react"`. If it is `"svelte"`, `"ripple"`, `"html"`
  or `"dynamic"`, you are on the wrong skill; see the bottom of this file.
- **`source`** — the `{path: contents}` map. Find the file that owns the
  section the user named (`src/components/Hero.tsx` for the hero) and read its
  current contents. This is where `old_string` comes from.
- **`keeps_client_bundle`** — needed only if your edit introduces the page's
  first client behaviour; see the interactivity section below.

If the user named a section but you cannot tell which file owns it, read
`src/App.tsx` — it is the composition root and imports every section in
order, so it is the site's table of contents.

## STEP 2 — Choose the edit shape

Three shapes. Pick by what the change actually is.

**`edits` — a targeted diff. The default, and strongly preferred.** A list of
`{old_string, new_string}` blocks applied to the file's current contents,
exactly like a careful search/replace. You send **only the change**, which is
the dominant saving on this track in both tokens and latency — a component is
often 200 lines and the edit is one of them.

```
edits = [{"old_string": "<h1 className=\"hero-title\">The finest coffee in Akron, roasted this morning</h1>",
          "new_string": "<h1 className=\"hero-title\">Roasted this morning, in Akron</h1>"}]
```

**`new_source` — the full replacement file.** Reserve it for a genuine rewrite,
where most of the file changes: "redesign the pricing section from scratch",
"rebuild the hero around the new photo". A rewrite expressed as fifteen
`edits` blocks is worse than one `new_source`; a one-line change expressed as
`new_source` wastes the whole file.

**`create=True` with `new_source` — a brand-new file.** This is how you add a
section. See "Adding a section is TWO calls" below.

**Exactly one of `edits` / `new_source` per call.** Passing both, or neither,
is rejected.

### Making `old_string` match exactly once

Each `old_string` must match the current file **exactly once**. Zero matches
and more than one match are both rejected as ambiguous, and the whole call
fails — nothing is half-applied.

- **Include surrounding context.** `className="card"` appears in every card;
  the enclosing line or the two lines around it usually do not.
- **Copy it, do not retype it.** Whitespace, indentation, quote style and JSX
  attribute order all count. Take the text out of the `source` you read in
  STEP 1.
- **On a rejection, widen — do not retry the same string.** "Matched 3 times"
  means add context until it is unique. "Matched 0 times" means the text is not
  what you thought it was; re-read the file rather than guessing again.
- Several blocks in one call are fine, and are the right way to do a change
  that touches three places in one file. Each block independently must match
  once.

## STEP 3 — Call `edit_react_component`

```
mcp__pocketpaw_sites_manager__edit_react_component(
  pocket_id      = "<the site pocket's id>",
  component_path = "src/components/Hero.tsx",
  edits          = [{"old_string": "...", "new_string": "..."}],   // OR:
  new_source     = "...",                                          // a full rewrite
  create         = False                                           // True only to ADD a file
)
```

- **`component_path` must already exist** when `create` is `False` (the
  default). A typo'd path is a **rejected call**, never a silently created
  file. If it rejects, fix the path — do **not** flip `create=True` to make the
  error go away: that writes a stray component nothing renders.
- **`create=True`** requires `new_source` and requires the path **not** to
  exist yet.
- Only `src/**` and `public/**` are writable.

## Adding a section is TWO calls

A new component that nothing imports is dead code — it renders nowhere, and
the page looks unchanged while the tool reports success. Adding a section is
always **two calls**, and stopping after the first is the failure mode:

1. **Write the component.**
   `component_path="src/components/Testimonials.tsx"`, `create=True`,
   `new_source=<the whole component>`.
2. **Render it.** `component_path="src/App.tsx"`, `edits=[...]` — one block
   adding the `import`, one block placing `<Testimonials />` at the right point
   in the funnel.

Removing a section is the mirror image: edit `src/App.tsx` to drop the import
and the element. Leaving the now-unused component file behind is harmless (the
build tree-shakes it), so prefer that over a delete you cannot undo.

## What you cannot change

**Generator-owned paths are refused** — `index.html`, `package.json`,
`vite.config.ts`, `paw-prerender.mjs`, and anything under `src/paw/**`. That
reservation is what guarantees the page cannot silently become a
blank-without-JavaScript SPA shell.

**So you cannot add a dependency.** The manifest is allowlisted and owned by
the generator; there is no call that installs a package. The project has
**react**, **react-dom**, **vite** and **@vitejs/plugin-react**, and that is
all — no router, no CSS framework, no state library, no animation library.

When a request needs a library that is not there, you have two honest answers:
implement it in **plain React and plain CSS** (which covers the large majority
of real asks — carousels, accordions, reveals, sticky nav, marquees), or tell
the user it is not available on this track. **Never claim you added a
package.** The site is also **one page**; there is no router, so "add an About
page" is a new section on the same page or a different conversation.

## ⚠️ THE PRERENDER AUTHORING RULE — an edit is the easiest place to break it

The page is **prerendered**: at build time `<App />` is rendered to HTML by
`react-dom/server`, **before any browser JavaScript runs**. `useEffect` does
**not** run then. `window` and `document` do **not** exist then.

**Every component must render its resting / final state in its RETURNED
MARKUP.** This applies to every edit, unchanged from create — and an edit is
where it usually breaks, because the file was correct until you touched it.

The classic edit that breaks it: "make that number count up."

```tsx
// The file today — prerenders the real number:
return <p className="stat">128 backers</p>;
```

```tsx
// ❌ WRONG edit — the shipped HTML now bakes "0 backers"
const [n, setN] = useState(0);
useEffect(() => { /* animate up to 128 */ }, []);   // never runs at prerender
return <p className="stat">{n} backers</p>;
```

```tsx
// ✅ RIGHT edit — the resting state stays in the markup
const TOTAL = 128;
const [n, setN] = useState(TOTAL);                  // RESTING state in markup
useEffect(() => {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  setN(0);                                          // restart on the client only
  /* animate up to TOTAL */
}, []);
return <p className="stat">{n} backers</p>;
```

The same trap, in the shapes edits actually take:

- **"Fade the sections in on scroll"** — the element and its text stay in the
  returned markup; the observer only adds a class. Never gate the *content*
  behind the observer.
- **"Make the FAQ an accordion"** — the open item is the `useState` *initial
  value*, so the first panel is in the prerendered HTML.
- **"Add a mobile menu"** — the closed nav is the resting markup; the toggle
  flips it.
- **Anything touching `window` / `document`** — inside `useEffect`, or behind
  `typeof window !== 'undefined'`. Never at module top level, never in a
  component body.

Before you send the edit, ask: *"with all JavaScript disabled, does this
section still look finished?"* If not, move the final state into the markup.

## When an edit introduces the page's FIRST client behaviour

`edit_react_component` has **no `interactive` argument** — it cannot change the
site's declared interactivity. So check `keeps_client_bundle` from STEP 1
before you add the first `onClick` / changing `useState` / `useEffect` to a
page that had none:

- **`null` / absent** (undeclared) — the deployment default ships the client
  bundle, so React hydrates and your new behaviour runs. Proceed.
- **`true`** — the site already declares that its JavaScript is load-bearing.
  Proceed.
- **`false`** — the site was **explicitly** built to ship no JavaScript at all.
  A handler you add there renders its resting markup and then does nothing,
  forever, with no error. Do not ship that silently. Either implement the
  behaviour **CSS-only** — `:hover`, `:focus-within`, `:target`,
  `<details>` / `<summary>`, keyframes, `@media (prefers-reduced-motion)`, all
  of which run at paint with no JavaScript and are usually the better page
  anyway — or tell the user the site is built bundle-free and that making it
  interactive needs the site rebuilt, which this tool cannot do.

CSS-only motion never needs the flag, whatever its value.

## The create-time rules that still bind

Every edit is held to the bar the page was built to. Briefly:

- **Real copy, always.** Never "TBD", never "Lorem ipsum", and never a
  fabricated testimonial, statistic, price, address or phone number. If the
  edit needs a fact you do not have, ask for it rather than inventing it.
- **Photography comes from `mcp__pocketpaw_stock__search_stock_images`** with a
  generic descriptive query. Embed the returned `url` directly, set `alt` from
  the returned `alt`, and render the returned `credit` — the providers' terms
  require it. On an empty result, use a gradient or solid treatment. **Never
  fabricate a photo URL**; a made-up `src` is a broken image on a live site.
  Set `width`/`height` (or `aspect-ratio`) so the page does not jump.
- **Every CTA is an anchor** — `href="#pricing"`, `tel:`, `mailto:` — not a
  bare `onClick` button.
- **The lead form stays a native `<form>`** with flat named fields (`name`,
  `email`, `phone`, `message`) and a `<button type="submit">`. There is no
  server route on this track, so an `onSubmit` that fetches drops the lead.
- **`className`, not `class`.** This is JSX.
- Keep the funnel's shape unless the user asked to change it, and keep the
  edit inside the page's existing design language — its tokens, type scale and
  spacing — rather than importing a new look for one section.

## STEP 4 — The edit is a DRAFT. It is not live.

**`edit_react_component` stages a draft. It does not publish and does not
rebuild.** Nothing the user's visitors see has changed yet.

⚠️ **And on the react track the draft shows its SOURCE, not the page.** The
in-app view has a client-side render lane for svelte and for html, and none for
react — a react draft falls through to the code viewer, so /sites shows your
`.tsx`, not the edited section. Rendering it needs the Vite build, which only a
publish runs. **Do not tell the user to go and look at the change.** They cannot
see it yet, and inviting them to look is how a working edit reads as broken.

So say what changed, be specific that seeing it needs a build, and offer to run
one — e.g. *"Shortened the hero headline. It's staged as a draft; React sites
only render once they're built, so say **publish** when you want me to build it
and put it live."* Then stop.

If the response carries a `preview_url`, it is a **preview** of the draft, not
the published site. Do not describe it as the live URL, and do not say
"published", "republished" or "live at".

**Publish only when the user asks** — "publish it", "make it live", "ship it":

```
mcp__pocketpaw_sites_manager__publish(pocket_id = "<the site pocket's id>")
```

⚠️ **"I've published it, here's your link" is the sentence that will be wrong.**
React is the only engine whose build runs **off-request**: `publish` queues the
build and returns immediately, and a worker deploys it later. On an edit you are
almost always on the second failure below, which is the dangerous one:

- **The site was never published before** — the response carries
  `deployed: false` and `url: ""`. There is no link. Do not show an empty url
  and do not invent one.
- **The site is already live** (the normal case for an edit) — `url` and
  `deployed` keep the **previous** deploy's values, so a rebuild never reports a
  working site as down. That url still serves the site **without your edit**.
  Handing it over as proof the change is live is wrong in the way that looks
  most convincing: the link works, it just shows the old page.

So report the build as **queued**, say the change goes live once it finishes,
and point the user at the site's build status under **/sites** — that status,
not the publish response, is what says whether the edit shipped. If they come
back with "is it up yet?", check the build status again rather than asserting
from the earlier response. Relay any `ok: false` — never claim a phantom
publish.

## Reading the response

- **`ok: true`** — the draft is updated. Give a **one-line** summary of what
  changed — and be concrete, because the user cannot see it until a build runs.
  If the payload carries a `message`,
  relay it rather than paraphrasing the publish state.
- **`ok: false`** — **nothing was applied.** Say what happened and fix it; do
  not report a successful edit. The usual causes, each with its own fix:
  an `old_string` that matched 0 or >1 times (widen it, or re-read the file);
  a `component_path` that does not exist with `create=False` (fix the path);
  `create=True` on a path that already exists (drop `create`); a
  generator-owned path (it is not editable — see above); both or neither of
  `edits` / `new_source`; a pocket that is not a react site (wrong skill).

Never fall back to another engine's tool, and never fall back to
`create_react_site`, after a failed edit.

## When NOT to use this skill

- **There is no site yet.** "Build me a React landing page" is a create —
  `pocketpaw-create-react-site`.
- **The pocket is `engine="svelte"`.** Use
  `mcp__pocketpaw_sites_manager__edit_svelte_component`, which takes the same
  `edits` / `new_source` shape against `src/lib/components/*.svelte`.
- **The pocket is `engine="ripple"` or `"dynamic"`.** Those carry a rippleSpec,
  not a source map — `mcp__pocketpaw_pocket_specialist__edit` is the right tool
  there, and it is what the refine preamble is written for.
- **The pocket is `engine="html"`.** There is no targeted component-edit tool
  on the html track today. `edit_react_component` rejects a non-react pocket,
  so do not aim it there; say plainly that this edit path does not cover an
  html site rather than reaching for a tool that will refuse.
- **The user wants an in-app dashboard pocket changed**, not a website. That is
  `pocketpaw-edit-pocket`.
- **The user wants the site deleted.** That is a workspace-level operation, not
  an edit.

## Quality bar — done right when

- Exactly one site pocket exists, and it is the one the user was looking at.
- The change went in as a **diff** unless it was a genuine rewrite.
- With **all JavaScript disabled**, the edited section still looks finished.
- A new section was **both** written and rendered from `src/App.tsx`.
- No claim was made about a package being added, or about the change being live.
- The user was told it is a draft, and was not invited to go and look at a
  rendered page that does not exist until a build runs.

## Related tools (via MCP)

- `mcp__pocketpaw_sites_manager__edit_react_component` — the edit itself (STEP 3)
- `mcp__pocketpaw_pocket__get_pocket` — read the current `source`, `engine` and
  `keeps_client_bundle` (STEP 1)
- `mcp__pocketpaw_sites_manager__publish` — deploy, on explicit request (STEP 4)
- `mcp__pocketpaw_sites_manager__edit_svelte_component` — the svelte-track sibling
- `mcp__pocketpaw_stock__search_stock_images` — real photography for a new section
- `mcp__pocketpaw_icons__search_icons` — feature icons
- `mcp__pocketpaw_palette__scale_from_color` / `extract_palette` — brand colour
- `mcp__pocketpaw_design_systems__get_design_system` — the page's token vocabulary
