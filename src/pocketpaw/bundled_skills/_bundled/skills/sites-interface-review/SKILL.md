---
name: sites-interface-review
description: |
  Review a built Paw Site and report ranked, evidence-backed findings across six
  domains - typography, colour, layout, UI polish, accessibility and interface
  writing. Invoke when the user asks to review, critique, audit, or "look over" a
  page, when they ask why a page "feels off", or before handing a site back. It is
  READ-ONLY by default: it produces one ranked findings table with a measured value
  and a fix for each, and it marks anything it could not actually observe as NOT
  VERIFIED rather than asserting it. This is the corrective counterpart to
  `pocketpaw-design-taste`, which is generative - taste decides what to build, this
  decides whether what got built holds up. Do NOT use it to author sections.
---

<!-- New file 2026-09-08: adapted from jakubkrehel/skills (the better-typography,
     better-colors, better-layout, better-ui, better-accessibility, better-writing and
     better-interface skills) into ONE self-contained Paw Sites review skill. Upstream
     ships seven skills across 40+ files; /sites denies Read/Glob/Bash, so reference
     subfiles are unreachable there and the six domains are consolidated into this
     single file with the highest-value rules kept verbatim in their exact values.
     See VENDORED.md. -->

# Sites: interface review

## Evidence, not taste

A review is a set of claims about a page. Every claim carries the evidence that
produced it, or it carries the words **not verified**.

**The honesty rule that matters most here.** On the /sites surface you usually
cannot open the rendered page - `Read`, `Bash` and `Glob` are denied, and a preview
may not exist. Bad wrapping, widows, truncation, contrast on a real background and
sticky-release timing **only show up at real content lengths in a real render**. A
claim about any of those, made from the source alone, is a guess.

So: inspect what you actually can, and for the rest write `not verified: no render
available`. A short honest review beats a long confident one. Never report a domain
as `Clear` when you did not look at it - mark it `Not reviewed` and say why.

## How to run it

1. **Resolve the scope.** One section, one page, or the whole site? If the user
   named a change, review the change; do not review the whole page around it.
2. **Recon before judgment.** Read the tokens, the type scale, the spacing system
   and the existing conventions first. A finding that says "use a token" on a
   project with no token system is noise.
3. **Rank by user impact,** not by domain order.
4. **Prefer the cheaper fix.** A token change that fixes eleven instances beats
   eleven instance-level findings.
5. **Consolidate systemic findings.** If the same defect appears nine times, that is
   one finding with a count, not nine findings.
6. **Do not mutate.** A review request is read-only. Implement only if the user also
   asks for it, and then re-verify afterwards.
7. **Cap pre-existing issues.** Report at most three problems that predate the change
   being reviewed, in their own section, outside the verdict.

## Domain 1: Typography

- **Fewer fonts, sizes and weights.** Rarely more than three families. Pair for
  contrast, not similarity: a serif headline over a sans body reads as deliberate;
  two near-identical sans-serifs read as a mistake.
- **Below 18px, stay at weight 400 or heavier.** Weights under 300 are display-only
  at 28px and above; they disappear at text sizes.
- **Heading sizes descend with level.** A visually subordinate heading never
  overpowers its parent.
- **Line-height by role.** Headings around `1.1`. Body `1.5` to `1.6`. Prefer
  unitless so it scales with size. Anything wrapping to three or more lines needs at
  least `1.4`, even in a height-constrained row.
- **Letter-spacing by size.** Large headings often want slightly negative tracking;
  small uppercase labels need slightly positive; body copy at reading size needs
  neither.
- **Cap the measure** at 60-75 characters per line.
- **Wrap deliberately.** `text-wrap: balance` on headings, `text-wrap: pretty` on
  body. No single word alone on a last line.
- **Tabular numbers on changing values** - `font-variant-numeric: tabular-nums` so
  figures do not jitter as they update.
- **Prefer properties over raw font tags.** `font-weight: 650`, not
  `font-variation-settings: "wght" 650`. Properties keep working when a non-variable
  fallback renders.
- **Load the weights and styles actually used.** Browsers synthesize a missing
  weight or italic and distort the face.
- **Inputs at 16px on mobile**, or iOS zooms the page on focus.

## Domain 2: Colour

