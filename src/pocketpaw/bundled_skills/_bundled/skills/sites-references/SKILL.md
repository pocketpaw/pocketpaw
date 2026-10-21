---
name: sites-references
description: |
  Where a Paw Site's look comes from, with evidence. Two jobs in one file. The
  RESEARCH LOOP: search real shipped design systems with the Refero or Inspo
  tools, open the screenshots, lock one direction and trace every major choice
  to it. Invoke it when the brief leaves the look open: a new page, a redesign,
  a "make it premium / editorial / technical" ask. The TECHNIQUE SOURCES: named
  places to read before hand-writing a ground, texture, canvas shader, CSS
  animation, text effect, type pairing or colour ramp, filtered to what this
  surface can build (text you retype, nothing downloaded). Invoke that half when
  you are about to build one of those, or the brief names an effect. Skip both
  for a copy edit or when the user supplied a design to match. Research that
  returns nothing is a normal outcome, not a failure.
---

# Sites: references

This skill supplies evidence. It is not the design authority.

| Question | Owner |
| --- | --- |
| What should this look like, and on what evidence? | **this skill** |
| How is the page scoped, directed and kept from looking generated? | `pocketpaw-design-taste` (MODULE 0 governs scope) |
| How is each value built (scale, ramp, space, motion timing)? | `sites-craft` |
| What does the page say, and in what order? | `sites-conversion-structure` |
| Is what I built broken or generated-looking? | `sites-check` |

The locked reference sets the look; design-taste fills what it leaves open.
Never let a reference override the brief: no reference justifies a section the
brief did not ask for.

## Part 1: the research loop

### The tools

| Job | Refero (when configured) | Inspo (always there) |
| --- | --- | --- |
| Find a direction | `search_styles`, then `get_style` | `research_page_design` |
| Real screens for one section | `search_screens` | `research_page_design` with a section brief |
| **See it** | `view_reference` (`screen_id` or a style `preview_url`) | `view_reference_screenshot` (`slug`, `view`) |
| Real values from the locked reference | `get_style` | `get_reference_design_system` |

**The picture is the layer that matters.** A search result is a description,
and descriptions miss what decides whether a page looks designed: how the fold
is built, whether the product is shown, how light the page is, where colour is
actually spent. Open the screenshots.

Refero needs a paid plan, so an empty Refero result is normal; fall back to
Inspo. If both come back empty or error, design on your own judgement under
`pocketpaw-design-taste` and still state the direction. Never cite a reference
you did not receive, and never name a company as your source because it
sounded plausible.

### The loop

1. **Search several angles before choosing.** Two or three for the overall
   look, varying the axis rather than rewording: one aesthetic
   (`modern light waitlist, bold typography`), one about products like this
   one (`AI creator tool landing page with product visual`), one named product
   the brief evokes.
2. **Search per section** you are unsure about, by what is on the screen:
   `join the waitlist hero`, `pricing table two tiers`.
3. **Open the 3 to 5 strongest** and look for what they share.
4. **Lock one direction** (below), then pull real values from it.
5. **Look at your own draft** with `preview_site` once it verifies, at desktop
   and phone width, next to the references. Fix what is off and look again.

Depth follows risk. A small visual change earns a search or two and one opened
reference. A new page or a redesign earns the whole loop.

### Three rules that make research worth doing

**Do not clone one reference.** Lead with one, but a single site reproduced
whole is someone else's brand wearing your client's name.

**Do not average.** Blending two references produces the safe centroid, which
is the generic output research was meant to escape. Pick one dominant
direction, keep its sharp traits, borrow only narrow, named details from the
others.

**Preserve roles.** If a style marks a colour CTA-only, a face display-only or
a surface elevated-only, it keeps that role or it does not ship. A palette
flattened into five nice hex values smeared across the page is not the
reference you researched. Same for media: if the direction depends on
photography, honour it with real or generated assets, never a decorative
gradient box.

### Lock it before you build

Write two short artefacts in your reasoning, never on the page.

**Reference lock:** the primary direction and the traits that must survive.

```
Primary: a light product-led scheduling tool. Keep the warm off-white
canvas, near-black ink, the one accent spent only on the primary action,
and the large real product screen in the hero.
Borrowed: the plain two-column pricing table from a second reference.
```

**Decision ledger:** every major choice traced to something.

| Decision | Source | Role to preserve | Why |
| --- | --- | --- | --- |
| Off-white canvas, near-black ink | primary style | surface, not accent | brief asks for "calm and credible" |
| One accent, CTA only | primary style | CTA-only | keeps the one page action loud |
| Real product screen in the hero | primary style | media role | the product is the proof here |

**A major choice with no source is not a design decision yet.** Research
further, tie it to something the user said, or drop it.

### Before you hand the page over

