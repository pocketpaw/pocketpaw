<!-- New file 2026-09-08: provenance for sites-craft, the authoring half of the
     jakubkrehel craft rules. Sibling of sites-interface-review (the review half). -->
# Vendored: sites-craft

- **Source:** https://github.com/jakubkrehel/skills (MIT, Jakub Krehel) — the
  `better-typography`, `better-colors`, `better-layout` and `better-ui` skills
- **Vendored:** 2026-09-08 from commit `267330e1adfc`
- **License:** MIT. Attribution retained; the body is a rewrite, not a copy.

## Second source (2026-09-15): Refero craft-details

- **Source:** https://github.com/referodesign/refero_skill (MIT, Refero) —
  `skills/refero-design/references/craft-details.md`
- **Vendored:** 2026-09-15 from commit `a9b54a3e62a6` (upstream v1.0.2)
- **License:** MIT. Attribution retained; the body is a rewrite, not a copy.

Second harvest from the Refero craft references, after `copywriting.md` landed in
`sites-conversion-structure` section 6. craft-details measured as the highest
net-new ratio of the seven (~55%), though roughly a third of that half is
React/JSX-shaped (`virtua`, `nuqs`, `ResizeObserver` + `useLayoutEffect`,
`next/image`) and does not port to a surface with no package manager.

**Where each piece landed, and why the placement is the whole decision.**
`_craft_system("floor")` slices this file from the `## 5. The floor` HEADING to
EOF, so anything added at or below that heading rides EVERY refine turn — and the
2026-09-08 diet exists because refine was paying 2,868 tokens to shorten a
headline. So the harvest was split by the floor's own test: *does a copy edit
break this?*

- **Into the floor (rides every refine turn, +12%):** `:focus-visible` rather than
  `:focus`, with the reason (`:focus` fires on mouse click too, which is why rings
  get removed) and the two-ring `box-shadow` replacement. This one sharpens a
  bullet that was already there rather than adding a concern, and any refine that
  adds a control can break it.
- **Into `## The page shell` (create-only, NEW section placed BEFORE the floor):**
  the viewport-meta zoom rule, `[id] { scroll-margin-top }`, `touch-action:
  manipulation`, `<link rel="preconnect">`. All four are written once at create
  and never touched again — a copy edit cannot break them, so charging every
  refine turn for them would have been the diet running backwards. They were in
  the floor in a first pass and measured at +31%; moving them cut it to +12%.
- **Into `## Forms and input` (create-only, NEW):** the `autocomplete` table,
  `type`/`inputmode`, `spellcheck="false"`, never-block-paste, the label-plus-
  control single hit target, and the `aria-invalid` / `aria-describedby` /
  `role="alert"` wiring with focus moved to the first failing field. Forms are
  conditional on the brief, so they are create-only by the same test.
- **Into `## 4. Surface`:** `fetchpriority="high"` on the LCP image with no
  `loading="lazy"`, lazy + `decoding="async"` below the fold, explicit dimensions.

**Not ported.** Everything npm- or framework-shaped (`virtua`, `nuqs`,
`next/image` `priority`, uncontrolled-input patterns); `content-visibility` list
virtualization and `Intl.*` formatting, both marginal on a static marketing page;
and `<link rel="preload">` for a local WOFF2, which cannot exist here —
`sites-design-sources` states flatly that a font file cannot be brought into the
project.

**Third pass, same day - motion, icons, colour and typography.** The remaining
four references were harvested into the create-only region by the same test, all
of them ABOVE `## 5. The floor`, so the refine slice is untouched (measured +0%).

- **`## Motion` (new section):** the five named easing curves with values,
  duration bands plus the 120/200/320 token triple, `transform-origin` per
  component, the inline-SVG `transform-box: fill-box` gotcha, and reduced-motion
  as a token override rather than a hunt through rules. Framed as plain CSS on
  prerendered markup, because upstream's examples assume a JS framework toggling
  classes and this surface guarantees no such thing. NOT ported: springs, Framer
  Motion, GSAP, Rive and Lottie (npm, and the last two additionally need a file
  ingested); and upstream's enter=ease-out / exit=ease-in split, which
  contradicts `sites-interface-review`'s "exits are softer than enters, ease-out
  both ways".