- **A system is ramps, not colours.** One neutral ramp, one accent ramp, and only
  the status ramps the page actually renders.
- **Every step has a job** - background, hover, border, solid fill, text. Do not
  generate a step no role consumes.
- **Name primitives by hue, semantics by role.** `--blue-500` is a primitive and is
  never referenced by a component; `--color-text-secondary` is what components use.
  That seam is what makes a dark mode possible.
- **Use a token only in its role.** Never borrow a separator token because its value
  happens to suit text today - when borders lighten, the text goes with them.
- **Hold the hue across the ramp.** Even steps in *perceived* lightness, constant
  hue, vividness peaking mid-ramp, steps denser at the light end. Both ends stop
  short of pure black and white, which cannot carry hue.
- **One colour, one meaning.** Treat anything within 15° of hue as the same colour.
  If the accent means interactive, that hue on static text tells people to click
  something that is not clickable.
- **Fill exactly one action per view.** Put the colour on the background, not the
  label: a filled button reads as primary across the room; accent-coloured text on a
  neutral button reads as a link.
- **Measure the rendered pair.** Measure a foreground against the background it
  actually sits on, not the page background. When a pair fails, report the pair, its
  measured value and the threshold it misses - then **leave the colours alone**.
  They are a design decision. Change them only when asked, and re-measure after.
- **Fix contrast with lightness, not hue.** Lightness is the channel contrast
  responds to.
- **Gradients: `in oklab` is the best default.** Use `in oklch` when a two-hue
  gradient goes grey in the middle. The sRGB default mutes the midpoint.

## Domain 3: Layout

- **Group with space, not lines.** Space first, background shapes second, separator
  lines last. The gap between groups is at least **2x** the gap within one (8px
  intra, 16px+ inter) or the grouping reads as noise.
- **Keep controls distinct from content.** A control styled like the text beside it
  does not read as a control.
- **Align to shared edges.** Every stray edge reads as noise.
- **Order by importance** - most important content near the top and the leading edge.
- **Hint at hidden content.** Progressive disclosure needs a visible affordance; let
  the next item peek 16-32px past the scroll edge. Content hidden with zero cue may
  as well not exist.
- **Breathing room between targets:** ~12px between adjacent bordered or filled
  controls, ~24px around borderless text- and icon-only ones.
- **Content bleeds, controls float.** Backgrounds and media extend to the viewport
  edge; controls and text stay inside the layout margins and safe areas.
- **Hold structure until it breaks.** Breakpoints come from the content, not from
  768/1024 because they are the defaults. Collapse late.
- **Plan for growth.** No fixed width or height on a text container. A one-word
  button label is the riskiest string on the page - short strings grow
  proportionally most under translation.
- **Use logical properties** (`padding-inline-start`, `margin-inline-end`). Reserve
  physical left/right for genuinely physical geometry.

## Domain 4: UI polish

Every value below is specific, not a range to approximate.

- **Concentric radius:** outer radius = inner radius + padding. Mismatched nested
  radii is the most common thing that makes an interface feel subtly off.
- **Optical over geometric alignment.** Icons with a visual centre of mass - play
  triangles, asymmetric glyphs - need a manual nudge.
- **Shadows for elevation, borders for structure.** Where a border exists only to
  fake depth, use a layered transparent `box-shadow` instead. Keep borders that
  communicate structure or state.
- **Interruptible animations.** CSS transitions for interactive state changes -
  they can be interrupted mid-flight. Keyframes only for staged one-shot sequences.
- **Stagger infrequent entrances by ~100ms**, in semantic chunks. Leave
  high-frequency interactions unstaggered.
- **Exits are softer than enters** - a small fixed `translateY`, `ease-out` both ways.
- **Icon transitions** animate `opacity`, `scale` and `blur`, never `visibility`.
  Exact values: scale `0.25` -> `1`, opacity `0` -> `1`, blur `4px` -> `0`.
- **Image outlines:** a `1px` outline at low opacity - `oklch(0 0 0 / 0.1)` in light,
  `oklch(1 0 0 / 0.1)` in dark. Never a tinted neutral like slate or zinc; a tinted
  outline picks up the surface beneath and reads as dirt on the image edge.