- Did I open the references, not just read about them?
- Did I keep one direction's sharp traits rather than averaging several?
- Did every colour, face and surface keep the role its source gave it?
- Does every major choice trace to a reference, the brief or a craft rule?
- Did I stay inside the brief, with no section a reference talked me into?
- Does my draft in `preview_site` hold up next to the references?

A no anywhere means research or cut, not ship. Tell the user briefly what you
locked, what you borrowed and why it fits. Do not paste the ledger.

## Part 2: technique sources

### What this surface can take from a source

1. **Packages live in a framework template, never in `static`.** On a
   framework template, add an npm package by editing `package.json` with the
   file tools (`pocketpaw-site` has the loop). There is no shell, so a CLI that
   generates files into the project (component registries, copy-in kits)
   cannot run.
2. **Text files only.** The tools are `list_site_files`, `read_site_files`,
   `write_site_files`, `patch_site_file`, the build and verify loop,
   `list_site_assets`, stock search and media generation. There is no download
   tool, so a WOFF2, an SVG pack or a texture PNG cannot be brought in.
3. **What remains is technique.** A source is useful when its value is
   something you retype: a CSS rule, an inline `<svg>`, a data URI, a fragment
   shader, an easing curve, a font URL.

`WebFetch` works on these pages. Fetch one when you are about to build the
thing it covers, never speculatively.

### Grounds, patterns and texture

Decorative grounds are off by default (design-taste 2.B). Use these when the
brief or the family asks for one, with the reason written down.

| Source | Licence | What you retype |
| --- | --- | --- |
| Pattern Craft (`patterncraft.fun`) | MIT | Pure-CSS grids, dots, rays, mesh. Retune colours to your tokens. |
| Hero Patterns (`heropatterns.com`) | CC-BY | Tiling SVG as a data URI. Set the fill to your own ink. |
| Pattern Monster (`pattern.monster`) | free | SVG pattern generator. Take the emitted markup. |
| fffuel (`fffuel.co`) | free tools | Grain, noise, soft gradients, blobs as SVG markup. |
| Haikei (`haikei.app`) | free tools | Waves, blobs, stacked shapes as SVG. |

**Licence discipline.** CC-BY means the author is credited somewhere real or
it does not ship. A generator's output is yours. Never paste something whose
terms you did not read into a page a business publishes under its own name.

### Canvas shaders

For a hand-written WebGL canvas (design-taste 2.C):

| Source | Licence | Notes |
| --- | --- | --- |
| Radiant Shaders (`radiant-shaders.com/gallery/all`) | MIT | Self-contained fragment shaders you can ship. |
| Codrops (`tympanus.net/codrops`) | per article | Technique writing for canvas, scroll and transitions. Write your own implementation. |
| Grainient (`grainient.supply`) | commercial tool | Decide the grainy-gradient look, then reproduce it. Never hotlink its output. |

The canvas sits over a finished CSS fallback, the buffer is capped at 2x DPR,
and the loop stops off-screen. A shader you copied without understanding is
one you cannot cap.

### Motion and effects

| Source | Licence | What you retype |
| --- | --- | --- |
| Animista (`animista.net`) | BSD | CSS keyframes. Slow them down: the defaults are demo speed. |
| Colorion Text Effects (`text-effects.colorion.co`) | MIT | CSS-only text treatments, for when the brief names one. |
| Theme Toggle Effect (`theme-toggle.rdsx.dev`) | free | View-Transitions light/dark switching. |

Named techniques kept here for when a brief asks: the double-bezel enclosure
(design-taste 3.C.A, soft-premium only), marquees, grain over a flat ground.
None is a default.

### Type

Fonts load from a URL in CSS: Google Fonts (`fonts.google.com`), per
design-taste 2.F. **Fontpair** (`fontpair.co`) and **Free Faces**
(`freefaces.gallery`) are for choosing; confirm both faces are on Google Fonts
before committing. The independent OFL foundries (Velvetyne, Open Foundry,
Uncut, Collletttivo, The League of Moveable Type) show what a display face can
look like, and are a real option only when the owner supplies the file through
their own assets. Never point `@font-face` at a foundry's server.

### Colour

Start with the palette tools, which return values:
`mcp__pocketpaw_palette__scale_from_color` builds a scale from one brand hex,
`mcp__pocketpaw_palette__extract_palette` reads one off a logo or image. Then:

- **OKLCH picker** (`oklch.com`) when a ramp needs perceptually even steps.
- **Colour Contrast Checker** (`colourcontrast.cc`) to confirm a pair against
  the 4.5:1 body and 3:1 large-text floors. A screenshot cannot measure a
  ratio, so contrast is the number worth confirming.

### The failure this half prevents

A catalogue of design resources read as a menu is how a page gets padded with
effects nobody asked for. MODULE 0 still governs: come here once you already
know you are building a ground, a shader, an animation or a type system, and
want that one thing to be good.
