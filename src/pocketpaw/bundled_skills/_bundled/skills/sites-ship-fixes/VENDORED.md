<!-- New file 2026-09-08: provenance + refresh recipe for the adapted sites-ship-fixes skill. -->
# Vendored: sites-ship-fixes

- **Source:** https://github.com/blackbrainpy/designs-by-kore (MIT, Designs by Kore /
  KORE INDUSTRIES) — `plugins/designs-by-kore/skills/designs-by-kore/SKILL.md`
- **Vendored:** 2026-09-08 from commit `5a82b1ee85ed`
- **License:** MIT. Attribution retained here; the skill body is a rewrite rather
  than a copy.

## What was ported

Upstream's **"Ship-it checklist — recurring first-draft fixes"**, which is the part
of that skill with no equivalent anywhere in the sites stack: concrete, measurable
build defects rather than taste.

- the hero must fit the fold, with the viewport sizes to test against;
- sticky/pinned sections bleeding into the next, and the ~40vh exit spacer;
- a transparent fixed nav letting headings bleed through it;
- screenshots presented as flat alternating rows, and the crop/`drop-shadow` details;
- the dead gutter in two-column media+copy blocks (`justify-items: end`);
- the mobile floor (`overflow-x: clip` not `hidden`, `minmax(0, 1fr)`,
  `overflow-wrap: anywhere`);
- reduced-motion, transform/opacity-only, and no scroll listeners;
- "verify with screenshots" — most of these are only caught by looking.

## What was deliberately NOT ported, and why

**All of its library stack.** Upstream is built around GSAP + ScrollTrigger +
SplitText, Lenis smooth scroll, Motion/Framer Motion, and Three.js / React Three
Fiber, and four of its six reference files are patterns for those libraries.

**None of that runs on a Paw Site.** Sites prerender, and unless the site sets
`keepsClientBundle` the bundle is pruned — `onMount`, `use:` actions,
IntersectionObserver, scroll listeners and WebGL never execute. This is stated in
`pocketpaw-design-taste` MODULE 3.E/3.F and it is the reason the port is CSS-only:
every fix in the skill body holds with JavaScript disabled. Porting the GSAP
patterns would have produced motion that looks correct in the source and renders as
a frozen start-frame on the live page.

`webgl-components` is already bundled separately for the cases that do keep a bundle.

Also dropped:
- **its stack-selection table** (Next.js / Vite / vanilla) — the engine is chosen by
  the /sites create flow, not by the design skill;
- **its toolkit and install commands** — nothing is installed on this surface;
- **its typography/colour/layout quality bar** — duplicates design-taste MODULES 2
  and 3;
- **its em-dash rule** — already design-taste MODULE 4, stated more strictly there;
- **its app-store-badge / JSON-LD section** — the schema half moved to
  `sites-conversion-structure`, which owns SEO; the store-badge half is
  app-landing-specific and did not generalise.

## Local edits

- Added the opening "rule that gates every fix here" making the prerender constraint
  explicit, so the CSS-only framing is a stated reason rather than an omission.
- Added `position: sticky` silently failing under an ancestor `overflow` — the most
  common cause of "sticky is not sticking", and the reason `overflow-x: clip` is
  specified over `hidden` elsewhere in the file.
- Added the closing honesty rule: if no preview is reachable, say the page was not
  previewed rather than implying it was checked.

## Why it ships bundled

design-taste says what a good page looks like. Nothing said what breaks on the first
draft. These are the revisions that actually come back.

## Not verified

The specific viewport sizes (1920x860, 1440x820, 1366x768) and the ~40vh exit spacer
are carried over from upstream and have not been re-derived against a real Paw Site
build.

## To refresh

Re-fetch upstream `plugins/designs-by-kore/skills/designs-by-kore/SKILL.md` and read
only the "Ship-it checklist" section. Before porting anything from its
`references/gsap-patterns.md`, `motion-patterns.md` or `webgl-patterns.md`, confirm
whether the target site keeps its client bundle — if it does not, those patterns are
inert by construction.
