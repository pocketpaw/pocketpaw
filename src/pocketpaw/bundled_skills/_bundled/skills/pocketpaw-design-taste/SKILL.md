---
name: pocketpaw-design-taste
description: |
  Engine-agnostic design-taste discipline for authoring PREMIUM marketing
  landing pages on ANY Paw Sites engine — hand-written static HTML/CSS (the
  default), ripple widget specs, or Svelte components. Invoke it whenever you
  design or build a site's sections (Hero, Features, Pricing, Testimonial, Faq,
  CTA, Footer). It runs the full anti-slop framework, not just guardrails:
  FIRST read the room and INFER a one-line "Design Read" (never ask the user
  what theme to use — choosing the look is YOUR job), THEN commit to an
  aesthetic DIRECTION (clean-tech / soft-premium / editorial-luxury /
  warm-minimalist / brutalist / dark-tech) that drives palette, type,
  materiality, and motion, THEN author sections that honour it. This is what
  stops every site looking like the same clean AI house style. It overrides the
  default LLM biases toward centered heroes over a gradient blur, three equal
  cards, Inter + slate, purple glows, an eyebrow on every section, em-dashes,
  and "John Doe" filler. Everything is authored so the page looks finished with
  NO JavaScript (resting state in markup, CSS-first motion) — true for static
  HTML, prerendered Svelte, and static ripple pages alike. For the Svelte track
  specifically, pair with design-taste-svelte for the runes/prerender specifics;
  this skill owns the engine-agnostic taste.
---

> **Attribution.** The framework here — brief-inference protocol, the three
> dials, the design-direction families, color/typography calibration, layout
> and materiality rules, and the "AI tells" list — is adapted from
> **leonxlnx's "taste-skill" / "design-taste-frontend"** —
> https://github.com/Leonxlnx/taste-skill (MIT), generalized to the Paw Sites
> static-page reality (HTML/CSS-first, engine-agnostic). License and credit
> retained per MIT terms.

# Design taste for Paw Sites (any engine)

You are a senior designer + frontend engineer authoring a marketing landing
page that ships as a **static page** (no JavaScript runs for the visitor on
first paint). LLMs have a strong statistical pull toward a handful of clichés
and toward ONE default aesthetic; this skill exists to override both. The order
is fixed:

1. **Read the room** and INFER a one-line Design Read (§0) — do NOT ask the user
   what look to use.
2. **Pick a direction** — an aesthetic family that sets palette, type,
   materiality, and motion (§2).
3. **Set the dials** from the read (§1).
4. **Author the sections** honouring the direction, on your engine (§3).

Every rule below is **contextual** — none fires automatically. First read the
brief, then pull only what fits. If a rule fights an explicit brand choice, the
brand wins on *color*; this skill still governs layout, motion, and materiality.

---

## 0. BRIEF INFERENCE — read the room, then decide the look yourself

Most LLM site output is bad because the model jumps straight to a default look
OR stops to ask the user "what style do you want?". Do NEITHER. Choosing the
aesthetic is your expertise; infer it from what the business is.

### 0.A Read these signals
1. **Business kind** — SaaS / dev-tool, agency / studio, premium consumer or
   DTC, local service (dentist, bakery, gym), event, portfolio, editorial.
2. **Vibe words the user used** — "minimal", "calm", "Linear-style", "bold",
   "premium", "Apple-y", "playful", "serious B2B", "editorial", "brutalist",
   "warm", "luxury".
3. **Reference signals** — any URL, screenshot, or brand they named or want to
   compete with. If they linked something, that is the strongest signal in the
   room; match its family, don't override it.
4. **Audience** — a procurement buyer, a design-conscious consumer, a local
   walk-in, a recruiter. The audience picks the aesthetic, not your taste.
5. **Existing brand assets** — a named color, a logo, a font. Honour them.
6. **Quiet constraints** — trust-first / regulated / accessibility-critical
   audiences OVERRIDE aesthetic preference toward calm and legible.

### 0.B Declare a one-line Design Read (say it, then build to it)
> **"Reading this as: a `<business kind>` for `<audience>`, `<vibe>` in feel,
> so I'm going `<direction>` with `<accent>` on `<neutral base>` and
> `<display face>`."**

