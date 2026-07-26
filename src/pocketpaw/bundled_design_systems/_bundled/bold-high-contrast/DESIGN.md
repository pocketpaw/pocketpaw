---
name: Monolith
slug: bold-high-contrast
description: >-
  Near-black on near-white, oversized display type, and one electric lime accent
  that does all the shouting. A confident, editorial-brutalist system for brands
  that want to feel loud and certain. Hard-edged, high-contrast, unafraid of
  empty space at scale.
aesthetic: [bold, high-contrast, brutalist, editorial, energetic, monochrome]
industries: [agency, creative-studio, fashion, events, portfolio, media, crypto-web3]
page_types: [landing, portfolio, event, product-launch, manifesto, coming-soon]
colors:
  primary:   # electric lime — the single accent, used with intent
    "50": "#F9FAF2"
    "100": "#F3F8E3"
    "200": "#EAF5C2"
    "300": "#E0F594"
    "400": "#D4F55E"
    "500": "#C8F42A"
    "600": "#B7E60C"
    "700": "#95BB0C"
    "800": "#708C0D"
    "900": "#4B5C0C"
  secondary:   # graphite — stays monochrome; supporting UI only
    "50": "#F6F6F6"
    "100": "#EBECED"
    "200": "#D6D7D9"
    "300": "#B9BCC0"
    "400": "#999CA3"
    "500": "#787D87"
    "600": "#60646C"
    "700": "#484B51"
    "800": "#2B2D31"
    "900": "#151619"
  neutral:   # true grayscale — the near-white canvas and the near-black ink
    "50": "#F6F6F6"
    "100": "#ECECEC"
    "200": "#D7D7D7"
    "300": "#BDBDBD"
    "400": "#9E9E9E"
    "500": "#808080"
    "600": "#666666"
    "700": "#4C4C4C"
    "800": "#2E2E2E"
    "900": "#171717"
typography:
  fonts:
    display: "'Archivo', 'Arial Narrow', sans-serif"
    heading: "'Archivo', system-ui, sans-serif"
    body: "'IBM Plex Sans', system-ui, sans-serif"
    mono: "'Space Mono', ui-monospace, monospace"
  scale:   # display → caption — a dramatic 1.333 ratio for big contrast
    display: { size: "6rem",     weight: 800, line_height: 0.95, tracking: "-0.03em" }
    h1:      { size: "4.209rem", weight: 800, line_height: 1.0,  tracking: "-0.03em" }
    h2:      { size: "3.157rem", weight: 700, line_height: 1.05, tracking: "-0.02em" }
    h3:      { size: "2.369rem", weight: 700, line_height: 1.1,  tracking: "-0.02em" }
    h4:      { size: "1.777rem", weight: 600, line_height: 1.2,  tracking: "-0.01em" }
    body_lg: { size: "1.333rem", weight: 400, line_height: 1.55, tracking: "0" }
    body:    { size: "1rem",     weight: 400, line_height: 1.6,  tracking: "0" }
    body_sm: { size: "0.875rem", weight: 400, line_height: 1.5,  tracking: "0" }
    caption: { size: "0.75rem",  weight: 600, line_height: 1.3,  tracking: "0.08em" }
spacing:   # 4px base, but scaled up — bold systems breathe
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "2rem"
  xl: "3.5rem"
  "2xl": "6rem"
  "3xl": "9rem"
rounded:   # hard edges — sharp is the whole point
  none: "0"
  sm: "0"
  md: "0.125rem"
  lg: "0.25rem"
  xl: "0.25rem"
  full: "9999px"
elevation:   # hard, offset, brutalist — no blur softness
  sm: "2px 2px 0 rgba(23, 22, 25, 1)"
  md: "4px 4px 0 rgba(23, 22, 25, 1)"
  lg: "8px 8px 0 rgba(23, 22, 25, 1)"
  xl: "12px 12px 0 rgba(23, 22, 25, 1)"
components:
  button:
    default: { bg: "neutral.900", fg: "#FFFFFF", radius: "rounded.none", padding: "0.875rem 1.75rem", weight: 700, tracking: "0.02em", shadow: "none" }
    hover:   { bg: "primary.500", fg: "neutral.900", shadow: "elevation.sm", transform: "translate(-2px, -2px)" }
    focus:   { outline: "3px solid primary.500", outline_offset: "2px" }
    active:  { transform: "translate(0, 0)", shadow: "none" }
  card:
    default: { bg: "#FFFFFF", border: "2px solid neutral.900", radius: "rounded.none", padding: "2rem", shadow: "elevation.md" }
    hover:   { transform: "translate(-2px, -2px)", shadow: "elevation.lg" }
  input:
    default: { bg: "#FFFFFF", border: "2px solid neutral.900", radius: "rounded.none", padding: "0.875rem 1rem", fg: "neutral.900" }
    hover:   { border: "2px solid neutral.700" }
    focus:   { border: "2px solid primary.600", outline: "none", shadow: "elevation.sm" }
