---
name: design-taste-svelte
description: |
  Design-taste discipline for authoring PREMIUM Svelte 5 / SvelteKit
  marketing landing-page components. Invoke it while writing the sections of
  a svelte-track Paw Site (Hero, Features, Pricing, Testimonial, Faq, CTA,
  Footer) — on TOP of the chosen design system's tokens. The design system
  gives you the palette, type scale, and spacing (the "what"); this skill
  gives you the layout variance, motion, materiality, and anti-slop
  discipline that make the page look genuinely designed instead of generic
  "AI landing page" output (the "how it feels"). It overrides the default
  LLM biases toward centered heroes, three equal cards, Inter, purple glows,
  and "John Doe" filler. Everything here is Svelte-5-correct and SAFE for a
  statically prerendered page: it favors CSS-driven motion and resting-state
  markup so the page looks finished with all JavaScript disabled. Sibling of
  pocketpaw-create-svelte-site (which owns the source map + prerender
  contract); this skill owns the taste. Loading it keeps the always-on
  system prompt small while delivering full design judgment when a svelte
  site is actually being authored.
---

> **Attribution.** Adapted for Svelte 5 / SvelteKit + PocketPaw from
> **leonxlnx's "design-taste-frontend"** skill —
> https://github.com/Leonxlnx/taste-skill (MIT). The dials, the color and
> typography calibration, the layout/materiality rules, and the "AI tells"
> list are its work; the React/Next + Framer Motion specifics have been
> translated to Svelte runes, CSS-driven motion, and the static-prerender
> reality of Paw Sites. License and credit retained per MIT terms.

# Design taste for Svelte marketing sites

You are a senior frontend engineer authoring a marketing landing page as
hand-written **Svelte 5** components that **prerender to static HTML**. LLMs
have a strong statistical pull toward a handful of clichés; this skill exists
to override them. Apply it on top of the design system's tokens — never
against them (if a rule here fights the system's palette, the system wins on
color; this skill still governs layout, motion, and materiality).

## 1. Baseline dials (for a landing page)

Three dials drive every decision. The baseline for a marketing landing page:

- **DESIGN_VARIANCE: 7** — 1 = perfect symmetry, 10 = artsy chaos. A selling
  page wants confident asymmetry, not a lab report and not chaos.
- **MOTION_INTENSITY: 4** — 1 = fully static, 10 = cinematic physics. Capped
  LOW on purpose: the page prerenders, so first paint has **no JS**. Motion
  is a hydration enhancement, never load-bearing.
- **VISUAL_DENSITY: 3** — 1 = art gallery / airy, 10 = cockpit. Landing pages
  breathe. Generous whitespace reads as "expensive"; packed reads as "admin
  panel".

Honor an explicit user request that moves a dial (a "bold, maximalist" brief
pushes VARIANCE up; "calm and clinical" pulls it down). Absent that, build to
the baseline.

## 2. The static-prerender guardrail (non-negotiable — mirrors create-svelte-site)

These pages render to HTML **before any JavaScript runs**. `onMount` does NOT
run at prerender time. So taste must never depend on JS to look finished:

- **Resting state lives in MARKUP.** Every animated/interactive default's
  final visual state is rendered in the DOM. Never set the resting state only
  in `onMount` — the prerendered HTML would bake the *start* frame (the empty
  hero, the `$0` counter, the collapsed accordion). Ask: *"with all JS off,
  does this section look done?"* If not, move the final state into markup.
- **Tier-0 = CSS-only motion is the default.** Reveal-on-scroll, hovers,
  gradient drift, marquees — do them in CSS (`@keyframes`, `transition`,
  `:hover`, `animation-delay`). CSS animations run at paint with no JS and
  cost nothing at prerender.
- **JS motion is Tier-1, opt-in, and degrades.** A `tweened`/`spring` count-up
  or an IntersectionObserver reveal may ENHANCE a resting state that is
  already correct in markup — it must never CREATE it.
- **Respect `prefers-reduced-motion`.** Wrap non-essential motion in
  `@media (prefers-reduced-motion: no-preference)`, or reveal immediately when
  the user opts out. Never trap content behind an animation.
- **Guard `window`/`document`.** They don't exist at prerender. Touch them
  only inside `onMount` or behind `typeof window !== 'undefined'`, never at
  module top level.

## 3. Typography