Examples:
- *"Reading this as a B2B SaaS for technical buyers, Linear-clean in feel, so
  I'm going clean-tech with electric-blue on cool graphite and Space Grotesk."*
- *"Reading this as an artisan bakery for local walk-ins, warm and tactile, so
  I'm going editorial-luxury with brick-red on bone and a display serif."*

### 0.C Do NOT ask the user what theme to use
The visual direction is YOUR call — infer it and go. Only ask the user about
things you genuinely cannot know and cannot sensibly default: real-world FACTS
(the exact business name if not given, real contact details, real pricing, a
specific offering list) — and even then, prefer to proceed with the read and a
placeholder you clearly flag, rather than blocking. **Never** ask "what style
/ colors / vibe do you want?" — that is the one question this skill forbids.

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
- **MOTION_INTENSITY: 5** — 1 = fully static, 10 = cinematic. Held moderate:
  the page is static, so first paint has no JS. Motion is CSS-first and never
  load-bearing (§3).
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
- **VARIANCE 4-7:** offset overlaps, mixed aspect ratios, left-aligned headers,
  asymmetric fractional grids (`grid-template-columns: 2fr 1fr`), deliberate
  empty zones. Any asymmetry collapses to one clean column below 768px.
- **MOTION 4-7:** CSS transitions + reveal cascades on `transform`/`opacity`,
  still degrading gracefully.
- **DENSITY 1-3:** big section gaps (`padding: 6rem 0` to `9rem 0`). **4-7:**
  standard (`4rem`-`6rem`). Honour an explicit user request that moves a dial.

---

## 2. Design direction — pick an aesthetic FAMILY (the anti-same-y rule)

This is the layer that makes each site look *designed for this business* rather
than "clean AI landing page No. 47". Commit to ONE family and let it drive the
palette, the display/body faces, the radius scale, the shadow character, and
the motion feel. **One family per site, top to bottom.** Do not blend two.

**A. Clean-Tech (Linear / Vercel)** — SaaS, dev-tools, AI. Cool graphite/zinc
neutrals, off-black `#0b0f14` ground, ONE saturated accent (electric blue,
emerald; no purple). Geometric grotesk (Space Grotesk, General Sans), mono for
numbers. Hairline borders, near-flat cards, small radius (8-12px). Crisp short
motion (150-300ms).

**B. Soft-Premium (Awwwards / agency-tier)** — brand, premium consumer,
studios. Silver-grey or deep OLED black grounds, soft diffused ambient shadows,
one refined accent. Large bold grotesk display (Clash Display, Cabinet Grotesk),
heavy weight, tight tracking. Nested "machined" enclosures, exaggerated squircle
radii (`2rem`), macro-whitespace (`6rem`-`10rem`). Heavy spring easing
(`cubic-bezier(0.32,0.72,0,1)`), staggered fade-up.

**C. Editorial-Luxury** — lifestyle, food, real estate, heritage, publications.
Warm bone/cream ground, deep espresso or brick-red accent. A display **serif**
(one of the few places serif is right — PP Editorial New, Tiempos, Playfair)
paired with a clean sans body. Flat, editorial, asymmetric grid, hairline rules
not boxes. Minimal, slow motion. (Beware the beige+brass default — rotate, §5.)

**D. Warm-Minimalist (Notion / editorial-doc)** — productivity, content, calm
consumer. Warm off-white `#faf9f6` ground, charcoal `#2f3437` text, muted
pastel spot accents used only semantically. Clean humanist sans (Switzer). Crisp
1px `#eaeaea` borders, small radius, near-zero shadows. No gradients. Subtle
functional motion.

**E. Brutalist / Structural** — bold statements, dev-culture, events, drops.
Raw black-on-white (or one loud flat color), high contrast, no gradients/soft
shadows. Mono or condensed grotesk, oversized, tight, often uppercase. Hard
borders (2-3px), sharp corners (radius 0), visible grid, offset. Instant/snappy.

**F. Dark-Tech / Terminal** — security, infra, crypto. Deep near-black, one
neon-ish accent used *sparingly* (no page-wide glow), mono everywhere. Hairline
grid, subtle scanline/noise on a FIXED overlay only, flat cards. Mostly still.

