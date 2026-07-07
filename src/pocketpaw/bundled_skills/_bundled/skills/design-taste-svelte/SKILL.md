---
name: design-taste-svelte
description: |
  Design-taste discipline for authoring PREMIUM Svelte 5 / SvelteKit
  marketing landing-page components. Invoke it while writing the sections of
  a svelte-track Paw Site (Hero, Features, Pricing, Testimonial, Faq, CTA,
  Footer). It runs the full anti-slop framework, not just guardrails: FIRST
  read the room and declare a one-line "Design Read", THEN pick an aesthetic
  DIRECTION (clean-tech / soft-premium / editorial-luxury / warm-minimalist /
  brutalist / dark-tech) that drives the palette, type, materiality, and
  motion, THEN author sections that honour it. This is what stops every site
  looking like the same clean house style. It overrides the default LLM
  biases toward centered heroes, three equal cards, Inter, purple glows,
  eyebrows-on-every-section, em-dashes, and "John Doe" filler. Everything is
  Svelte-5-correct and SAFE for a statically prerendered page: CSS-driven
  motion and resting-state markup so the page looks finished with all
  JavaScript disabled. Sibling of pocketpaw-create-svelte-site (which owns
  the source map + prerender contract); this skill owns the taste. Loading it
  keeps the always-on system prompt small while delivering full design
  judgment when a svelte site is actually being authored.
---

> **Attribution.** Adapted for Svelte 5 / SvelteKit + PocketPaw from
> **leonxlnx's "taste-skill" / "design-taste-frontend"** —
> https://github.com/Leonxlnx/taste-skill (MIT). The brief-inference protocol,
> the three dials, the design-direction families, the color/typography
> calibration, the layout/materiality rules, and the "AI tells" list are its
> work; the React/Next + Motion/GSAP specifics have been translated to Svelte
> runes, CSS-driven motion, and the static-prerender reality of Paw Sites.
> License and credit retained per MIT terms.
>
> **Revision (this version): ported the framework depth that the first cut
> dropped** — §0 Brief Inference (the "Design Read"), §2 Design Direction
> (aesthetic families), a more assertive dial baseline + inference tables, the
> em-dash ban, eyebrow restraint, section-layout-repetition ban, hero
> discipline, the premium-consumer palette ban, the copy self-audit, and a
> mechanical pre-flight. All translated to Svelte-static; nothing here assumes
> React, Tailwind utilities, `next/font`, or `motion/react`.

# Design taste for Svelte marketing sites

You are a senior frontend engineer authoring a marketing landing page as
hand-written **Svelte 5** components that **prerender to static HTML**. LLMs
have a strong statistical pull toward a handful of clichés and toward ONE
default aesthetic; this skill exists to override both. The order is fixed:

1. **Read the room** and declare a one-line Design Read (§0).
2. **Pick a direction** — an aesthetic family that sets palette, type,
   materiality, and motion (§2).
3. **Set the dials** from the read (§1).
4. **Author the sections** honouring the direction, on top of the design
   system's tokens in `app.css`.

Every rule below is **contextual** — none fires automatically. First read the
brief, then pull only what fits. If a rule here fights the chosen palette, the
palette wins on *color*; this skill still governs layout, motion, and
materiality.

---

## 0. BRIEF INFERENCE — read the room before you write a line

Most LLM site output is bad because the model jumps straight to a default
look. Do not. Before touching a component, infer what this specific business
actually wants.

### 0.A Read these signals
1. **Business kind** — SaaS / dev-tool, agency / studio, premium consumer or
   DTC, local service (dentist, bakery, gym), event, portfolio, editorial.
2. **Vibe words the user used** — "minimal", "calm", "Linear-style", "bold",
   "premium", "Apple-y", "playful", "serious B2B", "editorial", "brutalist",
   "warm", "luxury".
3. **Reference signals** — any URL, screenshot, or brand they named or said
   they want to compete with. If they linked something, that is the strongest
   signal in the room; match its family, don't override it.