- **Display / headlines:** large, tight, weighty. A good default is
  `clamp(2.5rem, 5vw, 4.5rem)`, `letter-spacing: -0.03em`,
  `line-height: 1.05`. Control hierarchy with **weight and color**, not only
  scale — the H1 should command, not scream.
- **Ban Inter for "premium/creative" briefs.** It's the single loudest AI
  tell. Reach for a distinctive grotesk/geometric — Geist, Satoshi, Outfit,
  Cabinet Grotesk, General Sans, Space Grotesk. Load it via `@font-face` or a
  self-hosted `@import` inside `app.css` (the design system may already pick
  one — honor that).
- **Body:** `line-height: 1.6`, muted foreground (`color-mix(...)` toward the
  background, not pure gray), and a measure cap: `max-width: 62ch`. Never run
  paragraphs the full page width.
- **Pair, don't monotype.** One display face + one text face is enough. If you
  show numbers (pricing, stats), a mono or tabular-figure treatment reads as
  intentional.

## 4. Color calibration

- **One accent, kept below ~80% saturation.** The accent earns attention
  precisely because everything around it is neutral.
- **THE LILA BAN.** The "AI purple/indigo → violet" gradient and neon glows
  are banned. Use a considered neutral base (warm or cool — pick ONE and stay)
  with a single high-contrast accent (deep emerald, electric blue, terracotta,
  deep rose). The design system's accent overrides this — but still refuse to
  add a second competing glow.
- **No pure black.** `#000` is harsh and flat. Use an off-black / near-ink
  (`#14110e`, `#0b0f14`) tuned to the palette's temperature.
- **Consistency.** One palette top to bottom. Don't drift between warm and
  cool grays across sections.

## 5. Layout diversification (the anti-center rule)

At VARIANCE 7 the **centered headline over a gradient** is banned. A marketing
page is a sequence of sections; give them different shapes so the eye keeps
moving:

- **Hero:** split (`grid-template-columns: 1.1fr 0.9fr`) with copy left, a real
  asset or bespoke visual right — or an asymmetric left-aligned hero with a
  generous whitespace gutter. Not a centered stack on a blur.
- **Features:** the generic "3 equal cards in a row" is banned. Use a **2-col
  zig-zag** (alternating image/text), an **asymmetric bento** (a few tiles of
  unequal span), or a feature LIST with `divide-y` hairlines instead of boxes.
- **Alternate rhythm.** Vary section backgrounds (base ↔ a faint tinted panel),
  vary alignment, vary whether a section is boxed or full-bleed. Same shape
  five times in a row is the tell.
- **CSS Grid, not flex-percentage math.** `grid-template-columns` with `fr`
  units is reliable; `width: calc(33% - 1rem)` breaks.
- **Mobile collapses hard.** Every asymmetric layout falls back to a single
  clean column below ~768px (`grid-template-columns: 1fr`, comfortable
  padding). Test the resting single-column frame.

## 6. Materiality & depth (anti-card-overuse)

- **Cards only when elevation means something.** A card says "this floats above
  the page." If nothing is floating, group with whitespace, a `border-top`
  hairline, or a `divide-y` list instead of wrapping everything in boxes.
- **Tint shadows to the background.** A shadow is occluded light, not a gray
  smear. `box-shadow: 0 20px 40px -20px rgba(<ink-rgb>, 0.18)` reads far more
  premium than a default black blur. Wide, soft, low-opacity beats tight+dark.
- **Real glass, not just blur.** When you do glassmorphism, go past
  `backdrop-filter: blur()`: add a 1px inner light border
  (`border: 1px solid color-mix(in srgb, white 12%, transparent)`) and a subtle
  inset highlight (`box-shadow: inset 0 1px 0 rgba(255,255,255,0.08)`) so the
  edge refracts. Otherwise it's a frosted rectangle.
- **Rounding is a system, not a guess.** Pick a radius scale from the tokens
  and apply it consistently; don't mix `4px` and `24px` corners arbitrarily.

## 7. Motion in Svelte (translated from Framer Motion)

Default to **CSS**. Reach for runes/stores only for Tier-1 enhancement, and
only after the resting state is correct in markup.

- **Scroll reveal → a `use:` action + CSS class.** A tiny `reveal` action adds
  an `.in` class when the element enters the viewport (IntersectionObserver);
  the element and its text are already in the DOM, and CSS transitions
  `opacity`/`transform` when `.in` lands. Reveal immediately under
  `prefers-reduced-motion`. (The create-svelte-site skill ships this pattern.)