### 2.A Variance mandate + palette rotation
- **Never ship the same family twice in a row** for similar briefs. If the last
  SaaS site was clean-tech electric-blue, the next reaches for a different accent
  or family.
- **Rotate palettes** within a family so two sites don't look identical.
- **State the family in the Design Read** so the choice is deliberate.

---

## 3. The static guardrail (non-negotiable, every engine)

These pages render to HTML before any JS runs. Taste must never depend on JS to
look finished:

- **Resting state lives in MARKUP.** Every animated/interactive default's final
  visual state is in the DOM. Never render the resting state only via
  JS/`onMount`. Ask: *"with all JS off, does this section look done?"* If not,
  move the final state into markup. (No `$0` counters, no collapsed-only
  accordions, no empty hero that only fills in on load.)
- **Tier-0 = CSS-only motion is the default.** Reveal-on-scroll, hovers,
  gradient drift, marquees — do them in CSS (`@keyframes`, `transition`,
  `:hover`, `animation-delay`). They run at paint with no JS.
- **JS motion is opt-in and degrades.** A count-up or IntersectionObserver
  reveal may ENHANCE a resting state already correct in markup — never CREATE it.
- **Respect `prefers-reduced-motion`.** Wrap non-essential motion in
  `@media (prefers-reduced-motion: no-preference)`, or reveal immediately on
  opt-out. Never trap content behind an animation.
- **No layout shift.** Set `width`/`height` (or `aspect-ratio`) on every image
  and media element so the page doesn't jump as assets load.
- **Support light AND dark** where the family allows: use
  `prefers-color-scheme` and design both variants so hierarchy and contrast hold
  in each.

*Engine notes.* **HTML track:** all of the above is plain CSS in `styles.css` +
resting markup in `index.html`. **Svelte track:** also load `design-taste-svelte`
for the runes/prerender/onMount specifics. **Ripple track:** express tokens in
the spec, keep interactive widgets to their resting state, animations Tier-0.

---

## 4. Typography

- **Display / headlines:** large, tight, weighty —
  `clamp(2.5rem, 5vw, 4.5rem)`, `letter-spacing: -0.03em`, `line-height: 1.05`.
  Control hierarchy with **weight and color**, not scale alone. Oversized,
  confident type is the 2026 premium signal — let the value prop be the visual.
- **Ban Inter for premium/creative briefs** — the loudest AI tell. Reach for a
  distinctive grotesk (Space Grotesk, Satoshi, General Sans, Clash Display,
  Cabinet Grotesk). Inter is only OK when the read is explicitly
  neutral/standard/Linear-style or trust-first.
- **Serif discipline.** Serif ONLY for editorial-luxury (§2.C) or a brief that
  names one. **Banned as defaults: Fraunces, Instrument Serif.** If justified,
  rotate PP Editorial New, Tiempos, Recoleta, Playfair, EB Garamond.
- **Emphasis within a headline** uses italic/bold of the SAME font — never inject
  a random serif word into a sans headline.
- **Body:** `line-height: 1.6`, muted foreground (`color-mix` toward the
  background, not pure gray), `max-width: 62ch`. Never run paragraphs full width.
- **Pair, don't monotype.** One display face + one text face. Numbers in mono or
  tabular figures read as intentional. Load fonts via a single `@import`/self-host,
  not a per-element runtime `<link>`.

---

## 5. Color calibration

- **One accent, kept below ~80% saturation.** It earns attention because
  everything around it is neutral. Reserve the HIGHEST contrast for the primary
  CTA and critical info — if everything is loud, nothing is important.
- **THE LILA BAN.** The AI purple/indigo→violet gradient and neon glows are
  banned as a default. Use a considered neutral base (warm OR cool, pick ONE)
  with a single high-contrast accent (deep emerald, electric blue, terracotta,
  deep rose, burnt orange). Embrace purple only if the brand names it.
- **Color-consistency lock.** One accent, used on the WHOLE page. A warm-grey
  site does not get a blue CTA in section 7. Audit before shipping.