---

# Monolith — bold, high-contrast

## Overview

Monolith is built to be remembered from across the room. It commits to a
near-black ink and a near-white page, then lets a single electric lime do every
bit of the emphasis. Type is oversized and set tight; layouts leave hard,
confident gaps; edges are sharp and shadows are solid offset blocks, not soft
glows.

Reach for it when the brand's job is to feel bold and self-assured — an agency,
a creative studio, a product launch, a manifesto page. It rewards restraint in
color with fearlessness in scale. One giant headline plus one lime button beats
ten timid gradients.

## Colors

- **Neutral (grayscale)** is the system. `neutral.900` (`#171717`) is the ink;
  `#FFFFFF` and `neutral.50` are the page. There are no colored neutrals here —
  the contrast is the identity.
- **Primary (electric lime)** is a spotlight. It belongs on the single most
  important action, a highlighted word in a headline, or one full-bleed accent
  block per page. Because the rest of the page is monochrome, even a small lime
  element becomes the loudest thing in the room. `primary.500`/`600` on ink;
  `primary.900` if you need lime as text on white (it stays legible).
- **Secondary (graphite)** stays inside the monochrome family — it is for
  secondary buttons, meta text, and borders that need to sit half a step off
  pure black. Do not treat it as a second color.
- Contrast discipline: ink on white, white on ink, ink on lime. Never lime text
  on white at body sizes — it fails legibility.

## Typography

Archivo — a grotesque with a tall x-height and an expanded personality at
weight 800 — carries the display and headings. It is engineered to look
enormous. Set display and H1 tight (`-0.03em`) and heavy; let them fill the
column. IBM Plex Sans handles body copy with a neutral, engineered calm that
keeps the loud headlines from becoming exhausting. Space Mono adds a technical,
slightly retro voice for labels, timestamps, and eyebrow text (uppercase,
`0.08em` tracking).

- Display type is the hero of the page. Go big — 5rem and up on desktop.
- Uppercase eyebrows in Space Mono set the tone above a headline.
- Keep body measure tight and the line-height generous so the mono/grotesque mix
  stays readable.

## Layout

- Asymmetry over symmetry. Push a headline hard left, let it run wide, and leave
  the right column empty on purpose.
- A visible structural grid is welcome — 1px or 2px ink rules that segment the
  page like a broadsheet.
- Generous section gaps (`spacing.2xl`/`3xl`). Empty space at scale reads as
  confidence, not as an unfinished page.
- Full-bleed accent blocks (one lime, one ink) can break the grid for emphasis.

## Elevation & Depth

Depth is graphic, not atmospheric. Shadows are solid ink offsets
(`Npx Npx 0`), so a card looks like it was printed and pasted rather than
floating. On hover, elements translate up-left into their own shadow, which then
grows — a tactile, physical response. Never soften these into blurred drop
shadows; the hard edge is the aesthetic.

## Shapes

Sharp. `rounded.none` on buttons, cards, and inputs; at most a hairline
`rounded.md` (2px) where a truly square corner looks like a rendering bug.
Full-round is allowed only for tags and status dots. Mixing soft rounding into
this system instantly dilutes the brutalist confidence.

## Components

- **Button** — ink fill, white uppercase-ish label, square corners, no shadow at
  rest. On hover it flips to lime with ink text, translates `-2px,-2px`, and
  drops a hard 2px offset shadow — it visibly pops off the page. Active returns
  to flat.
- **Card** — white fill, 2px solid ink border, square, 4px offset shadow. On
  hover it lifts into an 8px shadow. The border and the block-shadow do all the
  separation; there is no gradient or blur.
- **Input** — white fill, 2px ink border, square. Focus swaps the border to lime
  and adds a small hard shadow. The heavy border is legible even where focus
  rings are suppressed.

## Do's and Don'ts

**Do**
- Go oversized on the hero headline — this system is designed for it.
- Keep lime to a single job per view: the main CTA or one accent block.
- Use solid offset shadows and hard edges consistently.
- Set eyebrow labels in uppercase Space Mono with wide tracking.

**Don't**
- ❌ Don't add a second accent color. The moment there are two, the lime stops
  being loud and the page becomes noise.
- ❌ Don't round the corners or blur the shadows — that turns Monolith into a
  generic soft-SaaS look and throws away its only differentiator.
- ❌ Don't set lime as body text on white; it is an accent surface, not a text
  color at small sizes.
- ❌ Don't center a wall of text. Push it left; asymmetry is load-bearing here.
- ❌ Don't shrink the display type to "fit" — if it doesn't fit, cut the words,
  not the size.
