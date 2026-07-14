---
name: Northwind
slug: clean-saas
description: >-
  A calm, cool-neutral SaaS system with one confident cobalt-blue accent — the
  trust-first look of a modern product landing page. Generous whitespace,
  restrained shadows, precise type. Inspired by the clarity of Linear/Stripe-era
  product design without copying any brand's tokens.
aesthetic: [clean, modern, professional, trustworthy, minimal, cool]
industries: [saas, b2b-software, fintech, developer-tools, startup, analytics]
page_types: [landing, pricing, product, docs, changelog, waitlist]
colors:
  primary:   # cobalt — the single confident accent, no violet
    "50": "#E8EEFD"
    "100": "#C7D7FA"
    "200": "#94B2F5"
    "300": "#608DF0"
    "400": "#316CEC"
    "500": "#1554E0"
    "600": "#1146BB"
    "700": "#0E3895"
    "800": "#0A286B"
    "900": "#061941"
  secondary:   # supportive teal — used sparingly for a second data hue
    "50": "#E9F9FB"
    "100": "#CAF1F6"
    "200": "#9AE4EE"
    "300": "#6AD7E6"
    "400": "#3ECBDF"
    "500": "#23BCD2"
    "600": "#1D9CAF"
    "700": "#177D8C"
    "800": "#115A65"
    "900": "#0A373D"
  neutral:   # cool slate — text, surfaces, borders
    "50": "#F1F2F3"
    "100": "#DEE0E3"
    "200": "#A0A7B0"
    "300": "#A0A7B0"
    "400": "#848D99"
    "500": "#6F7886"
    "600": "#5D646F"
    "700": "#4A5059"
    "800": "#353940"
    "900": "#202327"
typography:
  fonts:
    display: "'Plus Jakarta Sans', system-ui, sans-serif"
    heading: "'Plus Jakarta Sans', system-ui, sans-serif"
    body: "'Inter', system-ui, sans-serif"
    mono: "'JetBrains Mono', ui-monospace, monospace"
  scale:   # display → caption
    display: { size: "3.815rem", weight: 700, line_height: 1.05, tracking: "-0.02em" }
    h1:      { size: "3.052rem", weight: 700, line_height: 1.1,  tracking: "-0.02em" }
    h2:      { size: "2.441rem", weight: 600, line_height: 1.15, tracking: "-0.015em" }
    h3:      { size: "1.953rem", weight: 600, line_height: 1.2,  tracking: "-0.01em" }
    h4:      { size: "1.563rem", weight: 600, line_height: 1.3,  tracking: "-0.005em" }
    body_lg: { size: "1.25rem",  weight: 400, line_height: 1.6,  tracking: "0" }
    body:    { size: "1rem",     weight: 400, line_height: 1.65, tracking: "0" }
    body_sm: { size: "0.875rem", weight: 400, line_height: 1.5,  tracking: "0" }
    caption: { size: "0.75rem",  weight: 500, line_height: 1.4,  tracking: "0.02em" }
spacing:   # 4px base rhythm
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "1.5rem"
  xl: "2.5rem"
  "2xl": "4rem"
  "3xl": "6.5rem"
rounded:
  none: "0"
  sm: "0.375rem"
  md: "0.625rem"
  lg: "0.875rem"
  xl: "1.25rem"
  full: "9999px"
elevation:   # soft, low-spread — depth without drama
  sm: "0 1px 2px rgba(46, 50, 59, 0.06)"
  md: "0 4px 12px rgba(46, 50, 59, 0.08)"
  lg: "0 12px 32px rgba(46, 50, 59, 0.10)"
  xl: "0 24px 64px rgba(46, 50, 59, 0.12)"
components:
  button:
    default: { bg: "primary.500", fg: "#FFFFFF", radius: "rounded.md", padding: "0.625rem 1.25rem", weight: 600, shadow: "elevation.sm" }
    hover:   { bg: "primary.600", shadow: "elevation.md", transform: "translateY(-1px)" }
    focus:   { ring: "3px", ring_color: "primary.200", outline: "none" }
    active:  { bg: "primary.700", transform: "translateY(0)" }
  card:
    default: { bg: "#FFFFFF", border: "1px solid neutral.200", radius: "rounded.lg", padding: "1.5rem", shadow: "elevation.sm" }
    hover:   { border: "1px solid neutral.300", shadow: "elevation.md" }
  input:
    default: { bg: "#FFFFFF", border: "1px solid neutral.300", radius: "rounded.md", padding: "0.625rem 0.875rem", fg: "neutral.900" }
    hover:   { border: "1px solid neutral.400" }
    focus:   { border: "1px solid primary.500", ring: "3px", ring_color: "primary.100", outline: "none" }
---

# Northwind — clean SaaS

## Overview

