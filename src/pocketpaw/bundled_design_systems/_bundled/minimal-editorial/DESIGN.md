---
name: Quill
slug: minimal-editorial
description: >-
  A minimal, editorial system built on whitespace: a soft near-neutral canvas, a
  beautiful serif display over a clean sans body, hairline rules instead of
  shadows, and one restrained garnet accent. For writers, studios, and brands
  that want a magazine's quiet authority.
aesthetic: [minimal, editorial, elegant, refined, whitespace, magazine, timeless]
industries: [publishing, writer, consultancy, studio, nonprofit, architecture, personal-brand, journal]
page_types: [landing, essay, about, portfolio, newsletter, contact]
colors:
  primary:   # garnet crimson — the one restrained accent
    "50": "#FBE9ED"
    "100": "#F6CBD4"
    "200": "#EE9BAC"
    "300": "#E66B84"
    "400": "#DE3F60"
    "500": "#D12447"
    "600": "#AE1E3B"
    "700": "#8B1830"
    "800": "#641122"
    "900": "#3D0A15"
  secondary:   # muted ink blue — quiet links and meta
    "50": "#F0F2F5"
    "100": "#DBDFE6"
    "200": "#B9C1D0"
    "300": "#98A4B9"
    "400": "#7989A4"
    "500": "#637492"
    "600": "#536179"
    "700": "#424E61"
    "800": "#2F3846"
    "900": "#1D222B"
  neutral:   # soft near-neutral → ink — the whole system lives here
    "50": "#F3F2F2"
    "100": "#E1E1E0"
    "200": "#C6C5C3"
    "300": "#AAA9A6"
    "400": "#91908C"
    "500": "#7D7B77"
    "600": "#686764"
    "700": "#545250"
    "800": "#3C3B39"
    "900": "#252423"
typography:
  fonts:
    display: "'Newsreader', Georgia, serif"
    heading: "'Newsreader', Georgia, serif"
    body: "'IBM Plex Sans', system-ui, sans-serif"
    mono: "'IBM Plex Mono', ui-monospace, monospace"
  scale:   # display → caption — editorial 1.25 ratio, serif display
    display: { size: "4.768rem", weight: 400, line_height: 1.08, tracking: "-0.015em" }
    h1:      { size: "3.815rem", weight: 500, line_height: 1.12, tracking: "-0.01em" }
    h2:      { size: "2.827rem", weight: 500, line_height: 1.18, tracking: "-0.005em" }
    h3:      { size: "1.999rem", weight: 500, line_height: 1.25, tracking: "0" }
    h4:      { size: "1.414rem", weight: 600, line_height: 1.35, tracking: "0" }
    body_lg: { size: "1.266rem", weight: 400, line_height: 1.75, tracking: "0" }
    body:    { size: "1.062rem", weight: 400, line_height: 1.8,  tracking: "0" }
    body_sm: { size: "0.889rem", weight: 400, line_height: 1.6,  tracking: "0" }
    caption: { size: "0.79rem",  weight: 500, line_height: 1.4,  tracking: "0.06em" }
spacing:   # 4px base, very generous — whitespace is the system
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "2rem"
  xl: "3.5rem"
  "2xl": "6rem"
  "3xl": "10rem"
rounded:   # nearly none — editorial precision
  none: "0"
  sm: "0.125rem"
  md: "0.125rem"
  lg: "0.25rem"
  xl: "0.25rem"
  full: "9999px"
elevation:   # hairlines, not shadows — depth by rule, not by lift
  sm: "none"
  md: "0 1px 0 rgba(37, 36, 35, 0.10)"
  lg: "0 12px 40px rgba(37, 36, 35, 0.08)"
  xl: "0 24px 60px rgba(37, 36, 35, 0.10)"
components:
  button:
    default: { bg: "neutral.900", fg: "neutral.50", radius: "rounded.sm", padding: "0.75rem 1.5rem", weight: 500, tracking: "0.01em", shadow: "none" }
    hover:   { bg: "primary.700" }
    focus:   { outline: "1px solid neutral.900", outline_offset: "3px" }
    active:  { bg: "primary.800" }
  card:
    default: { bg: "neutral.50", border: "1px solid neutral.200", radius: "rounded.sm", padding: "2rem", shadow: "none" }
    hover:   { border: "1px solid neutral.400" }
  input:
    default: { bg: "transparent", border: "0", border_bottom: "1px solid neutral.400", radius: "rounded.none", padding: "0.5rem 0", fg: "neutral.900" }
    hover:   { border_bottom: "1px solid neutral.600" }
    focus:   { border_bottom: "2px solid primary.600", outline: "none" }