4. **Audience** — a procurement buyer, a design-conscious consumer, a local
   walk-in customer, a recruiter. The audience picks the aesthetic, not your
   taste.
5. **Existing brand assets** — a named color, a logo, a font. Honour them.
6. **Quiet constraints** — trust-first / regulated / accessibility-critical
   audiences OVERRIDE aesthetic preference toward calm and legible.

### 0.B Declare a one-line Design Read (do this out loud, before authoring)
State it in one sentence, then build to it:

> **"Reading this as: a `<business kind>` for `<audience>`, `<vibe>` in feel,
> so I'm going `<direction>` with `<accent>` on `<neutral base>` and
> `<display face>`."**

Examples:
- *"Reading this as: a B2B SaaS for technical buyers, Linear-clean in feel, so
  I'm going clean-tech with electric-blue on cool graphite and Geist."*
- *"Reading this as: an artisan bakery for local walk-ins, warm and tactile,
  so I'm going editorial-luxury with brick-red on bone and a display serif."*
- *"Reading this as: a design studio's portfolio for hiring managers,
  confident and expressive, so I'm going soft-premium with off-black on silver
  and Clash Display."*

### 0.C If the brief is genuinely ambiguous, ask ONE question — never guess
Ask exactly one, only when the read truly forks. e.g. *"Should this feel
closer to Linear-clean or bolder and more expressive?"* If you can confidently
infer, do NOT ask — just declare the read and go.

### 0.D Anti-default discipline
Do not reflexively reach for: AI-purple/indigo gradients, a centered hero over
a dark mesh, three equal feature cards, glassmorphism on everything, an eyebrow
above every section, Inter + slate. These are the defaults. Reach past them
deliberately, based on the read.

---

## 1. The three dials

After the read, set three dials. Every layout, motion, and density decision
below is gated by them.

- **DESIGN_VARIANCE: 7** — 1 = perfect symmetry, 10 = artsy chaos. A selling
  page wants confident asymmetry.
- **MOTION_INTENSITY: 5** — 1 = fully static, 10 = cinematic. Held moderate on
  purpose: the page prerenders, so first paint has **no JS**. Motion is a
  hydration enhancement, never load-bearing (§3).
- **VISUAL_DENSITY: 3** — 1 = art gallery / airy, 10 = cockpit. Landing pages
  breathe; generous whitespace reads as "expensive".

### 1.A Dial inference (read → values)
| The read says… | VARIANCE | MOTION | DENSITY |
|---|---|---|---|
| minimal / clean / calm / editorial / Linear-style | 5-6 | 3-4 | 2-3 |
| premium consumer / Apple-y / luxury / brand | 7-8 | 5-6 | 3-4 |
| playful / bold / agency / experimental | 8-9 | 6-8 | 3-4 |
| landing / marketing site (default) | 7 | 5 | 3 |
| trust-first / regulated / accessibility-critical | 4 | 3 | 4 |

### 1.B What the dials mean
- **VARIANCE 1-3:** symmetric grid, equal padding, centered. **4-7:** offset
  overlaps, mixed aspect ratios, left-aligned headers over centered data.
  **8-10:** asymmetric fractional grids (`grid-template-columns: 2fr 1fr`),
  large deliberate empty zones. *Any asymmetry above collapses to one clean
  column below 768px.*
- **MOTION 1-3:** `:hover`/`:active` only. **4-7:** CSS transitions + reveal
  cascades on `transform`/`opacity`. **8-10:** scroll-driven reveals, ambient
  keyframes — still CSS-first, still degrading (§8).
- **DENSITY 1-3:** big section gaps (`padding: 6rem 0` to `9rem 0`).
  **4-7:** standard (`4rem`-`6rem`). **8-10:** tight, hairline dividers not
  cards, tabular/mono numbers.

Honour an explicit user request that moves a dial. Absent that, build to the
inferred values.

---

## 2. Design direction — pick an aesthetic FAMILY (the anti-same-y rule)

