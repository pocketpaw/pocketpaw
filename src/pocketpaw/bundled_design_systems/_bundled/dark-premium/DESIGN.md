---
name: Obsidian
slug: dark-premium
description: >-
  A deep, dark, editorial-luxury system. Warm charcoal canvas, a champagne-gold
  primary and an emerald secondary, high-contrast serif display type, and glow
  plus deep shadow for depth. For premium brands, agencies, and products that
  want to feel expensive and considered.
aesthetic: [dark, premium, luxury, editorial, elegant, moody, sophisticated]
industries: [luxury, hospitality, fine-dining, real-estate, jewelry, agency, fintech-premium, portfolio]
page_types: [landing, portfolio, product, about, showcase, coming-soon]
theme: dark
colors:
  primary:   # champagne gold — the luxury accent
    "50": "#F9F7F3"
    "100": "#F4F0E6"
    "200": "#ECE2CB"
    "300": "#E4D1A5"
    "400": "#DABD79"
    "500": "#D0A94E"
    "600": "#BF9533"
    "700": "#9C7A2B"
    "800": "#755D24"
    "900": "#4E3F1B"
  secondary:   # emerald — a jewel-tone counterpoint to gold
    "50": "#F4F9F7"
    "100": "#E7F3EE"
    "200": "#CDEADE"
    "300": "#A9DFCA"
    "400": "#80D3B2"
    "500": "#57C79A"
    "600": "#3DB685"
    "700": "#33946D"
    "800": "#297054"
    "900": "#1E4A39"
  neutral:   # warm charcoal — the dark canvas (900) up to near-white text (50)
    "50": "#F6F6F6"
    "100": "#EDECEB"
    "200": "#D9D8D6"
    "300": "#C0BDB9"
    "400": "#A39F99"
    "500": "#878178"
    "600": "#6C6760"
    "700": "#514D48"
    "800": "#312E2B"
    "900": "#191715"
typography:
  fonts:
    display: "'Playfair Display', 'Times New Roman', serif"
    heading: "'Playfair Display', 'Times New Roman', serif"
    body: "'Manrope', system-ui, sans-serif"
    mono: "'JetBrains Mono', ui-monospace, monospace"
  scale:   # display → caption — high-contrast 1.333 editorial ratio
    display: { size: "5.61rem",  weight: 500, line_height: 1.05, tracking: "-0.01em" }
    h1:      { size: "4.209rem", weight: 500, line_height: 1.1,  tracking: "-0.01em" }
    h2:      { size: "3.157rem", weight: 500, line_height: 1.15, tracking: "0" }
    h3:      { size: "2.369rem", weight: 500, line_height: 1.2,  tracking: "0" }
    h4:      { size: "1.777rem", weight: 600, line_height: 1.3,  tracking: "0" }
    body_lg: { size: "1.25rem",  weight: 400, line_height: 1.7,  tracking: "0.005em" }
    body:    { size: "1rem",     weight: 400, line_height: 1.75, tracking: "0.01em" }
    body_sm: { size: "0.875rem", weight: 400, line_height: 1.6,  tracking: "0.01em" }
    caption: { size: "0.75rem",  weight: 500, line_height: 1.4,  tracking: "0.12em" }
spacing:   # 4px base, generous editorial rhythm
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "1.75rem"
  xl: "3rem"
  "2xl": "5rem"
  "3xl": "8rem"
rounded:   # subtle — luxury reads as precise, near-square
  none: "0"
  sm: "0.25rem"
  md: "0.375rem"
  lg: "0.5rem"
  xl: "0.75rem"
  full: "9999px"
elevation:   # deep shadow + a soft gold glow for the hero accent
  sm: "0 2px 8px rgba(0, 0, 0, 0.4)"
  md: "0 8px 24px rgba(0, 0, 0, 0.5)"
  lg: "0 20px 50px rgba(0, 0, 0, 0.6)"
  xl: "0 0 40px rgba(208, 169, 78, 0.25)"
components:
  button:
    default: { bg: "primary.500", fg: "neutral.900", radius: "rounded.md", padding: "0.75rem 1.75rem", weight: 600, tracking: "0.02em", shadow: "elevation.sm" }
    hover:   { bg: "primary.400", shadow: "elevation.xl" }
    focus:   { ring: "2px", ring_color: "primary.300", outline: "none" }
    active:  { bg: "primary.600" }
  card:
    default: { bg: "neutral.800", border: "1px solid neutral.700", radius: "rounded.lg", padding: "2rem", shadow: "elevation.md" }
    hover:   { border: "1px solid primary.700", shadow: "elevation.lg" }
  input:
    default: { bg: "neutral.900", border: "1px solid neutral.700", radius: "rounded.md", padding: "0.75rem 1rem", fg: "neutral.100" }
    hover:   { border: "1px solid neutral.600" }
    focus:   { border: "1px solid primary.500", ring: "2px", ring_color: "primary.900", outline: "none" }
---

# Obsidian — dark premium

## Overview