- **Premium-consumer palette ban** (cookware / wellness / artisan / luxury /
  DTC): the LLM default is warm beige/cream + brass/clay + espresso, and it
  makes every premium brand invisible. Rotate to cold-luxury (silver + chrome),
  forest (deep green + bone + amber), black-and-tan, cobalt + cream, or
  monochrome + one pop. Only use beige+brass if the brand explicitly names it.
- **No pure black or white.** Use tuned off-black (`#14110e`, `#0b0f14`) and
  off-white grounds matched to the palette temperature.

---

## 6. Layout diversification (the anti-center rule)

A marketing page is a *sequence* of sections; give them different shapes so the
eye keeps moving. At VARIANCE ≥ 5 the centered-headline-over-a-gradient hero is
banned.

- **Hero:** a split (`grid-template-columns: 1.1fr 0.9fr`) with copy left and a
  real asset/visual right — or an asymmetric left-aligned hero with a whitespace
  gutter. Not a centered stack on a blur. Headline ≤ 2 lines desktop, subtext
  ≤ 20 words AND ≤ 4 lines, primary CTA above the fold, `min-height: 100dvh`
  (never `100vh`). Max 4 text elements in the hero.
- **Features:** the generic "3 equal cards in a row" is banned. Use a 2-col
  zig-zag (alternating image/text), an asymmetric bento (unequal spans), or a
  feature LIST with hairlines instead of boxes.
- **Section-layout-repetition ban.** Once a layout family is used (3-col cards,
  full-width quote, split text/image, bento), it appears at most once more. An
  8-section page uses ≥ 4 different layout families.
- **Zig-zag cap.** Max 2 consecutive image+text splits; break the 3rd with a
  full-width band, a stat row, a bento, or one marquee (max one per page).
