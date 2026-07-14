---
name: Obsidian
slug: dark-premium
description: >-
  A deep, dark, editorial-luxury system. Cool graphite canvas, a jade-emerald
  primary and a cool-steel secondary, high-contrast serif display type, and glow
  plus deep shadow for depth. For premium brands, agencies, and products that
  want to feel expensive and considered.
aesthetic: [dark, premium, luxury, editorial, elegant, moody, sophisticated]
industries: [luxury, hospitality, fine-dining, real-estate, jewelry, agency, fintech-premium, portfolio]
page_types: [landing, portfolio, product, about, showcase, coming-soon]
theme: dark
colors:
  primary:   # jade emerald — the jewel accent, not gold
    "50": "#E8FDF6"
    "100": "#C7FAE9"
    "200": "#94F5D4"
    "300": "#61F0C0"
    "400": "#32EBAD"
    "500": "#16DF9C"
    "600": "#12BA82"
    "700": "#0E9568"
    "800": "#0A6B4B"
    "900": "#06412D"
  secondary:   # cool steel blue — a restrained metal counterpoint
    "50": "#EFF2F5"
    "100": "#D9E0E8"
    "200": "#B6C3D2"
    "300": "#94A7BD"
    "400": "#748DAA"
    "500": "#5D7898"
    "600": "#4E647E"
    "700": "#3E5065"
    "800": "#2D3A49"
    "900": "#1B232C"
  neutral:   # cool graphite — the dark canvas (900) up to near-white text (50), never warm charcoal
    "50": "#F2F2F3"
    "100": "#DFE0E1"
    "200": "#C3C4C6"
    "300": "#A6A8AB"
    "400": "#8B8E92"
    "500": "#77797E"
    "600": "#636569"
    "700": "#4F5154"
    "800": "#393A3C"
    "900": "#232325"
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
elevation:   # deep shadow + a soft emerald glow for the hero accent
  sm: "0 2px 8px rgba(0, 0, 0, 0.4)"
  md: "0 8px 24px rgba(0, 0, 0, 0.5)"
  lg: "0 20px 50px rgba(0, 0, 0, 0.6)"
  xl: "0 0 40px rgba(22, 223, 156, 0.20)"
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

Obsidian is built to feel expensive. It sits on a cool graphite canvas, sets
headlines in a high-contrast serif, and spends its color budget on jade
emerald — used the way a luxury brand uses a jewel: sparingly, on the details
that matter. A cool steel blue appears as a second note. Depth comes from real
darkness and a soft emerald glow, not from busy gradients.

Use it for luxury goods, fine dining, hospitality, high-end real estate, premium
agencies, and portfolios that want gravitas. The mood is quiet confidence and
craft. Restraint is non-negotiable — a dark premium look fails the instant it
gets cluttered or neon.

## Colors

- **Canvas** is `neutral.900` (`#232325`), a cool near-black with a whisper of
  graphite so it feels considered rather than clinical. Elevated surfaces (cards,
  nav) step up to `neutral.800`.
- **Text** is the light end of the neutral scale: `neutral.100`/`50` for
  headings and body, `neutral.300`/`400` for muted meta. Never pure white on the
  graphite — the slight softness keeps it easy on the eye.
- **Primary (jade emerald)** is the luxury signal. Reserve `primary.500` for
  the primary CTA, a hairline divider, a small icon, or a thin underline on the
  hero word. Emerald text on graphite (`primary.400`/`500`) reads elegant; emerald
  fills should be small.
- **Secondary (cool steel)** is the restrained metal — a badge, a price highlight, a
  second data hue. Emerald + steel on graphite is a considered pairing; keep
  steel rarer than emerald.
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
- Let darkness be the negative space — full-bleed graphite sections with a single
  emerald detail are more premium than a busy grid.
- Thin emerald hairline rules (1px `primary.700`/`800`) segment sections with
  elegance.
- Large, high-quality imagery framed with subtle borders; let photos sit in the
  dark rather than filling every pixel.

## Elevation & Depth

Depth is dramatic but controlled. Cards lift off the canvas with deep, soft
black shadows (`elevation.md`/`lg`); the hero CTA and key accents can carry a
soft emerald glow (`elevation.xl`, `0 0 40px rgba(22,223,156,0.22)`) that reads as
lit rather than shadowed. Use the glow rarely — one glowing element per view.
Elevated surfaces get lighter (`neutral.800`), not darker; light-from-above is
the mental model.

## Shapes

Precise and near-square. `rounded.md` (6px) on buttons and inputs, `rounded.lg`
(8px) on cards. Luxury reads as exactness, so the rounding is subtle — enough to
avoid harshness, not enough to look playful. Full-round is reserved for avatars
and small status dots.

## Components

- **Button** — emerald fill with graphite label (`neutral.900`), subtle tracking,
  small radius. On hover it brightens to `primary.400` and gains the emerald glow;
  active settles to `primary.600`. A secondary/"ghost" button is a 1px emerald
  outline with emerald text on the graphite canvas.
- **Card** — `neutral.800` surface, 1px `neutral.700` border, deep soft shadow.
  On hover the border shifts to `primary.700` emerald and the shadow deepens. Ideal
  for product tiles, testimonials, and pricing.
- **Input** — sits on the darkest `neutral.900` with a `neutral.700` border and
  light text; focus swaps the border to emerald with a faint `primary.900` ring.
  The border carries the state; the ring is a quiet reinforcement.

## Do's and Don'ts

**Do**
- Spend emerald like it is precious — small, deliberate, on the details.
- Keep the canvas cool graphite (`neutral.900`), not pure black.
- Use one glowing element per view for the focal accent.
- Set headlines big in Playfair and give sections room to breathe.

**Don't**
- ❌ Don't flood surfaces with emerald — an emerald-heavy page reads as cheap, not
  luxurious. Emerald is a detail, not a wash.
- ❌ Don't set Playfair as body copy; its high contrast shreds legibility at
  small sizes. Body is Manrope.
- ❌ Don't use pure black (`#000`) or pure white (`#FFF`) — the warm off-values
  are what make it feel crafted.
- ❌ Don't add neon or a third bright accent; it destroys the restrained,
  expensive mood.
- ❌ Don't clutter. Empty dark space is the luxury; fill it and you lose the
  entire effect.
