---
name: Hearth
slug: warm-local-service
description: >-
  A warm, earthy, friendly system for local businesses — cafes, salons,
  dentists, studios. Honey amber and deep teal on soft stone, rounded corners, a
  characterful display, and gentle shadows. Approachable and human,
  never corporate.
aesthetic: [warm, friendly, earthy, approachable, rounded, cozy, organic]
industries: [cafe, restaurant, salon, spa, dentist, wellness, local-service, boutique, bakery]
page_types: [landing, services, menu, booking, about, contact, gallery]
colors:
  primary:   # honey amber — warm and inviting, not terracotta
    "50": "#FCF4E9"
    "100": "#F7E4CA"
    "200": "#EFCB99"
    "300": "#E8B369"
    "400": "#E19C3D"
    "500": "#D48921"
    "600": "#B1721B"
    "700": "#8D5B16"
    "800": "#664210"
    "900": "#3E280A"
  secondary:   # deep teal — natural, grounded counterpoint
    "50": "#E9FCF9"
    "100": "#CAF7F0"
    "200": "#98F0E1"
    "300": "#67E9D3"
    "400": "#3BE3C7"
    "500": "#1FD6B7"
    "600": "#1AB299"
    "700": "#158F7A"
    "800": "#0F6758"
    "900": "#093E35"
  neutral:   # soft stone — earthy but near-neutral, never sand
    "50": "#F3F3F1"
    "100": "#E3E1DE"
    "200": "#C9C6C0"
    "300": "#AFAAA1"
    "400": "#989186"
    "500": "#847D71"
    "600": "#6E685E"
    "700": "#58544B"
    "800": "#3F3C36"
    "900": "#272521"
typography:
  fonts:
    display: "'Bricolage Grotesque', Georgia, sans-serif"
    heading: "'Bricolage Grotesque', Georgia, sans-serif"
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
  sm: "0 2px 6px rgba(39, 37, 33, 0.08)"
  md: "0 6px 18px rgba(39, 37, 33, 0.10)"
  lg: "0 16px 40px rgba(39, 37, 33, 0.12)"
  xl: "0 28px 70px rgba(39, 37, 33, 0.14)"
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
a hospital. It trades cool corporate neutrals for soft stone and amber, softens
every corner, and pairs a characterful display with a rounded body sans. The
result feels like a person, not a company.

The emotional target is welcome. Warm honey amber invites, deep teal reassures, and
generous rounding makes every button feel like it wants to be pressed. Photos of
real people, real food, and real spaces belong here — this system is a frame for
warmth, not a substitute for it.

## Colors

- **Primary (honey amber)** is the warmth. Use `primary.500`/`600` for buttons,
  links, and the one or two moments that should feel inviting. It can also wash a
  soft `primary.50`/`100` section background — unlike a cool accent, a warm one
  can cover more surface without feeling aggressive.
- **Secondary (deep teal)** is the calm. It pairs naturally with amber (they sit
  opposite on the warm/cool axis) for tags, secondary buttons, "open now"
  badges, and botanical accents. An amber + teal duo is the signature combo.
- **Neutral (soft stone)** replaces cold gray everywhere. Text is `neutral.800`/`900`
  (a soft near-black with a hint of warmth, never `#000`), borders are `neutral.200`, and
  surfaces alternate `#FFFFFF` with `neutral.50`. The quiet warmth in the neutral is
  what keeps the whole page cohesive.
- Keep neutrals in the stone family — a starkly cold gray will fight the cozy mood.

## Typography

Bricolage Grotesque sets the display and headings — a warm, characterful
grotesque with just enough quirk to feel handmade rather than institutional. Its
friendly forms read warm at large sizes. Nunito Sans carries body copy with
rounded terminals that echo the rounded UI. Together they feel friendly and
legible without tipping into childish.

- Headlines can be a touch playful — Bricolage Grotesque at weight 600 with an
  italic for an emphasized word works beautifully.
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

Shadows are soft and low (`rgba(39,37,33,…)`), like light through a
window rather than a hard studio spotlight. Cards rest at `elevation.sm` and
lift gently on hover with a small upward translate — a friendly little bounce.
Nothing is sharp; depth here is cozy, not architectural.

## Shapes

Rounding is the signature. Buttons are fully pill-shaped (`rounded.full`), cards
use `rounded.lg` (20px), inputs `rounded.md` (14px). The generous radius is what
makes Hearth feel approachable — never square a primary button here. Full-round
photos and avatars reinforce the human, personal tone.

## Components

- **Button** — honey-amber fill (`primary.500`), deep bold label (`neutral.900`), fully
  pill-shaped. On hover it deepens to `primary.600`, lifts 2px, and grows its soft
  shadow; focus draws a wide 4px `primary.200` ring. Secondary buttons use a teal
  fill (`secondary.700`, white label) or a `primary.100` tint with amber text (`primary.700`).
- **Card** — white fill, hairline `neutral.200` border, `rounded.lg`, soft
  resting shadow. On hover it floats up 3px with a warmer shadow. Great for
  service tiles, menu items, and testimonials with a rounded avatar.
- **Input** — sits on a `neutral.50` fill (slightly inset from white) with a
  `neutral.300` border, focusing to amber with a wide soft ring. The inset
  fill makes forms feel gentle and tactile.

## Do's and Don'ts

**Do**
- Pair amber and teal — it is the signature warm/calm duo.
- Keep every corner rounded and every button a pill.
- Use soft low shadows and stone neutrals throughout.
- Lead with a real, warm photograph — people, food, or the space.

**Don't**
- ❌ Don't introduce a cool gray or a pure-black text color; one cold value
  breaks the entire warm mood.
- ❌ Don't square the corners — sharp edges make Hearth feel clinical, the exact
  opposite of the goal.
- ❌ Don't over-saturate the amber into a hot orange; keep it warm and mellow,
  not alarming.
- ❌ Don't use hard, blurred-black drop shadows; keep them soft and low.
- ❌ Don't cram the layout — a local business page should feel unhurried, with
  room to breathe between sections.
