---
name: sites-craft
description: |
  The CRAFT MECHANICS for a Paw Site — how to actually build the type scale, the
  colour ramp, the spacing rhythm and the surface treatment, with exact values.
  Invoke it WHILE AUTHORING sections, at the moment you lock tokens and again
  whenever you set a font size, pick a colour step, space a group, round a
  corner, or style a state. `pocketpaw-design-taste` chooses WHICH font, WHICH
  palette family and WHICH layout composition; this decides HOW those are
  constructed so they read as one system instead of a pile of one-off values.
  It answers "what line-height does a heading take", "how do I build a ramp",
  "what radius does a nested element get", "how much space separates two
  groups". Generative, not corrective — `sites-interface-review` checks a built
  page against these same rules; this one is for while you are writing it.
---

<!-- New file 2026-09-08: the AUTHORING half of the jakubkrehel/skills (MIT) craft
     rules. sites-interface-review already carries them as review criteria, but a
     review skill only fires after the page exists, and design-taste carries taste
     (which font, which palette) rather than mechanics (how a ramp is built, what a
     nested radius is). This is the teaching layer, phrased as do-this. See VENDORED.md. -->

# Sites: craft mechanics

Taste picks the ingredients. Craft is what makes them read as one system.

`pocketpaw-design-taste` decides WHICH display face, WHICH aesthetic family,
WHICH accent, and WHICH layout composition. Every one of those choices still has
to be *built*, and the difference between a page that looks designed and a page
that looks assembled is almost entirely in this file: a real scale instead of
four arbitrary sizes, a ramp whose steps each have a job, groups separated by
space that means something, radii that agree.

**Precedence.** design-taste governs every CHOICE — if it bans a font or a
palette, that is final. This file governs the CONSTRUCTION of whatever it chose.
They do not conflict: one names the ingredient, the other sets the method.

---

## 1. Type — build a scale, then stay on it

**Use a modular scale with semantic names.** Pick a ratio (about 1.2 for dense
UI, 1.25-1.333 for marketing, up to 1.6 for editorial display) and generate the
steps. Name them by role (`--text-body`, `--text-lead`, `--text-display`), not by
appearance. Then deviate from it as little as possible — hard-coded sizes with no
system behind them are what make a page feel assembled.

**Heading sizes descend with level.** A visually subordinate heading never
outweighs its parent. Adjacent levels may share a size at the small end as long
as weight or spacing keeps them distinct.

**Line-height by role.** This is the single highest-leverage typographic value on
a marketing page and the one most often left at the browser default.

| Role | Value |
| --- | --- |
| Display / hero headline | `1.0` - `1.1` |
| Section heading | `1.15` - `1.25` |
| Body copy | `1.5` - `1.6` |
| Anything wrapping to 3+ lines | at least `1.4`, even in a tight row |
| Buttons, labels, single-line UI | `1` - `1.2` |

Prefer **unitless** values so they scale with the font size. A fixed `24px`
line-height does not.

**Letter-spacing by size.** Large display type usually wants slightly negative
tracking (`-0.02em` to `-0.04em`). Small uppercase labels need slightly positive
(`0.04em` to `0.08em`) or the letters crowd. Body copy at reading size needs
neither — leave it at `0`.

**Cap the measure.** 60-75 characters per line for anything anyone reads.
`max-width: 62ch` on body copy. Full-width paragraphs are the fastest way to make
a page tiring.

**Weight floors.** Below `18px`, stay at weight `400` or heavier. Weights under
`300` are display-only at `28px`+ and disappear at text sizes.

**Wrap deliberately.** `text-wrap: balance` on headings so a two-line headline
splits evenly. `text-wrap: pretty` on body so no single word is stranded on the
last line. Both are one line of CSS and both are visible.

**Tabular numerals on anything that changes or aligns.** Prices in a pricing
table, stats in a row, countdown digits:
`font-variant-numeric: tabular-nums`. Otherwise the figures jitter and columns
fail to line up.

**Load the weights you actually use.** A browser will synthesize a missing bold
or italic and distort the face. If the design uses 400 and 700, load 400 and 700.

**Prefer real properties over raw font tags.** `font-weight: 650`, not
`font-variation-settings: "wght" 650`. Properties keep working when a
non-variable fallback renders.

**Inputs at `16px` on mobile**, or iOS zooms the page when the field is focused.

---

## 2. Colour — build a ramp, not a set of colours

**A system is ramps, not colours.** One neutral ramp, one accent ramp, and only
the status ramps the page actually renders. A `warning` ramp nothing uses is
maintenance for zero pixels.

**Every step has a job.** A ramp is not a gradient to eyeball from. Each step
exists because a role needs it: page background, raised surface, hover, border,
solid fill, secondary text, body text. Do not generate a step nothing consumes.

**Four properties of a well-formed ramp:**

1. Steps move evenly in **perceived** lightness, not in whatever the colour
   format calls lightness. (This is why `oklch()` is the best default for new
   work — its numbers behave the way this rule describes.)
2. **Hue stays constant** end to end. A ramp that drifts hue reads as two
   different colours at its ends.
3. **Vividness peaks mid-ramp** and falls off at both ends.
4. Steps sit **denser at the light end** than the dark end, or your two lightest
   surfaces are indistinguishable.

Both ends stop short of pure black and white, which cannot carry hue at all.

**Two token tiers, and only one is referenced.** Primitives name a value
(`--blue-500`) and are **never** used in a component. Semantic tokens name a job
(`--color-text-secondary`, `--color-surface-raised`) and are the only tier the
markup touches. That seam is what makes a dark mode or a rebrand possible instead
of an audit of every usage.