---

# Quill — minimal editorial

## Overview

Quill is a system made of whitespace and type. It puts a beautiful serif display
over a clean sans body on a soft near-neutral canvas, separates content with hairline
rules instead of boxes and shadows, and spends its single accent — a restrained
garnet — only where a link or a pull-quote asks for it. The effect is a
magazine's quiet authority: it looks read, not designed.

Reach for it when the words are the product — a writer, a consultancy, a studio,
an essay, a considered personal brand. It is the opposite of a busy landing
page. Its confidence comes from what it leaves out.

## Colors

- **Neutral (near-neutral → ink)** is nearly the whole system. The canvas is
  `neutral.50` (`#F3F2F2`), a soft off-white; text is `neutral.900` (`#252423`),
  a soft ink that reads as black without the harshness. Meta and captions use
  `neutral.500`/`600`. Rules and borders are `neutral.200`/`300`.
- **Primary (garnet)** is the one editorial accent — for links, a pull-quote
  bar, a drop-cap, a small "read more" arrow. It should appear a handful of times
  per page, never as a fill across a large surface. `primary.600`/`700` for
  links on paper.
- **Secondary (ink blue)** is even quieter — an alternative link color, a
  footnote, a subtle tag. It exists so not every accent has to be red.
- The palette is deliberately close-valued and restrained. The drama comes from
  type and space, not from color.

## Typography

Newsreader sets the display and headings — a serif designed for on-screen
reading with a quiet, literary voice. Set the display at a light weight (400) and
large size so it reads like a magazine masthead. IBM Plex Sans carries body copy
with an engineered neutrality that lets the serif headings sing. Body runs at a
long 1.8 line-height with a comfortable measure — this system expects real
reading.

- Display in Newsreader, large and light, is the centerpiece; an italic on one
  word adds editorial warmth.
- Keep the body measure to a strict 60–70 characters — a magazine column, not a
  full-width block.
- Wide-tracked small caps (`0.06em`) in IBM Plex Sans make refined eyebrows and
  section labels.

## Layout

- Whitespace is the layout. `spacing.3xl` (10rem) between sections is normal, not
  excessive. Let sections breathe until they feel almost too airy, then stop.
- A narrow centered measure for reading content; a wider grid only for a portfolio
  or index.
- Hairline `neutral.200` rules — full-width or short — segment the page like a
  printed article. A single horizontal rule can replace an entire card.
- Asymmetry via the type: a large left-aligned headline against a small
  right-aligned label reads editorial.

## Elevation & Depth

There are almost no shadows. Depth is created by hairline rules and generous
space, the way a print page has no drop shadows yet clear hierarchy. `elevation.sm`
is literally `none`; `elevation.md` is a 1px bottom rule; only overlays (a menu,
a modal) earn the soft `elevation.lg`/`xl`. If you find yourself adding a box
shadow to a section, add a rule and more space instead.

## Shapes

Effectively square. `rounded.sm` (2px) at most on buttons and cards — just enough
to avoid a mechanical corner — and `rounded.none` on inputs, which are underlined
rather than boxed. The editorial precision depends on straight edges; heavy
rounding would make Quill look like a generic app.

## Components

- **Button** — solid ink (`neutral.900`) with paper text, minimal radius, no
  shadow. On hover it shifts to garnet (`primary.700`); focus is a 1px outline
  offset from the label. A "text button" — garnet label with an underline on
  hover — is the more common editorial affordance.
- **Card** — paper fill, 1px `neutral.200` border, minimal radius, no shadow. On
  hover the border darkens to `neutral.400`. Often you need no card at all — a
  rule and spacing do the same job with less furniture.
- **Input** — underlined, not boxed: transparent fill, a single `neutral.400`
  bottom border that thickens to garnet on focus. It reads like a form in a
  printed booklet.

## Do's and Don'ts

**Do**
- Let whitespace do the heavy lifting — more space between sections, not less.
- Set headlines in Newsreader, large and light; keep body measure narrow.
- Use hairline rules to segment content instead of boxes and shadows.
- Keep the garnet accent rare — links and one or two emphases per page.

**Don't**
- ❌ Don't box everything in cards with shadows — that is the app look this system
  rejects. Reach for a rule and space first.
- ❌ Don't fill large surfaces with the garnet; it is an ink accent, not a brand
  color wash.
- ❌ Don't round the corners heavily or the editorial precision collapses into a
  generic SaaS look.
- ❌ Don't run body text full-width; a magazine column (60–70ch) is the whole
  point of readability here.
- ❌ Don't crowd the page. If it feels too empty, it is probably right — resist
  filling the space.
