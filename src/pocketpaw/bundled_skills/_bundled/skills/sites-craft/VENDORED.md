<!-- New file 2026-09-08: provenance for sites-craft, the authoring half of the
     jakubkrehel craft rules. Sibling of sites-interface-review (the review half). -->
# Vendored: sites-craft

- **Source:** https://github.com/jakubkrehel/skills (MIT, Jakub Krehel) — the
  `better-typography`, `better-colors`, `better-layout` and `better-ui` skills
- **Vendored:** 2026-09-08 from commit `267330e1adfc`
- **License:** MIT. Attribution retained; the body is a rewrite, not a copy.

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