- **Scale on press: `scale(0.96)`.** Always `0.96`; below `0.95` feels exaggerated.
- **Suppress transitions on a theme switch.** Otherwise every colour, background,
  border and shadow transition fires at once and the flip smears. Inject
  `*,*::before,*::after{transition:none !important}`, force a reflow, remove it next
  frame.
- **Transition only what changes.** Name the properties; never `transition: all`.
- **`will-change` sparingly** - only `transform`, `opacity`, `filter`, and only after
  you see a first-frame stutter.
- **Match icon stroke to text weight:** `1.5px` beside regular (400), `2px` beside
  semibold (600). One stroke weight per set, one icon library per surface.
- **One SVG, recoloured per state** via `currentColor`. Outline is the default
  variant; fill marks active.
- **Motion restraint.** High-frequency interactions get instant feedback or a
  transition of 150ms or less. Every animated state change also needs a static cue -
  colour, an icon, or a label. Motion is never the only feedback channel.

## Domain 5: Accessibility

- **Native elements first.** A `<button>` before a `div` with a click handler.
- **Visible focus rings**, never removed without a replacement. At least 2px on a
  dark canvas.
- **Full keyboard support**, and focus trapped then restored for any overlay.
- **Minimum hit area 44x44px**, even where the visual control is smaller.
- **Label and type every control.** Every input has a programmatic label and the
  right `type` / `inputmode`.
- **Errors announce**, sit next to what broke, and say how to fix it.
- **Never rely on colour alone** to carry meaning.
- **Honour `prefers-reduced-motion`** with a complete, non-broken fallback.
- **Alt text by purpose:** describe the function, not the picture; `alt=""` for
  decorative images.
- **Structure is navigation** - correct heading order, real landmarks.
- **Survive 200% zoom and text resize** without clipping or horizontal scroll.

## Domain 6: Interface writing

- **One voice, flexible tone.** Match the existing voice before improving it.
- **Address the reader directly.** "Your invoices", not "the user's invoices".
- **Plain words over clever ones.**
- **Verb-first buttons.** "Save changes", not "OK" or "Submit".
- **Consistent flow vocabulary** - one word per concept across the whole page.
- **Links describe their destination.** Never "click here" or a bare "read more".
- **One capitalization policy**, applied everywhere.
- **Settings describe the ON state**, so the toggle reads unambiguously.
- **Errors say how to fix it, next to where it broke.** No `Oops!`, no bare
  "Something went wrong", no exclamation marks in success messages.
- **Empty states point forward** with the action that fills them.
- **Placeholders are examples, not labels** - a placeholder that disappears on focus
  is not a label.

## Review output format

```
## Scope
<what was reviewed, and at what widths / in what render>

## Coverage
| Domain | Status |
| --- | --- |
| Typography   | Reviewed |
| Colour       | Reviewed |
| Layout       | Reviewed |
| UI polish    | Not reviewed: no render available |
| Accessibility| Reviewed (source only; focus order not verified) |
| Writing      | Reviewed |

## Findings
| # | Severity | Domain | Finding | Evidence | Fix |
| - | -------- | ------ | ------- | -------- | --- |
| 1 | HIGH     | Colour | Body text on the tinted callout misses AA | measured 3.8:1 against `--surface-2`, needs 4.5:1 | Lift the ink rung; do not change the accent |

## Pre-existing (not part of this change, max 3)
...

## Verdict
<one line: ship, ship with the HIGH items fixed, or needs another pass>
```

**Severity.** `HIGH` makes content unreadable, unreachable, or assigns a misleading
meaning. `MEDIUM` is a noticeable system or theme failure. `LOW` is isolated polish.

## Before you finish

| Mistake | Fix |
| --- | --- |
| Six disconnected domain reports | One ranked findings table |
| A visual claim inferred from source only | Inspect the render, or mark it not verified |
| Domain marked `Clear` that was never inspected | Mark it `Not reviewed` and say why |
| Nine instances of one systemic defect | One finding with a count |
| Contrast "fixed" by changing the brand colour | Report the measured pair; leave the colours to the user |
| Every legacy issue in a touched file reported | Three pre-existing findings, in their own section |
| Findings implemented during a review request | Review is read-only unless implementation was asked for |