Northwind is the look of software that expects to be trusted before it is
understood. It leans on cool slate neutrals, deliberate whitespace, and a single
cobalt accent that appears only where it earns attention: the primary call to
action, an active nav item, a focused field. Everything else recedes so the
product screenshot and the value proposition carry the page.

Use it for SaaS, B2B, fintech, and developer tools — anywhere the reader is
evaluating, not browsing. The mood is composed and confident. Nothing shouts;
the restraint is the message.

## Colors

- **Primary (cobalt)** is the accent, not the background. Reserve `primary.500`
  and `primary.600` for interactive emphasis — buttons, links, focus rings,
  selected states. On a landing page it should cover well under 10% of the
  pixels. That scarcity is what makes it read as confident rather than loud.
- **Secondary (teal)** is a supporting hue for a second data series, an
  informational badge, or a subtle gradient partner to cobalt. Never let it
  compete with primary for the CTA.
- **Neutral (cool slate)** does the real work. `neutral.900` for headings,
  `neutral.600`–`700` for body copy, `neutral.200`–`300` for borders and
  dividers, `neutral.50`–`100` for section-alternating surfaces. Pure white
  (`#FFFFFF`) is the base canvas; the slate neutrals sit on top of it.
- Body text should be `neutral.700` on white for a softer, more premium read
  than pure black. Headings step up to `neutral.900`.

## Typography

Plus Jakarta Sans sets every heading — a geometric humanist sans with enough
character in its terminals to feel designed, tightened with negative tracking at
display sizes so large headings hold together. Inter carries body copy at a
comfortable 1.65 line-height. The scale is a 1.25 (major third) ratio, which
gives crisp jumps between levels without the theatrical contrast of an editorial
system.

- Display and H1 are for the hero only — one per page.
- Keep body measure to 60–75 characters. Long marketing paragraphs on a full
  container width are the fastest way to look unfinished.
- JetBrains Mono appears only in code snippets, API keys, and metric readouts.

## Layout

- A 12-column grid, 1200px max content width, gutters at `spacing.lg`.
- Vertical rhythm keys off the spacing scale: `spacing.3xl` between major
  sections, `spacing.xl` between a heading and its content, `spacing.md`
  inside components.
- Prefer a clear single-column reading flow for hero and feature copy; break to
  two or three columns only for feature grids, logo walls, and pricing tables.
- Whitespace is a feature. When in doubt, add one step more vertical space, not
  less.

## Elevation & Depth

Depth is implied, never dramatized. Shadows are low-spread and tinted with the
slate hue (`rgba(46,50,59,…)`) rather than pure black, so they sit naturally on
cool surfaces. Cards rest at `elevation.sm` and lift to `elevation.md` on hover.
Modals and popovers use `elevation.lg`/`xl`. Borders carry most of the
separation work; shadows only reinforce interactive lift.

## Shapes

Rounded but not soft — `rounded.md` (10px) on buttons and inputs, `rounded.lg`
(14px) on cards. Full-round (`rounded.full`) is reserved for avatars, pills, and
tags. Consistency matters more than the exact radius: pick one radius per
component type and never mix three radii in a single card.

## Components

- **Button** — solid `primary.500`, white label, `weight 600`. On hover it
  deepens to `primary.600` and lifts 1px with a soft shadow; focus draws a 3px
  `primary.200` ring; active settles to `primary.700` at rest position.
  Secondary buttons are `neutral.900` text on a `neutral.100` fill with no
  shadow.
- **Card** — white fill, 1px `neutral.200` border, `rounded.lg`, resting
  `elevation.sm`. On hover the border warms to `neutral.300` and the shadow
  steps up. The border is the primary edge; the shadow is secondary.
- **Input** — white fill, `neutral.300` border, focus swaps the border to
  `primary.500` plus a 3px `primary.100` ring. Never rely on the ring alone —
  the border color change carries the state for anyone who dims focus rings.

## Do's and Don'ts

**Do**
- Let the cobalt be rare. One primary action per view.
- Use `neutral.700` for body copy on white — it reads more premium than black.
- Alternate `#FFFFFF` and `neutral.50` section backgrounds to segment a long page.
- Keep shadows tinted with the slate hue for cohesion.

**Don't**
- ❌ Don't paint large surfaces in `primary.500` — cobalt is an accent, not a
  brand wash. A full cobalt hero reads as a template, not a product.
- ❌ Don't center every section. Center the hero; left-align dense feature copy
  so the eye has an anchor. Centered-everything is the canonical "AI slop" tell.
- ❌ Don't stack teal and cobalt at equal weight — the teal is support, not a
  co-lead.
- ❌ Don't use pure-black shadows or pure-black text; both feel harsh against the
  cool neutrals.
- ❌ Don't mix more than two radii on one component.