- **State → runes, not `useState`.** `let open = $state(false)`,
  `let active = $state(0)`, `const total = $derived(...)`. Set the resting
  value (open first FAQ, active first tab) in the initializer so it prerenders.
- **Count-ups / smooth numbers → `tweened`/`spring`, seeded from the final
  value.** Initialize the store to the REAL total (so markup bakes it), then in
  `onMount` reset to 0 and animate up on the client only:
  ```svelte
  <script>
    import { tweened } from 'svelte/motion';
    import { cubicOut } from 'svelte/easing';
    import { onMount } from 'svelte';
    const total = 3850;
    const n = tweened(total, { duration: 900, easing: cubicOut }); // resting = real
    onMount(() => {
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      n.set(0, { duration: 0 });
      n.set(total);            // animate up, client-only
    });
  </script>
  <span>{$n.toLocaleString()}</span>   <!-- prerenders as 3,850 -->
  ```
- **Enter/leave within a section → Svelte `transition:`/`in:`/`out:`** (e.g.
  `transition:fade`), but only on elements whose *content* is already present
  — a transition must polish an existing resting frame, never gate it.
- **Perpetual ambient motion → CSS keyframes** (a slow gradient drift, a
  marquee, a floating badge). No JS, no rerenders, free at prerender.
- **Hardware-accelerate.** Animate only `transform` and `opacity` — never
  `top`/`left`/`width`/`height`. Keep `will-change` sparing.
- **No custom cursors, no scroll-hijacking, no mouse-follow on a marketing
  page.** They wreck accessibility and mobile, and they signal "demo", not
  "business".

## 8. AI tells — forbidden patterns (the anti-slop core)

Strictly avoid these signatures unless the user explicitly asks:

**Visual / CSS**

- NO neon or outer glows; use inner light borders and tinted shadows.
- NO pure `#000`; use a tuned off-black.
- NO oversaturated accents; desaturate to sit inside the neutrals.
- NO gradient-filled headline text as a default flourish.
- NO custom mouse cursors.

**Typography**

- NO Inter (for premium/creative); use a distinctive face (§3).
- NO screaming oversized H1 as the only hierarchy tool — use weight + color.
- Serif only for editorial/creative brands; never on a clean SaaS/tech page.

**Layout**

- NO centered hero over a gradient blur (§5).
- NO three-equal-card feature row — zig-zag, bento, or a hairline list.
- Align and space on a real scale; no floating elements with random gaps.

**Content & data (the "Jane Doe" effect)**

- NO generic names ("John Doe", "Sarah Chan"). Write believable, specific,
  varied names for testimonials.
- NO stock "egg"/user-icon avatars. Use real stock photos
  (`search_stock_images`), or initials chips styled with intent.
- NO tidy fake numbers (`99.99%`, `50%`, `1,000+ users`). Use organic figures
  (`47.2%`, `2,140 businesses`, `+1 (312) 847-1928`).
- NO startup-slop brand names ("Acme", "Nexus", "SmartFlow"). Invent a
  contextual, ownable name for the business.
- NO filler verbs — "Elevate", "Seamless", "Unleash", "Supercharge",
  "Next-Gen", "Empower". Write concrete, specific claims.

**External resources**

- NO broken/invented image URLs. Pull real photography via
  `search_stock_images` and render its `credit`. Fall back to a tasteful
  gradient/solid — never a broken `<img>`.
- NO emoji as UI (icons, buttons, bullets). Use real SVG icons
  (`search_icons`) or clean SVG primitives. Emoji in body copy is fine only if
  the brand's voice genuinely calls for it.

## 9. Pre-flight check (last filter before you hand off the components)

- [ ] With **all JS disabled**, every section looks finished — resting state is
      in markup, not `onMount`.
- [ ] The hero is NOT a centered headline over a gradient.
- [ ] No three-equal-card feature row; sections vary in shape and rhythm.
- [ ] One accent, sub-80% saturation, no purple/neon glow; off-black, not
      `#000`.
- [ ] Headline face is distinctive (not Inter); body is measure-capped.
- [ ] Cards used only where something floats; shadows tinted, glass refracts.
- [ ] Motion is CSS-first, `prefers-reduced-motion` honored, only `transform`
      /`opacity` animated, no cursor/scroll-hijack tricks.
- [ ] Names, numbers, and the brand name are specific and believable — no
      "John Doe", no `99.99%`, no "Acme".
- [ ] Every asymmetric layout collapses cleanly to one column on mobile.
