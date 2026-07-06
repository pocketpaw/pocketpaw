---
name: Hearth
slug: warm-local-service
description: >-
  A warm, earthy, friendly system for local businesses — cafes, salons,
  dentists, studios. Terracotta and sage on soft sand, rounded corners, a
  characterful soft-serif display, and gentle shadows. Approachable and human,
  never corporate.
aesthetic: [warm, friendly, earthy, approachable, rounded, cozy, organic]
industries: [cafe, restaurant, salon, spa, dentist, wellness, local-service, boutique, bakery]
page_types: [landing, services, menu, booking, about, contact, gallery]
colors:
  primary:   # terracotta clay — warm and inviting
    "50": "#F9F5F3"
    "100": "#F4EAE6"
    "200": "#EDD4CA"
    "300": "#E6B6A3"
    "400": "#DD9376"
    "500": "#D47149"
    "600": "#C4592E"
    "700": "#A04927"
    "800": "#783A21"
    "900": "#502819"
  secondary:   # sage — calm, natural counterpoint
    "50": "#F5F8F5"
    "100": "#EBF1EA"
    "200": "#D5E4D3"
    "300": "#B8D5B4"
    "400": "#97C390"
    "500": "#76B06D"
    "600": "#5E9D55"
    "700": "#4E8146"
    "800": "#3D6237"
    "900": "#2A4227"
  neutral:   # warm sand — text and surfaces, never cold gray
    "50": "#F7F6F5"
    "100": "#EFEDEB"
    "200": "#E0DCD7"
    "300": "#CCC5BC"
    "400": "#B6AB9D"
    "500": "#A0917E"
    "600": "#8D7C66"
    "700": "#746653"
    "800": "#5A4E3F"
    "900": "#3E362B"
typography:
  fonts:
    display: "'Fraunces', Georgia, serif"
    heading: "'Fraunces', Georgia, serif"
    body: "'Nunito Sans', system-ui, sans-serif"
    mono: "'IBM Plex Mono', ui-monospace, monospace"
  scale:   # display → caption — gentle 1.2 ratio, nothing jarring
    display: { size: "3.583rem", weight: 600, line_height: 1.1,  tracking: "-0.01em" }
    h1:      { size: "2.986rem", weight: 600, line_height: 1.15, tracking: "-0.01em" }
    h2:      { size: "2.488rem", weight: 600, line_height: 1.2,  tracking: "0" }
    h3:      { size: "2.074rem", weight: 500, line_height: 1.25, tracking: "0" }
    h4:      { size: "1.728rem", weight: 500, line_height: 1.3,  tracking: "0" }
    body_lg: { size: "1.2rem",   weight: 400, line_height: 1.7,  tracking: "0" }
    body:    { size: "1rem",     weight: 400, line_height: 1.7,  tracking: "0" }
    body_sm: { size: "0.875rem", weight: 400, line_height: 1.55, tracking: "0" }
    caption: { size: "0.8rem",   weight: 600, line_height: 1.4,  tracking: "0.01em" }
spacing:   # 4px base, comfortable rhythm
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "1.75rem"
  xl: "2.75rem"
  "2xl": "4.5rem"
  "3xl": "7rem"
rounded:   # soft and friendly — this is the signature
  none: "0"
  sm: "0.5rem"
  md: "0.875rem"
  lg: "1.25rem"
  xl: "2rem"
  full: "9999px"
elevation:   # soft, warm-tinted — like afternoon light
  sm: "0 2px 6px rgba(90, 78, 63, 0.08)"
  md: "0 6px 18px rgba(90, 78, 63, 0.10)"
  lg: "0 16px 40px rgba(90, 78, 63, 0.12)"
  xl: "0 28px 70px rgba(90, 78, 63, 0.14)"
components:
  button:
    default: { bg: "primary.500", fg: "#FFFFFF", radius: "rounded.full", padding: "0.75rem 1.75rem", weight: 700, shadow: "elevation.sm" }
    hover:   { bg: "primary.600", shadow: "elevation.md", transform: "translateY(-2px)" }
    focus:   { ring: "4px", ring_color: "primary.200", outline: "none" }
    active:  { bg: "primary.700", transform: "translateY(0)" }
  card:
    default: { bg: "#FFFFFF", border: "1px solid neutral.200", radius: "rounded.lg", padding: "1.75rem", shadow: "elevation.sm" }
    hover:   { shadow: "elevation.md", transform: "translateY(-3px)" }
  input:
    default: { bg: "neutral.50", border: "1px solid neutral.300", radius: "rounded.md", padding: "0.75rem 1rem", fg: "neutral.900" }
    hover:   { border: "1px solid neutral.400" }
    focus:   { border: "1px solid primary.500", ring: "4px", ring_color: "primary.100", outline: "none" }
