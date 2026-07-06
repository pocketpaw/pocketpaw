---
name: Quill
slug: minimal-editorial
description: >-
  A minimal, editorial system built on whitespace: a warm paper canvas, a
  beautiful serif display over a clean sans body, hairline rules instead of
  shadows, and one restrained oxblood accent. For writers, studios, and brands
  that want a magazine's quiet authority.
aesthetic: [minimal, editorial, elegant, refined, whitespace, magazine, timeless]
industries: [publishing, writer, consultancy, studio, nonprofit, architecture, personal-brand, journal]
page_types: [landing, essay, about, portfolio, newsletter, contact]
colors:
  primary:   # editorial oxblood / ink-red — the one restrained accent
    "50": "#F9F4F3"
    "100": "#F5E7E6"
    "200": "#EECDC9"
    "300": "#E7A9A2"
    "400": "#DF7F74"
    "500": "#D75547"
    "600": "#C73B2C"
    "700": "#A23225"
    "800": "#7A281F"
    "900": "#511E18"
  secondary:   # muted ink blue — quiet links and meta
    "50": "#F5F6F7"
    "100": "#EAECF0"
    "200": "#D3D8E3"
    "300": "#B5BED3"
    "400": "#92A0C1"
    "500": "#6F82AE"
    "600": "#576C9B"
    "700": "#48597F"
    "800": "#394560"
    "900": "#282F41"
  neutral:   # warm paper → ink — the whole system lives here
    "50": "#F7F6F6"
    "100": "#EEEEEC"
    "200": "#DEDCD9"
    "300": "#C9C6C0"
    "400": "#B0ACA3"
    "500": "#989285"
    "600": "#847D6E"
    "700": "#6D675A"
    "800": "#544F45"
    "900": "#3A362F"
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
  md: "0 1px 0 rgba(58, 54, 47, 0.10)"
  lg: "0 12px 40px rgba(58, 54, 47, 0.08)"
  xl: "0 24px 60px rgba(58, 54, 47, 0.10)"
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
over a clean sans body on a warm paper canvas, separates content with hairline
rules instead of boxes and shadows, and spends its single accent — a restrained
oxblood — only where a link or a pull-quote asks for it. The effect is a
magazine's quiet authority: it looks read, not designed.

Reach for it when the words are the product — a writer, a consultancy, a studio,
an essay, a considered personal brand. It is the opposite of a busy landing
page. Its confidence comes from what it leaves out.

## Colors

- **Neutral (warm paper → ink)** is nearly the whole system. The canvas is
  `neutral.50` (`#F7F6F6`), a warm off-white; text is `neutral.900` (`#3A362F`),
  a soft ink that reads as black without the harshness. Meta and captions use
  `neutral.500`/`600`. Rules and borders are `neutral.200`/`300`.
- **Primary (oxblood)** is the one editorial accent — for links, a pull-quote
  bar, a drop-cap, a small "read more" arrow. It should appear a handful of times
  per page, never as a fill across a large surface. `primary.600`/`700` for
  links on paper.
- **Secondary (ink blue)** is even quieter — an alternative link color, a
  footnote, a subtle tag. It exists so not every accent has to be red.
- The palette is deliberately close-valued and warm. The drama comes from type
  and space, not from color.

## Typography

Newsreader sets the display and headings — a serif designed for on-screen
reading with a warm, literary voice. Set the display at a light weight (400) and
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
  shadow. On hover it shifts to oxblood (`primary.700`); focus is a 1px outline
  offset from the label. A "text button" — oxblood label with an underline on
  hover — is the more common editorial affordance.
- **Card** — paper fill, 1px `neutral.200` border, minimal radius, no shadow. On
  hover the border darkens to `neutral.400`. Often you need no card at all — a
  rule and spacing do the same job with less furniture.
- **Input** — underlined, not boxed: transparent fill, a single `neutral.400`
  bottom border that thickens to oxblood on focus. It reads like a form in a
  printed booklet.

## Do's and Don'ts

**Do**
- Let whitespace do the heavy lifting — more space between sections, not less.
- Set headlines in Newsreader, large and light; keep body measure narrow.
- Use hairline rules to segment content instead of boxes and shadows.
- Keep the oxblood accent rare — links and one or two emphases per page.

**Don't**
- ❌ Don't box everything in cards with shadows — that is the app look this system
  rejects. Reach for a rule and space first.
- ❌ Don't fill large surfaces with the oxblood; it is an ink accent, not a brand
  color wash.
- ❌ Don't round the corners heavily or the editorial precision collapses into a
  generic SaaS look.
- ❌ Don't run body text full-width; a magazine column (60–70ch) is the whole
  point of readability here.
- ❌ Don't crowd the page. If it feels too empty, it is probably right — resist
  filling the space.