Obsidian is built to feel expensive. It sits on a warm charcoal canvas, sets
headlines in a high-contrast serif, and spends its color budget on champagne
gold — used the way a luxury brand uses gold leaf: sparingly, on the details
that matter. Emerald appears as a jewel-tone second note. Depth comes from real
darkness and a soft gold glow, not from busy gradients.

Use it for luxury goods, fine dining, hospitality, high-end real estate, premium
agencies, and portfolios that want gravitas. The mood is quiet confidence and
craft. Restraint is non-negotiable — a dark premium look fails the instant it
gets cluttered or neon.

## Colors

- **Canvas** is `neutral.900` (`#191715`), a warm near-black with a whisper of
  brown so it feels considered rather than clinical. Elevated surfaces (cards,
  nav) step up to `neutral.800`.
- **Text** is the light end of the neutral scale: `neutral.100`/`50` for
  headings and body, `neutral.300`/`400` for muted meta. Never pure white on the
  charcoal — the slight warmth keeps it soft on the eye.
- **Primary (champagne gold)** is the luxury signal. Reserve `primary.500` for
  the primary CTA, a hairline divider, a small icon, or a thin underline on the
  hero word. Gold text on charcoal (`primary.400`/`500`) reads elegant; gold
  fills should be small.
- **Secondary (emerald)** is the jewel accent — a badge, a price highlight, a
  second data hue. Gold + emerald on charcoal is a classic luxury pairing; keep
  emerald rarer than gold.
- Contrast the light text carefully; body copy at `neutral.100` on `neutral.900`
  clears AA comfortably.

## Typography

Playfair Display sets every headline — a high-contrast Didone serif whose thick
and thin strokes read as editorial luxury at large sizes. It is a display face:
use it big, and never for body copy. Manrope carries body text — a clean,
slightly geometric sans with open counters that stays legible on a dark canvas.
JetBrains Mono handles the occasional spec, price, or label; caption text uses
wide `0.12em` uppercase tracking for a refined eyebrow.

- Display and H1 in Playfair, large and airy, are the centerpiece.
- Body in Manrope at a generous 1.75 line-height with a hair of positive
  tracking — light text on dark needs the extra spacing to breathe.
- Wide-tracked uppercase captions signal luxury; use them for eyebrows and
  section labels.

## Layout

- Editorial and unhurried. Wide margins, generous `spacing.2xl`/`3xl` between
  sections, and one clear focal point per screen.
- Let darkness be the negative space — full-bleed charcoal sections with a single
  gold detail are more premium than a busy grid.
- Thin gold hairline rules (1px `primary.700`/`800`) segment sections with
  elegance.
- Large, high-quality imagery framed with subtle borders; let photos sit in the
  dark rather than filling every pixel.

## Elevation & Depth

Depth is dramatic but controlled. Cards lift off the canvas with deep, soft
black shadows (`elevation.md`/`lg`); the hero CTA and key accents can carry a
soft gold glow (`elevation.xl`, `0 0 40px rgba(208,169,78,0.25)`) that reads as
lit rather than shadowed. Use the glow rarely — one glowing element per view.
Elevated surfaces get lighter (`neutral.800`), not darker; light-from-above is
the mental model.

## Shapes

Precise and near-square. `rounded.md` (6px) on buttons and inputs, `rounded.lg`
(8px) on cards. Luxury reads as exactness, so the rounding is subtle — enough to
avoid harshness, not enough to look playful. Full-round is reserved for avatars
and small status dots.

## Components

- **Button** — gold fill with charcoal label (`neutral.900`), subtle tracking,
  small radius. On hover it brightens to `primary.400` and gains the gold glow;
  active settles to `primary.600`. A secondary/"ghost" button is a 1px gold
  outline with gold text on the charcoal canvas.
- **Card** — `neutral.800` surface, 1px `neutral.700` border, deep soft shadow.
  On hover the border warms to `primary.700` gold and the shadow deepens. Ideal
  for product tiles, testimonials, and pricing.
- **Input** — sits on the darkest `neutral.900` with a `neutral.700` border and
  light text; focus swaps the border to gold with a faint `primary.900` ring.
  The border carries the state; the ring is a quiet reinforcement.

## Do's and Don'ts

**Do**
- Spend gold like it is precious — small, deliberate, on the details.
- Keep the canvas warm charcoal (`neutral.900`), not pure black.
- Use one glowing element per view for the focal accent.
- Set headlines big in Playfair and give sections room to breathe.

**Don't**
- ❌ Don't flood surfaces with gold — a gold-heavy page reads as cheap, not
  luxurious. Gold is a detail, not a wash.
- ❌ Don't set Playfair as body copy; its high contrast shreds legibility at
  small sizes. Body is Manrope.
- ❌ Don't use pure black (`#000`) or pure white (`#FFF`) — the warm off-values
  are what make it feel crafted.
- ❌ Don't add neon or a third bright accent; it destroys the restrained,
  expensive mood.
- ❌ Don't clutter. Empty dark space is the luxury; fill it and you lose the
  entire effect.