- **Into `## 4. Surface`:** the optical-correction numbers (play triangle
  `0.5-1px` right, chevrons toward the point, the circle test), visual mass vs
  stroke width, and the icon-to-text size pairing. These sharpen two bullets that
  were already there.
- **Into `## 2. Colour`:** `color-scheme` + `theme-color` and the native
  `<select>` dark fix, the most valuable block in upstream's colour file and
  entirely absent here; plus the 60-30-10 area split, two colours per component,
  semantic colours as four-token sets, and keeping semantic clear of the brand
  hue. NOT ported: upstream's `rgba(0,0,0,0.45)` tertiary text, a rung
  `sites-restraint` documents as failing 4.5:1 and requiring a lift to ~56%; and
  its light-mode-by-default rule, which would make three of our six aesthetic
  families unreachable.
- **Into `## 1. Type`:** the `line-height x 0.5` rhythm ladder, the count caps
  and squint audit, `clamp()` fluid type with the mobile-to-desktop change table,
  the four preconditions for tightening tracking, the overflow recipes including
  the flex-child `min-width: 0` truncation trap, and the real punctuation set.
  NOT ported: upstream's Inter / Geist / Plus Jakarta recommendations, which
  `pocketpaw-design-taste` 2.F bans BY NAME as the loudest AI tell; its 1.2
  default ratio, below our 1.25-1.333 marketing band; and its 1.7 body leading,
  11px minimum size and `0.06-0.10em` caps tracking, all outside our ranges.

**Cost.** This file is embedded WHOLE on create, so the create preamble grew 46%
(4,080 -> 5,956 approx tokens). The refine floor did not move. Whether create is
worth that is a budget call rather than a craft one; the conditional part is
`## Forms and input`, which a brochure page with no form still pays for and which
could move to a named skill if the number matters more than the guarantee.

**Conflicts resolved in OUR favour**, because our values were measured against
this surface: upstream's `scale(0.98)` on press (we specify `0.96` and say above
`0.98` is invisible); upstream's 32x32px desktop icon hit area (ours is 44x44
everywhere); and upstream's "Title Case for buttons", which contradicts
`sites-interface-review`'s verb-first `Save changes` and our one-capitalization-
policy rule. None of the three was carried.

## Why this exists as well as `sites-interface-review`

They are the same knowledge pointed at two different moments, and shipping only
one of them left a real gap.

`sites-interface-review` is **corrective**: read-only, ranked findings, invoked
when someone asks for a review. That framing means the rules only reach the agent
*after* the page exists. On a create turn — the common case on /sites — nothing
was teaching the agent how to build a type scale or a colour ramp in the first
place.

`pocketpaw-design-taste` does not close that gap either. Measured on this commit,
it contains none of: ramp construction, perceived lightness, semantic-vs-primitive
token tiers, the concentric-radius formula, `text-wrap`, line-height by role,
optical alignment, `oklab`, or hit-area minimums. What it *does* carry is taste —
which font to reach for, which palettes are banned, which layout compositions to
rotate. Those are choices; this file is the method for building whatever was
chosen.

So: design-taste picks the ingredient, `sites-craft` sets the method,
`sites-interface-review` checks the result.

## What was ported

The **generative** half of four upstream skills, rephrased as do-this rather than
check-this, with upstream's exact values kept because upstream is explicit that
they are values and not ranges:

- **Type** — modular scale with semantic names, heading descent, line-height by
  role, letter-spacing by size, the 60-75ch measure, weight floors, `text-wrap`
  balance/pretty, `tabular-nums`, loading real weights, properties over raw font
  tags, 16px mobile inputs.