- **Eyebrow restraint** (the #1 violated rule). The small uppercase
  wide-tracking label above a headline appears at most once per 3 sections
  (hero counts). No section-number eyebrows (`001 · Features`).
- **Bento discipline.** Exactly as many cells as content (3 items → 3 cells).
  ≥ 2-3 cells carry real visual variation, not all text-on-white.
- **Nav renders on ONE line** at desktop, height ≤ 80px.
- **CSS Grid, not flex-percentage math.** `grid-template-columns` with `fr`.
- **Mobile collapses hard** to one clean column below ~768px.

---

## 7. Materiality & depth (anti-card-overuse)

- **Cards only when elevation means something.** If nothing floats, group with
  whitespace, a `border-top` hairline, or a divided list instead of boxing
  everything.
- **Tint shadows to the background** — occluded light, not a gray smear:
  `box-shadow: 0 20px 40px -20px rgba(<ink-rgb>, 0.18)`. Wide, soft, low-opacity
  beats tight+dark. No harsh `rgba(0,0,0,0.3)` drops.
- **Real glass, not just blur.** Add a 1px inner-light border and an inset
  highlight so the edge refracts; provide a solid fallback under
  `prefers-reduced-transparency`.
- **Shape-consistency lock.** ONE radius scale everywhere (or a documented rule:
  buttons pill, cards 16px, inputs 8px), applied consistently.

---

## 8. Trust & conversion

- **Trust signals earn their place.** Real (or clearly-plausible) testimonials
  with name + role + company, recognizable-logo strips, certifications/security
  badges — placed near the CTA and pricing, not dumped in a wall.
- **Specificity is the conversion lever.** Replace abstract benefit language
  with concrete outcomes tied to a clear audience. "Cut invoice time from 3 days
  to 20 minutes" beats "Streamline your workflow." Claims stay realistic and
  decision-oriented.
- **One primary CTA intent**, repeated in nav/hero/footer with ONE label. The
  lead-capture form uses real named fields (name, email, phone, message) and a
  submit button — never a decorative dead button.

---

## 9. Content, copy & data (the anti-slop core for words)

- **Em-dash is BANNED** (`—` and separator `–`) — the loudest text tell. Use a
  period/comma in headlines; restructure body into two sentences, a comma,
  parentheses, or a colon; use ` - ` in attribution. Ranges use a hyphen
  (`2018-2026`, `$40-80`). A single `—`/`–` visible fails pre-flight.
- **No "Jane Doe" filler.** Believable, varied, locale-appropriate names.
- **No tidy fake numbers** (`99.99%`, `1,000+ users`). Use organic figures
  (`47.2%`, `2,140 businesses`). Don't fabricate specs the brand doesn't claim.
- **No startup-slop names** ("Acme", "Nexus"). Invent a contextual, ownable name.
- **No filler verbs** — "Elevate", "Seamless", "Unleash", "Supercharge",
  "Next-Gen", "Empower", "Revolutionize". Write concrete, specific claims.
- **No duplicate CTA intent.** One label per intent, ≤ 3 words for a primary CTA.
- **Content density is lean.** Per section: short headline (≤ 8 words) + short
  sub-paragraph (≤ 25 words) + one asset or one CTA.
- **Don't fabricate real-world facts.** If you don't know the address, hours,
  price, or a real testimonial, use an obviously-generic placeholder and flag it
  — never invent a specific false fact.

---

## 10. AI tells — forbidden patterns (avoid unless the brief asks)

**Visual:** NO neon/outer glows (use inner-light borders + tinted shadows); NO
pure `#000`/`#fff`; NO oversaturated accents; NO gradient-filled headline text
as a default; NO custom cursors; NO decorative status dots on every row; NO
decorative crosshair grid lines.
**Typography:** NO Inter for premium/creative; NO Fraunces / Instrument Serif;
NO giant H1 as the only hierarchy tool (use weight + color).
**Layout:** NO centered hero over a gradient blur; NO three-equal-card feature
row; NO eyebrow on every section; NO version labels / scroll cues / decoration
text strips in the hero.
**Content & assets:** NO "John Doe", `99.99%`, "Acme", filler verbs, em-dash; NO
locale/time/weather strips; NO pills overlaid on images; NO broken/invented image
URLs (pull real photography via `search_stock_images`, render its `credit`, or
fall back to a tasteful gradient); NO div-based fake dashboards; NO emoji as UI
(use real SVG via `search_icons`).

---

## 11. Pre-flight check (mechanical — last filter before publish)

Tick every box; if one can't be honestly ticked, the page is not done.

- [ ] **Design Read** declared as a one-liner (§0.B) and a **family** picked
      (§2) — inferred, not a defaulted house style, and not asked of the user.
- [ ] **Dials** set from the read (§1).
- [ ] With **all JS disabled**, every section looks finished — resting state in
      markup (§3); no layout shift (media has width/height/aspect-ratio).
- [ ] **ZERO em-dashes** (`—`/`–`) anywhere visible.
- [ ] Hero is NOT centered-over-gradient; hero discipline holds (≤ 2-line
      headline, ≤ 20-word subtext, CTA above the fold, `min-height: 100dvh`).
- [ ] No three-equal-card feature row; **≥ 4 different layout families**; no 3rd
      consecutive image+text split.
- [ ] **Eyebrow count** ≤ `ceil(sectionCount / 3)`; no section-number eyebrows.
- [ ] One accent < 80% saturation, no purple/neon glow; color-consistency lock
      holds; highest contrast reserved for the primary CTA; premium-consumer
      palette not the banned beige+brass default.
- [ ] Off-black not `#000`, off-white not `#fff`.
- [ ] Distinctive display face (not Inter for premium); serif only if justified
      and not Fraunces/Instrument; body measure-capped at ~62ch.
- [ ] **Shape-consistency lock**: one radius system throughout.
- [ ] Cards only where something floats; shadows tinted; glass refracts.
- [ ] Motion CSS-first, motivated, `prefers-reduced-motion` honored, only
      `transform`/`opacity` animated; marquee ≤ 1.
- [ ] **Copy self-audit**: no broken/hallucinated strings; specific believable
      names/numbers/brand; no filler verbs; specificity over abstract benefits.
- [ ] Trust signals present near CTA/pricing; **no duplicate CTA intent**.
- [ ] Real images via `search_stock_images` with credit; no broken `<img>`, no
      div-based fake screenshots; icons via `search_icons`, no emoji UI.
- [ ] Nav on one line ≤ 80px; every asymmetric layout collapses to one clean
      column < 768px; light/dark both hold if the family uses both.
