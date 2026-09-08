<!-- New file 2026-09-08: provenance + refresh recipe for the adapted sites-conversion-structure skill. -->
# Vendored: sites-conversion-structure

- **Source:** https://github.com/elayadesign/ai-design-skills (MIT, Elaya) —
  `skills/landing-page-design/SKILL.md`
- **Vendored:** 2026-09-08 from commit `1c1e97cb9878`
- **License:** MIT. Attribution retained here; the skill body is a rewrite rather
  than a copy.

## What was ported

**Part A only** — the strategy layer: the one-offer/one-audience/one-action frame,
the intake batch, the four page archetypes (A–D), the argument order, the
conversion rules, the headline/CTA/benefit formulas, the section-by-section build
order, the index/noindex + FAQ-schema call, and the pitfalls list.

## What was deliberately NOT ported, and why

**All of Part B (its visual system).** `pocketpaw-design-taste` already governs the
visual layer on every /sites engine, and it is embedded into the create preamble by
`ee/pocketpaw_ee/cloud/surface/handlers/sites.py::_design_taste_system`. Porting
Part B would have shipped a second, conflicting design system in the same turn.
The two genuinely disagree:

| Part B says | design-taste says |
| --- | --- |
| Use Geist / Manrope / Poppins; never Inter | Bans Inter for premium, but rotates a different named list (Satoshi, General Sans, Clash Display, PP Neue Montreal…) |
| Never use gradients in backgrounds | MODULE 2.B mandates a deliberate background architecture, gradients included |
| Hero heading gets a white→grey text gradient | MODULE 5 bans gradient-filled headline text as a default |
| Snap every size to the Tailwind type scale | Sites' html/svelte tracks author scoped `<style>` blocks, not Tailwind utilities |
| Dark backgrounds from a fixed 6-hex list | MODULE 2.G requires an off-black tuned to the palette temperature |

Its **B11 "mandatory tagline reveal section"** is also dropped: it mandates a
per-word scroll-driven reveal, and Paw Sites prerender with the client bundle
pruned unless `keepsClientBundle` is set, so the section would ship as muted
25%-opacity text that never activates.

Its **B8 content-realism rules** were dropped as duplicates — design-taste MODULE 4
already carries organic metrics, no "John Doe", no "Acme", and no filler verbs.

## Local edits

- Reframed throughout for Paw Sites and given an explicit ownership table so the
  skill composes with design-taste instead of competing with it.
- Added the "research the real business first" step (WebSearch/WebFetch), which the
  upstream does not have and which is what keeps generated copy specific.
- Added the archetype-C note (a waitlist page does not need twelve sections).
- Tightened the SEO section to forbid inventing values into structured data.

## Why it ships bundled

Nothing in the sites stack decided *what the page argues*. design-taste is
engine-agnostic visual direction; the create skills are per-track authoring. The
strategy layer was the gap.

## Not verified

No site has been built end-to-end under this skill yet. The archetype table and the
intake batch in particular have not been exercised against a real brief.

## To refresh

Re-fetch upstream `skills/landing-page-design/SKILL.md`, re-read Part A only,
re-check the Part B divergence table above against the current
`pocketpaw-design-taste`, and note the new commit here.