---

# Hearth — warm local service

## Overview

Hearth is for the business around the corner — the cafe that knows your order,
the salon you book by name, the dentist who does not want the site to feel like
a hospital. It trades cool corporate neutrals for warm sand and clay, softens
every corner, and pairs a friendly soft-serif with a rounded body sans. The
result feels like a person, not a company.

The emotional target is welcome. Warm terracotta invites, sage reassures, and
generous rounding makes every button feel like it wants to be pressed. Photos of
real people, real food, and real spaces belong here — this system is a frame for
warmth, not a substitute for it.

## Colors

- **Primary (terracotta)** is the warmth. Use `primary.500`/`600` for buttons,
  links, and the one or two moments that should feel inviting. It can also wash a
  soft `primary.50`/`100` section background — unlike a cool accent, a warm one
  can cover more surface without feeling aggressive.
- **Secondary (sage)** is the calm. It pairs naturally with terracotta (they sit
  opposite on the warm/cool axis) for tags, secondary buttons, "open now"
  badges, and botanical accents. A terracotta + sage duo is the signature combo.
- **Neutral (warm sand)** replaces gray everywhere. Text is `neutral.800`/`900`
  (a soft warm brown-black, never `#000`), borders are `neutral.200`, and
  surfaces alternate `#FFFFFF` with `neutral.50`. The warmth in the neutral is
  what keeps the whole page cohesive.
- Avoid any true cool gray — a single cold neutral will look out of place and
  break the cozy mood instantly.

## Typography

Fraunces sets the display and headings — a soft, old-style serif with optical
sizing and just enough wobble to feel handmade rather than institutional. Its
gentle contrast reads warm at large sizes. Nunito Sans carries body copy with
rounded terminals that echo the rounded UI. Together they feel friendly and
legible without tipping into childish.

- Headlines can be a touch playful — Fraunces at weight 600 with a soft italic
  for an emphasized word works beautifully.
- Body line-height runs generous (1.7) so menus, service lists, and hours are
  easy to scan.
- Keep the type scale gentle (1.2 ratio) — no dramatic jumps. This is a calm
  system.

## Layout

- Comfortable, centered, and unhurried. A local-service landing can absolutely
  center its hero — the warmth carries it where a SaaS system would look
  templated.
- Rounded photo cards, soft-cornered service tiles, and pill-shaped tags.
- `spacing.2xl`/`3xl` between sections; nothing feels cramped.
- Two- and three-up card grids for services and menu items; a single warm hero
  photo up top.

## Elevation & Depth

Shadows are soft and warm-tinted (`rgba(90,78,63,…)`), like light through a
window rather than a hard studio spotlight. Cards rest at `elevation.sm` and
lift gently on hover with a small upward translate — a friendly little bounce.
Nothing is sharp; depth here is cozy, not architectural.

## Shapes

Rounding is the signature. Buttons are fully pill-shaped (`rounded.full`), cards
use `rounded.lg` (20px), inputs `rounded.md` (14px). The generous radius is what
makes Hearth feel approachable — never square a primary button here. Full-round
photos and avatars reinforce the human, personal tone.

## Components

- **Button** — terracotta fill, white bold label, fully pill-shaped. On hover it
  deepens to `primary.600`, lifts 2px, and grows its soft shadow; focus draws a
  wide 4px `primary.200` ring. Secondary buttons use a sage fill or a
  `primary.100` tint with terracotta text.
- **Card** — white fill, hairline `neutral.200` border, `rounded.lg`, soft
  resting shadow. On hover it floats up 3px with a warmer shadow. Great for
  service tiles, menu items, and testimonials with a rounded avatar.
- **Input** — sits on a `neutral.50` fill (slightly inset from white) with a
  `neutral.300` border, focusing to terracotta with a wide soft ring. The inset
  fill makes forms feel gentle and tactile.

## Do's and Don'ts

**Do**
- Pair terracotta and sage — it is the signature warm/calm duo.
- Keep every corner rounded and every button a pill.
- Use warm-tinted shadows and warm neutrals throughout.
- Lead with a real, warm photograph — people, food, or the space.

**Don't**
- ❌ Don't introduce a cool gray or a pure-black text color; one cold value
  breaks the entire warm mood.
- ❌ Don't square the corners — sharp edges make Hearth feel clinical, the exact
  opposite of the goal.
- ❌ Don't over-saturate the terracotta into a hot red; keep it earthy and clay,
  not alarming.
- ❌ Don't use hard, blurred-black drop shadows; keep them soft and warm.
- ❌ Don't cram the layout — a local business page should feel unhurried, with
  room to breathe between sections.