This is the layer that makes each site look *designed for this business*
rather than "clean AI landing page No. 47". After the read, commit to ONE
family below and let it drive `app.css` — the palette, the display/body faces,
the radius scale, the shadow character, and the motion feel. **One family per
site, top to bottom.** Do not blend two.

> Note: unlike the React taste-skill, Paw Sites don't `npx` a component
> library (Material/Carbon/shadcn) — the "system" here is the token set YOU
> author in `app.css`. So "pick a system" becomes "pick a family and express
> it in tokens." Everything is plain CSS variables + scoped `<style>`; there
> is no Tailwind in the skeleton.

### The families

**A. Clean-Tech (Linear / Vercel)** — SaaS, dev-tools, AI.
- Palette: cool graphite/zinc neutrals, off-black `#0b0f14` ground, ONE
  saturated accent (electric blue, emerald). No purple.
- Type: geometric grotesk (Geist, General Sans, Space Grotesk), tight display,
  mono for numbers/labels.
- Materiality: hairline borders, 1px inner-light edges, near-flat cards, small
  radius (8-12px). Restraint.
- Motion: crisp, short (150-300ms), reveal-on-scroll, no bounce.

**B. Soft-Premium (Awwwards / agency-tier)** — brand, premium consumer,
studios, portfolios. This is the "$150k agency build" register.
- Palette: silver-grey or deep OLED black grounds, extremely soft diffused
  ambient shadows, one refined accent.
- Type: large bold grotesk display (Clash Display, PP Neue Montreal, Cabinet
  Grotesk), heavy weight, tight tracking.
- Materiality: the **double-bezel** (§7.A) — nested enclosures like machined
  hardware — exaggerated squircle radii (`border-radius: 2rem`),
  button-in-button CTAs, macro-whitespace (`padding: 6rem 0` to `10rem 0`).
- Motion: heavy spring easing (`cubic-bezier(0.32, 0.72, 0, 1)`), staggered
  reveals, gentle fade-up with a touch of blur.