- **Colour** — ramps not colours, every step has a job, the four properties of a
  well-formed ramp (perceived lightness, constant hue, vividness peaking
  mid-ramp, denser at the light end), the primitive/semantic token seam, the 15°
  one-colour-one-meaning rule, fill one action per view, measuring the rendered
  pair, fixing contrast with lightness not hue, `oklab` as the gradient default.
- **Space** — the 2x grouping gap, shared alignment edges, 12px/24px control
  spacing, content-bleeds/controls-float, growth and clipping.
- **Surface** — concentric radius, optical alignment, shadows-vs-borders, the
  image outline at `oklch(0 0 0 / 0.1)` and the warning against tinted neutrals,
  `scale(0.96)` on press, icon stroke matched to text weight, naming transition
  properties, `currentColor` icon states.
- **The floor** — 44x44px hit areas, visible focus, no colour-only meaning,
  reduced motion, real elements, alt by purpose, 200% zoom.

## Deliberate overlap with `sites-interface-review`

Some rules appear in both files. That is a considered duplication rather than an
oversight: the alternative — making the review skill reference this one for its
criteria — adds an invocation hop on a path where a miss silently degrades the
review into an opinion.

**The original wording here said "both are loaded ON DEMAND and rarely in the same
turn, so the cost is paid only when one is actually used". That stopped being true
the same day it was written**, when this file was embedded in the /sites preamble.
It is now unconditional on create, so the overlap with `sites-interface-review` is
paid on every create turn where a review is also requested. Measured: the two files
share the 62ch measure cap, `prefers-reduced-motion`, and tabular figures. Three
rules, small, and worth knowing before someone adds a fourth.

If the two ever disagree on a value, **this file is the one to correct**: the
review skill was written first and compressed harder.

## What was NOT ported

- Everything already covered by `pocketpaw-design-taste` — font *choice*, palette
  *choice*, layout composition, the anti-slop copy rules.
- The upstream review protocol (scope resolution, coverage table, findings
  format), which lives in `sites-interface-review`.
- `break`, `variant` and `explain-interface`, which need a filesystem and a
  browser and cannot run on /sites.
- The 35 reference subfiles. `_SITES_BUILTIN_DENY` strips `Read`, `Glob` and
  `Bash` on every /sites mode, so a subfile is unreachable there; the rules they
  carried are inlined or dropped.

**For Claude Code use, prefer the upstream plugin over this file.** The full suite
is installed in this workspace as the `interfaces` marketplace plugin
(`/plugin install interfaces@interfaces`), which has all 11 skills and all 35
reference files at full depth and updates with upstream. This bundled copy exists
for the cloud sites agent, which has no filesystem and cannot reach a plugin.

## How it ships

Not as an on-demand skill on /sites, which is where it matters most.

- **create** — EMBEDDED whole in the preamble (`_craft_system("full")`), because its
  trigger is every section of every site and that is not something a probabilistic
  `Skill` call delivers. 2,868 tokens.
- **refine** — only §5 (the floor) and the symptom index are embedded
  (`_craft_system("floor")`, 448 tokens). The full method is NAMED in
  `<design-skills>` with the trigger "when an edit adds or restructures a section".
  Measured 2026-09-08: shipping it whole made it 65% of a refine turn, on a surface
  where most refines are a copy change. See SD-2 in
  `docs/design/drafts/2026-09-08-sites-system-prompt-diet.md` (paw-workspace).
- **everywhere else** — an ordinary bundled skill, loaded on demand.

The refine slice is keyed on the `## 5. The floor` HEADING, so **re-ordering or
renaming that heading changes what a refine agent receives.** A mutation covers it.

## Not verified

No site has been authored under it. The values are carried from upstream and have
not been independently re-derived.

## To refresh

Re-fetch upstream `skills/better-{typography,colors,layout,ui}/SKILL.md`. Check
the numeric values against the ones inlined here — upstream states they are exact,
so drift matters. Re-check the design-taste coverage claim above before assuming
this file is still additive.