**Use a token only in its role.** Never borrow a separator token because its
value happens to suit text today. When borders lighten, the text goes with them.
If a role has no token, add the token.

**One colour, one meaning.** Treat anything within 15° of hue as the same colour.
If the accent means interactive, that hue on static text tells people to click
something that is not clickable.

**Fill exactly one action per view.** Put the colour on the *background*, not the
label: a filled button reads as primary across the room, while accent-coloured
text on a neutral button reads as a link.

**Measure the rendered pair.** Contrast is foreground against the background it
*actually sits on* — the tinted callout, the raised card — not against the page.
And **fix a failure with lightness, not hue**: lightness is the channel contrast
responds to. Changing the hue changes the brand and usually does not fix it.

**Gradients: pick the interpolation space.** `in oklab` is the best default —
even brightness, no hue surprises. Use `in oklch` when a two-hue gradient goes
grey in the middle. The plain sRGB default mutes the midpoint, which is what you
get by not asking.

---

## 3. Space — grouping is the layout

**Group with space, not lines.** Space first, a background shape second,
a separator line last and only where space alone cannot carry it.

**The 2x rule.** The gap *between* groups must be at least **twice** the gap
*within* one — 8px inside, 16px+ outside. Anything less and the grouping reads as
noise rather than structure. This one rule fixes more "it feels cluttered" than
any other change on this page.

**Align to shared edges.** Pick the alignment edges and hold them. Every stray
edge reads as noise. One spacing step per level of subordination.

**Breathing room between controls:** about `12px` between adjacent bordered or
filled controls, about `24px` around borderless text- and icon-only ones.

**Content bleeds, controls float.** Backgrounds and media run to the viewport
edge; text and controls stay inside the layout margins.

**Leave room to grow.** No fixed width or height on a text container. A one-word
button label is the riskiest string on the page — short strings grow
proportionally most when the copy changes.

---

## 4. Surface — the details that read as finish

**Concentric radius.** `outer radius = inner radius + padding`. A 16px card with
8px of padding gives its inner element an 8px radius. Mismatched nested radii is
the most common thing that makes an interface feel subtly wrong without anyone
being able to name it.

**Optical over geometric alignment.** A play triangle, an asymmetric glyph, an
icon beside a label — centre them by eye, not by box. Geometric centring looks
off for anything whose visual mass is not centred.

**Shadows for elevation, borders for structure.** Where a border exists only to
fake depth, use a layered transparent `box-shadow` instead. Keep borders that
communicate structure or state — dividers, selection, focus.

**Image outlines.** A `1px` outline at low opacity gives images consistent depth:
`oklch(0 0 0 / 0.1)` in light, `oklch(1 0 0 / 0.1)` in dark. **Never a tinted
neutral** like slate or zinc — a tinted outline picks up the surface beneath and
reads as dirt on the image edge.

**Scale on press: `scale(0.96)`.** Always `0.96`. Below `0.95` feels exaggerated,
above `0.98` is invisible.

**Match icon stroke to text weight.** `1.5px` stroke beside regular (400) text,
`2px` beside semibold (600). One stroke weight per icon set, one icon library per
page. A hairline icon next to bold text is a visible mismatch.

**Transition only what changes.** Name the properties (`transition-property:
opacity, transform`). Never `transition: all` — it animates things you did not
mean, including layout, and costs frames.

**Icons recolour, they do not swap.** One SVG using `currentColor`, taking its
states from CSS colour and opacity. Outline is the default variant; fill marks
active.

---

## 5. The floor — non-negotiable on a public page

- **Hit areas at least 44x44px**, even where the visual control is smaller.
- **A visible focus ring** on everything focusable. Never removed without a
  replacement; at least 2px on a dark ground.
- **Never rely on colour alone** to carry meaning — pair it with a label, an
  icon, or a shape.
- **Honour `prefers-reduced-motion`** with a complete fallback, not a broken one.
- **Real elements**: `<button>` for actions, `<a>` for navigation, one `<h1>`,
  headings in order, real landmarks.
- **Alt text by purpose** — describe the job the image does, `alt=""` if it is
  decorative.
- **Survive 200% zoom** without clipping or horizontal scroll.
- **Contrast floors**: `4.5:1` for body text, `3:1` for large text and for the
  edge of any control you have to find. Measure against the surface the element
  actually sits on, and fix a failure by moving lightness, not hue.
- **Size floors**: body copy starts near `16px` and interface text near `14px`.
  A 9px legal line and a 12px nav link are both unreadable, not compact.
- **Text never touches the viewport edge.** Give the page container side
  padding of at least `16px` at every width, set once on one wrapper.

---

## Before you ship a section

| Symptom | The rule it is breaking |
| --- | --- |
| Feels cluttered, cannot say why | The 2x grouping gap; groups are not separated from their contents |
| Type looks "off" but sizes seem fine | Line-height by role, and probably a heading left at body leading |
| A headline splits 5 words / 1 word | `text-wrap: balance` |
| Prices or stats jitter or misalign | `font-variant-numeric: tabular-nums` |
| Nested card corners look wrong | Concentric radius: outer = inner + padding |
| The page reads tiring | Measure is uncapped; cap body at ~62ch |
| Two surfaces look identical | Ramp is evenly spaced; tighten the light end |
| Dark mode needed a rewrite | Components referenced primitives instead of semantic tokens |
| Contrast fails on the callout only | Measured against the page, not the surface it sits on |
| Icon looks flimsy beside the heading | Icon stroke does not match text weight |
| Hover feels laggy | `transition: all`; name the properties |
| Two things use the same colour for different jobs | One colour, one meaning (15° rule) |