**C. Editorial-Luxury** — lifestyle, food, real estate, heritage craft,
publications.
- Palette: warm bone/cream ground, deep espresso or brick-red text/accent,
  optional 3% film-grain overlay for a paper feel. (Beware the beige+brass
  default — see §5's premium-consumer ban and rotate.)
- Type: a justified display **serif** (this is one of the few places serif is
  right — PP Editorial New, Reckless Neue, Tiempos, Playfair) paired with a
  clean sans body.
- Materiality: flat, editorial, asymmetric grid, generous margins, hairline
  rules not boxes.
- Motion: minimal, slow, tasteful — a quiet fade, no bounce.

**D. Warm-Minimalist (Notion / editorial-doc)** — productivity, content,
calm consumer.
- Palette: warm off-white `#faf9f6` / pure white ground, charcoal `#2f3437`
  text, muted pastel spot accents used only semantically (pale blue/green/
  yellow chips).
- Type: clean humanist/geometric sans (Switzer, Geist), optional editorial
  serif for the hero only; mono for meta.
- Materiality: crisp 1px `#eaeaea` borders, small radius (8-12px), shadows
  near-zero (opacity < 0.05). No pills on big containers. No gradients.
- Motion: subtle, functional, `transform: scale(0.98)` on press.

**E. Brutalist / Structural** — bold statements, dev-culture, events, drops.
- Palette: raw black-on-white (or one loud flat color), high contrast, no
  gradients, no soft shadows.
- Type: mono or condensed grotesk, oversized, tight, often uppercase.
- Materiality: hard borders (2-3px solid), sharp corners (radius 0), visible
  grid, offset/overlap.
- Motion: instant or snappy, deliberate — glitch/marquee at most once.

**F. Dark-Tech / Terminal** — security, infra, crypto, hacker-adjacent.
- Palette: deep near-black, one neon-ish accent used *sparingly* (no page-wide
  glow), mono everywhere.
- Type: mono display + mono body, terminal motifs, tabular figures.
- Materiality: hairline grid, subtle scanline/noise on a FIXED overlay only,
  flat cards.
- Motion: type-scramble/typewriter once, otherwise still.

### 2.A The variance mandate + palette rotation
- **Never ship the same family twice in a row** for similar briefs. If the
  last SaaS site you built was clean-tech electric-blue, the next reaches for
  a different accent or family.
- **Rotate palettes.** Within a family, vary the accent and neutral temperature
  so two sites in the same family don't look identical.
- **State the family in the Design Read** so the choice is deliberate, not
  accidental.

### 2.B Honesty rule
Express the family in `app.css` tokens (`--ink`, `--bg`, `--accent`,
`--radius`, `--shadow`, font `@font-face`/`@import`). Load fonts self-hosted or
via a single `@import` in `app.css` — never a runtime `<link>` per component.
If the create-svelte-site pipeline already picked tokens, honour them and let
this skill govern layout/motion/materiality on top.

---

## 3. The static-prerender guardrail (non-negotiable — mirrors create-svelte-site)

These pages render to HTML **before any JavaScript runs**. `onMount` does NOT
run at prerender time. Taste must never depend on JS to look finished:

- **Resting state lives in MARKUP.** Every animated/interactive default's final
  visual state is rendered in the DOM. Never set the resting state only in
  `onMount` — the prerendered HTML would bake the *start* frame (the empty
  hero, the `$0` counter, the collapsed accordion). Ask: *"with all JS off,
  does this section look done?"* If not, move the final state into markup.
- **Tier-0 = CSS-only motion is the default.** Reveal-on-scroll, hovers,
  gradient drift, marquees — do them in CSS (`@keyframes`, `transition`,
  `:hover`, `animation-delay`). CSS animations run at paint with no JS.
- **JS motion is Tier-1, opt-in, and degrades.** A `tweened` count-up or an
  IntersectionObserver reveal may ENHANCE a resting state already correct in
  markup — it must never CREATE it.
- **Respect `prefers-reduced-motion`.** Wrap non-essential motion in
  `@media (prefers-reduced-motion: no-preference)`, or reveal immediately when
  the user opts out. Never trap content behind an animation.
- **Guard `window`/`document`.** They don't exist at prerender. Touch them only
  inside `onMount` or behind `typeof window !== 'undefined'`.

---

## 4. Typography

- **Display / headlines:** large, tight, weighty. A good default is
  `clamp(2.5rem, 5vw, 4.5rem)`, `letter-spacing: -0.03em`, `line-height: 1.05`.
  Control hierarchy with **weight and color**, not only scale.
- **Ban Inter for premium/creative briefs.** It's the single loudest AI tell.
  Reach for a distinctive grotesk — Geist, Satoshi, General Sans, Space
  Grotesk, Clash Display, Cabinet Grotesk, PP Neue Montreal. *Inter is only OK
  when the read is explicitly neutral/standard/Linear-style, or trust-first.*
- **Serif discipline.** Serif is the most-tested AI tell: "creative = serif" is
  a wrong reflex. Use serif ONLY for the editorial-luxury family (§2.C) or a
  brief that names one. **Banned as defaults: Fraunces and Instrument Serif**
  (the two LLM-favourite display serifs). If a serif is justified, rotate from
  PP Editorial New, Reckless Neue, Tiempos, Recoleta, Playfair, EB Garamond.
- **Emphasis within a headline** uses italic or bold of the SAME font — never
  inject a random serif word into a sans headline. Mixed-family emphasis is
  amateur. If an italic display word contains a descender (`y g j p q`), give
  it `line-height: 1.1` min + a little bottom padding so it isn't clipped.
- **Body:** `line-height: 1.6`, muted foreground (`color-mix(...)` toward the
  background, not pure gray), `max-width: 62ch`. Never run paragraphs full
  width.
- **Pair, don't monotype.** One display face + one text face. Numbers
  (pricing, stats) in mono or tabular figures read as intentional.

---

## 5. Color calibration

- **One accent, kept below ~80% saturation.** The accent earns attention
  because everything around it is neutral.
- **THE LILA BAN.** The AI purple/indigo→violet gradient and neon glows are
  banned as a default. Use a considered neutral base (warm OR cool — pick ONE)
  with a single high-contrast accent (deep emerald, electric blue, terracotta,
  deep rose, burnt orange). If the brand explicitly asks for purple, embrace
  it — but with intent, no second competing glow.
- **Color-consistency lock.** Once an accent is chosen it is used on the WHOLE
  page. A warm-grey site does not get a blue CTA in section 7. Audit every
  component before shipping.
- **Premium-consumer palette ban** (cookware / wellness / artisan / luxury /
  DTC): the LLM default is warm beige/cream + brass/clay/oxblood + espresso.
  It makes every premium brand invisible. **Banned as the default reach.**
  Rotate to a different family instead: cold-luxury (silver + chrome), forest
  (deep green + bone + amber), black-and-tan, cobalt + cream, terracotta +
  slate, or monochrome + one saturated pop. Only use beige+brass if the brand
  explicitly names it. Don't ship the same warm-craft palette twice in a row.
- **No pure black.** `#000` is harsh and flat. Use a tuned off-black
  (`#14110e`, `#0b0f14`) matched to the palette's temperature. Same for pure
  `#fff` grounds — nudge them off-white.
- **Consistency.** One palette top to bottom; don't drift warm↔cool between
  sections.

---

## 6. Layout diversification (the anti-center rule)

A marketing page is a *sequence* of sections; give them different shapes so the
eye keeps moving. At VARIANCE ≥ 5 the centered-headline-over-a-gradient hero is
banned.

- **Hero:** a split (`grid-template-columns: 1.1fr 0.9fr`) with copy left and a
  real asset/bespoke visual right — or an asymmetric left-aligned hero with a
  whitespace gutter. Not a centered stack on a blur. **Hero discipline:**
  headline ≤ 2 lines at desktop, subtext ≤ 20 words AND ≤ 4 lines, primary CTA
  visible without scroll, hero top padding capped (don't float content halfway
  down the viewport), full-height sections use `min-height: 100dvh` (never
  `100vh` — it jumps on mobile Safari). **Max 4 text elements in the hero**
  (eyebrow OR brand-strip, headline, subtext, CTAs). No trust micro-strip, no
  tagline-below-CTAs, no feature bullets in the hero — those move to sections
  below.
- **Features:** the generic "3 equal cards in a row" is banned. Use a **2-col
  zig-zag** (alternating image/text), an **asymmetric bento** (unequal spans),
  or a feature LIST with hairlines instead of boxes.
- **Section-layout-repetition ban.** Once a layout family is used for a section
  (3-col cards, full-width quote, split text/image, bento), it appears **at
  most once more**. An 8-section page uses at least 4 different layout families.
- **Zig-zag cap.** Max 2 consecutive image+text split sections. The 3rd in a
  row is a fail — break it with a full-width band, a stat row, a bento, or a
  marquee (max one marquee per page).
- **Eyebrow restraint** (the #1 violated rule). The small uppercase
  wide-tracking label above a headline should appear **at most once per 3
  sections** (hero counts as one). Mechanical check: count uppercase-tracking
  micro-labels across sections; if it exceeds `ceil(sectionCount / 3)`, remove
  some. Usually the headline alone is enough — the section's position already
  categorises it. No section-number eyebrows (`00 / INDEX`, `001 · Features`).
- **Split-header ban.** "Left big headline + right small floating explainer
  paragraph" as a section header is banned by default. Stack headline over body
  (`max-width: 62ch`) unless the right column carries a real visual.
- **Bento discipline.** A grid has exactly as many cells as content (3 items →
  3 cells, no empty tiles). At least 2-3 cells carry real visual variation (an
  image, a brand gradient, a pattern) — not all text-on-white.
- **Navigation renders on ONE line** at desktop, height ≤ 80px. Condense or
  collapse to a menu rather than wrapping to two lines.
- **CSS Grid, not flex-percentage math.** `grid-template-columns` with `fr` is
  reliable; `width: calc(33% - 1rem)` breaks.
- **Mobile collapses hard.** Every asymmetric layout falls back to a single
  clean column below ~768px.

---

## 7. Materiality & depth (anti-card-overuse)

- **Cards only when elevation means something.** A card says "this floats above
  the page." If nothing floats, group with whitespace, a `border-top` hairline,
  or a divided list instead of wrapping everything in boxes. At high density,
  drop card boxes entirely and separate with 1px lines.
- **Tint shadows to the background.** A shadow is occluded light, not a gray
  smear. `box-shadow: 0 20px 40px -20px rgba(<ink-rgb>, 0.18)` reads far more
  premium than a default black blur. Wide, soft, low-opacity beats tight+dark.
  No harsh `rgba(0,0,0,0.3)` drops.
- **Real glass, not just blur.** When you do glassmorphism, go past
  `backdrop-filter: blur()`: add a 1px inner light border
  (`border: 1px solid color-mix(in srgb, white 12%, transparent)`) and an inset
  highlight (`box-shadow: inset 0 1px 0 rgba(255,255,255,0.08)`) so the edge
  refracts. Provide a solid fallback under `prefers-reduced-transparency`.
- **Shape-consistency lock.** Pick ONE radius scale and apply it everywhere.
  Round buttons on a sharp-cornered layout (or vice versa) is broken. Mixed
  systems allowed only with a documented rule (e.g. buttons pill, cards 16px,
  inputs 8px) followed consistently.

### 7.A The double-bezel (soft-premium family)
For the soft-premium look, never place a card flatly on the background — nest
it like machined hardware:
- **Outer shell:** a wrapper with a faint fill (`background: rgba(255,255,255,0.05)`),
  a hairline border, small padding (`0.375rem`-`0.5rem`), large radius (`2rem`).
- **Inner core:** the real content container, its own background, its own inner
  highlight (`box-shadow: inset 0 1px 1px rgba(255,255,255,0.15)`), and a
  concentric smaller radius (`calc(2rem - 0.375rem)`).
- **Button-in-button CTA:** a trailing arrow lives inside its own circular
  wrapper flush with the button's right padding, not naked next to the text.

---

## 8. Motion in Svelte (translated from Motion/GSAP)

Default to **CSS**. Reach for runes/stores only for Tier-1 enhancement, and
only after the resting state is correct in markup.

- **Motion must be motivated.** Before adding any animation, name what it
  communicates: hierarchy, sequence/storytelling, feedback, or state change.
  "It looked cool" is not a reason. If you can't justify it in a sentence, drop
  it.
- **Motion claimed = motion shown.** If MOTION_INTENSITY > 4 the page actually
  moves (hero entrance, scroll-reveal on key sections, CTA hover). If you can't
  ship working motion, drop the dial to 3 and ship a clean static page — never
  half-built motion that breaks.
- **Scroll reveal → a `use:` action + CSS class.** A tiny `reveal` action adds
  an `.in` class when the element enters the viewport (IntersectionObserver);
  the element and its text are already in the DOM, and CSS transitions
  `opacity`/`transform` when `.in` lands. Reveal immediately under
  `prefers-reduced-motion`. (create-svelte-site ships this pattern.)
- **State → runes.** `let open = $state(false)`, `let active = $state(0)`,
  `const total = $derived(...)`. Set the resting value (first FAQ open, first
  tab active) in the initializer so it prerenders.
- **Count-ups → `tweened`/`spring`, seeded from the final value.** Initialize
  the store to the REAL total (so markup bakes it), then in `onMount` reset to
  0 and animate up on the client only:
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
- **Enter/leave within a section → Svelte `transition:`/`in:`/`out:`** on
  elements whose *content* is already present — polish an existing frame, never
  gate it.
- **Ambient motion → CSS keyframes** (slow gradient drift, marquee, floating
  badge). No JS, free at prerender. Marquee max one per page.
- **Custom easing = premium.** For soft-premium, use
  `cubic-bezier(0.32, 0.72, 0, 1)` and 600-800ms fade-up, not `linear`/`ease`.
- **Hardware-accelerate.** Animate only `transform` and `opacity` — never
  `top`/`left`/`width`/`height`. `will-change` sparingly.
- **No `window.addEventListener('scroll')`** — it re-runs every frame. Use
  IntersectionObserver, a `use:` action, or CSS scroll-driven animations.
- **No custom cursors, no scroll-hijacking, no mouse-follow.** They wreck
  accessibility and mobile and signal "demo", not "business".
- **Blur/noise only on fixed, `pointer-events: none` overlays** — never on
  scrolling containers (continuous GPU repaints kill mobile FPS).

---

## 9. Content, copy & data (the anti-slop core for words)

- **Em-dash is BANNED** (`—` and separator `–`). It is the single loudest text
  tell — no "sparingly" allowance. In headlines use a period or comma; in body
  restructure into two sentences, a comma, parentheses, or a colon; in
  attribution use ` - ` (spaced hyphen) or a line break. Ranges use a hyphen
  (`2018-2026`, `$40-80`). The only dash allowed anywhere visible is the plain
  hyphen `-`. A single `—`/`–` on the page fails pre-flight.
- **Copy self-audit before ship.** Re-read every visible string (headlines,
  subheads, eyebrows, buttons, body, captions, alt text, footer). Rewrite
  anything grammatically broken, with unclear referents, or that reads like an
  LLM trying to sound thoughtful (forced wordplay, mock-poetic micro-meta,
  fake-craftsman labels like "From the field" / "On our desks"). Plain
  functional copy beats cute-but-wrong copy.
- **No "Jane Doe" filler.** Write believable, varied, locale-appropriate names
  for testimonials — never "John Doe" / "Sarah Chan". Attribution is name +
  role + (optionally) company, never name only.
- **No tidy fake numbers** (`99.99%`, `50%`, `1,000+ users`). Use organic
  figures (`47.2%`, `2,140 businesses`, `+1 (312) 847-1928`). Don't fake
  engineering-precision specs the brand doesn't actually claim.
- **No startup-slop brand names** ("Acme", "Nexus", "SmartFlow"). Invent a
  contextual, ownable name for the business.
- **No filler verbs** — "Elevate", "Seamless", "Unleash", "Supercharge",
  "Next-Gen", "Empower", "Revolutionize". Write concrete, specific claims.
- **No duplicate CTA intent.** "Get in touch" + "Contact us" + "Let's talk" on
  one page = a fail. Pick ONE label per intent and use it in nav, hero, footer.
  CTA labels fit on one line at desktop (≤ 3 words for a primary CTA).
- **Quotes ≤ 3 lines** of body; a landing-page quote is a snippet, not the full
  review. Use real typographic quotes or none — not straight ASCII, and no
  em-dash inside.
- **Content density is lean.** Per section: short headline (≤ 8 words) + short
  sub-paragraph (≤ 25 words) + one asset or one CTA. No 20-row spec tables or
  giant pricing matrices on a marketing page — top 3-5 + "view full" instead.

---

## 10. AI tells — forbidden patterns

Strictly avoid unless the user explicitly asks:

**Visual / CSS**
- NO neon/outer glows; use inner light borders and tinted shadows.
- NO pure `#000` or `#fff`; use tuned off-black / off-white.
- NO oversaturated accents; desaturate to sit inside the neutrals.
- NO gradient-filled headline text as a default flourish.
- NO custom mouse cursors.
- NO decorative colored status dots on every nav item / list row / badge (only
  for real semantic state, sparingly).
- NO crosshair/hairline grid lines drawn purely as decoration.

**Typography**
- NO Inter for premium/creative (§4). NO Fraunces / Instrument Serif serifs.
- NO screaming oversized H1 as the only hierarchy tool — use weight + color.

**Layout**
- NO centered hero over a gradient blur (§6). NO three-equal-card feature row.
- NO eyebrow on every section (§6). NO section-number eyebrows.
- NO version labels in the hero (`V0.6`, `BETA`, `EARLY ACCESS`) unless the
  brief is literally a launch.
- NO decoration text strip at the hero bottom (`BRAND. MOTION. SPATIAL.`).
- NO `border-top` + `border-bottom` on every row of a long list/spec table —
  reach for a card grid, grouped chunks, or a scroll-snap instead.

**Content & external**
- NO "John Doe", `99.99%`, "Acme", filler verbs (§9). NO em-dash (§9).
- NO locale/time/weather strips (`Lisbon 14:23 · 18°C`) unless the brand is
  genuinely place- or timezone-focused.
- NO scroll cues (`Scroll`, `↓ scroll`, animated mouse-wheel).
- NO pills/labels overlaid on images (`Plate · 02`); caption below the image if
  needed. NO pretentious photo-credit captions (`Frame XII · 35mm`) — real
  photographer credit only.
- NO broken/invented image URLs. Pull real photography via
  `search_stock_images` and render its `credit`; fall back to a tasteful
  gradient/solid — never a broken `<img>`.
- NO div-based fake product screenshots (fake dashboards/terminals built from
  `<div>`s). Use a real screenshot, a generated image, a real mini-component,
  or skip the preview.
- NO emoji as UI (icons, buttons, bullets). Use real SVG icons (`search_icons`)
  or clean SVG primitives. NO hand-rolled decorative SVG illustrations as a
  default.

---

## 11. Pre-flight check (mechanical — last filter before hand-off)

Run every box. If one can't be honestly ticked, the page is not done.

- [ ] **Design Read** declared as a one-liner (§0.B), and a **family** picked
      (§2), not a defaulted house style.
- [ ] **Dials** set from the read (§1), not silently baseline.
- [ ] With **all JS disabled**, every section looks finished — resting state in
      markup, not `onMount` (§3).
- [ ] **ZERO em-dashes** (`—`/`–`) anywhere visible — headlines, body, quotes,
      captions, buttons, alt (§9).
- [ ] Hero is NOT centered-over-gradient; **hero discipline** holds (≤ 2-line
      headline, ≤ 20-word subtext, CTA above the fold, ≤ 4 text elements,
      `min-height: 100dvh`).
- [ ] No three-equal-card feature row; **≥ 4 different layout families** across
      the page; **no 3rd consecutive** image+text split.
- [ ] **Eyebrow count** ≤ `ceil(sectionCount / 3)`; no section-number eyebrows.
- [ ] One accent < 80% saturation, no purple/neon glow; **color-consistency
      lock** holds; premium-consumer palette not the banned beige+brass default.
- [ ] Off-black not `#000`, off-white not `#fff`.
- [ ] Display face is distinctive (not Inter for premium); serif only if the
      family/brief justifies it and it isn't Fraunces/Instrument; body
      measure-capped.
- [ ] **Shape-consistency lock**: one radius system throughout.
- [ ] Cards used only where something floats; shadows tinted; glass refracts
      (or double-bezel applied for soft-premium).
- [ ] Motion CSS-first, motivated, `prefers-reduced-motion` honored, only
      `transform`/`opacity` animated, no `scroll` listener / cursor / hijack;
      marquee ≤ 1.
- [ ] **Copy self-audit** done — no broken/AI-hallucinated strings; names,
      numbers, brand name specific and believable; no filler verbs.
- [ ] **No duplicate CTA intent**; CTA labels fit one line.
- [ ] Real images via `search_stock_images` with credit rendered; no broken
      `<img>`, no div-based fake screenshots, no hand-rolled decorative SVG.
- [ ] Nav on one line ≤ 80px; every asymmetric layout collapses to one clean
      column < 768px.
