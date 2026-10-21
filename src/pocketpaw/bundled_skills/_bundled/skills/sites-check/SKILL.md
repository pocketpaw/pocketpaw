---
name: sites-check
description: |
  Every measurable defect on a Paw Site, each with a stable id (SC-BUILD-02,
  SC-LINK-01, SC-COPY-04): build defects, honesty and fabrication, dead
  controls, generated-copy tells, layout tells, accessibility and mobile. Two
  modes. FIX mode: the author runs it after writing the sections and before
  showing a draft, or after a refine edit that moved layout, and fixes what it
  finds. REVIEW mode: read-only, only when the user asks to review, critique or
  audit the page or asks why it feels off; it returns one ranked findings table
  with evidence and edits nothing until the user asks for the fixes.
  `pocketpaw-design-taste` decides how the page should look and `sites-craft`
  how values are built; this decides whether what got built is broken, false or
  generated-looking. Its defects are not waived by a direction or a brief.
---

# Sites: check

## Pick the mode first

**FIX mode** (you just built or changed the page). Walk the ids below over
the draft, fix every hit, then look at the page again with `preview_site`
(desktop, then `device: mobile`). Do not paste the list into the reply. Tell
the user only what you could not verify and which placeholders remain.

**REVIEW mode** (the user asked for a review, a critique, an audit, or why it
feels off). Read-only. Produce the findings table at the end of this file and
stop. Implement only if the user also asks for the fixes, then re-check what
you changed.

Every rule has an id: `SC-<GROUP>-<NN>`. Groups: BUILD, HONEST, LINK, COPY,
LAYOUT, A11Y, MOBILE, SCORE. Cite the id in a finding, so a tool, a review and
a later fix all point at the same rule. Ids are stable: a retired rule keeps
its number and is never reused.

**What this file does not restate.** Typography, colour, spacing and surface
values (scale, leading, measure, ramps, the 2x grouping gap, concentric radius,
icon stroke, press scale) live in `sites-craft` sections 1 to 4 and its
symptom table. In REVIEW mode, check against those; load `sites-craft` if it
is not in context. The accessibility floor is `sites-craft` section 5; the A11Y
ids below are how you check it.

**Precedence.** User-supplied copy and assets ship verbatim and are never a
finding against the page. A named request (a marquee, a glow, a dark page) is
the spec, not a tell. The floor, honesty and working controls hold whatever
the brief or the direction says.

## Severity and strength

- `HIGH`: content is unreadable or unreachable, a control is dead, or the page
  states something false. Fix before the draft is shown.
- `MEDIUM`: a visible system failure or a strong tell.
- `LOW`: isolated polish or a weak tell.
- `WARN`: worth telling the owner, never a reason to hold the draft or to
  refuse publish. Placeholders are always WARN.

Tells are ranked strongest first inside each group. A **strong** tell convicts
alone. A **weak** tell never convicts alone: report it only when three or more
weak tells sit in one section or the same one repeats across the page.
Structural tells (a sentence shape, a layout shape) always outrank vocabulary.
Cleared by the data: bento alone, a dark page alone, a themed component kit
alone, icons that carry meaning, and a short sentence used once for emphasis
are not tells.

## BUILD: defects that come back as the first revision

- **SC-BUILD-01 Finished with JavaScript off** (HIGH). Every animated element's
  resting state lives in the markup; nothing sets its final state in `onMount`
  or a script. Ask of each section: with all JavaScript off, does this look
  finished? A `static` template runs only scripts you wrote; a framework
  hydrates after the visitor has seen the HTML. The prerender rule itself is
  `pocketpaw-design-taste` 3.F.
- **SC-BUILD-02 Hero fits the fold** (HIGH). Headline, subhead and primary CTA
  sit in the first viewport at 1366x768, 1440x820 and 1920x860. If the CTA
  falls below: lower the display `clamp()` maximum, tighten top padding and
  gaps, cut a line of copy.
- **SC-BUILD-03 Hero height** (MEDIUM). The hero is as tall as its content. If
  it is viewport-tall, size it in `dvh`, never `vh` (mobile `100vh` hides the
  CTA under browser chrome). Cap it at headline, subtext, CTA row and at most
  one proof line.
- **SC-BUILD-04 Sticky bleed** (MEDIUM). A sticky element beside a column of
  steps is sized to fit one step, and the last step has a trailing spacer
  (about `40vh`) so it is fully gone before the next heading appears. Check the
  transition frame itself.
- **SC-BUILD-05 Sticky not sticking** (MEDIUM). An ancestor with `overflow`
  `hidden`, `auto` or `scroll` silently disables `position: sticky`. Walk the
  ancestors before touching the element.
- **SC-BUILD-06 Fixed nav bleed-through** (MEDIUM). A transparent fixed nav
  turns opaque (about 95% plus a blur) past roughly 60px of scroll, driven
  from CSS (`animation-timeline: scroll()`), or it is opaque from the start.
  Desktop nav is one line, 80px tall or less.
- **SC-BUILD-07 Screenshots** (MEDIUM). A real screen sits in a `<figure>`
  with a hairline border, still. No float keyframe, glow, halo or rim light.
  Crop to the real frame; never re-round an image that already has rounded
  corners.
- **SC-BUILD-08 Layout shift** (MEDIUM). Every image and video has
  `width`/`height` or `aspect-ratio`. Fonts use `font-display: swap` or are
  preloaded.
- **SC-BUILD-09 Motion properties** (MEDIUM). Animate `transform` and
  `opacity` only. No `window` scroll listener (use `animation-timeline:
  view()`), no scroll hijacking, custom cursor or mouse-follow, at most one
  marquee, and no entrance that makes a visitor wait to read. Timing and
  easing values are `sites-craft`.
- **SC-BUILD-10 Hover gated** (LOW). Hover effects sit behind `@media (hover:
  hover) and (pointer: fine)`. An element that swaps between two states (label
  to icon, image to image) stacks both in one grid cell so nothing reflows.
- **SC-BUILD-11 Hero video** (MEDIUM). A short muted loop with `playsinline`
  and a poster frame, a few MB at most, paused when out of view and under
  reduced motion.
- **SC-BUILD-12 Page basics** (MEDIUM). A real `<title>`, a meta description,
  `og:image` and Twitter card tags, a favicon that is not the scaffold
  `vite.svg`, no generator meta tags left by a template, and a 404 page. On a
  multi-page site the nav marks the current page.
- **SC-BUILD-13 Interactive states** (LOW). Every control has hover,
  `:focus-visible`, `:active` and disabled states, plus loading, empty and
  error where they apply.
- **SC-BUILD-14 Form behaviour** (MEDIUM). Required fields and email format
  are validated client-side. Each field reserves its error line so a message
  does not push the form around. Inputs and buttons in one row share a height.
- **SC-BUILD-15 Look before done** (HIGH). Call `preview_site` and look at the
  hero, every major section and every section-to-section transition, desktop
  and mobile. If the preview fails, say the page was not visually checked.
  Claiming a check that did not happen is the one defect that costs trust.

## HONEST: nothing false on a real business's domain

- **SC-HONEST-01 Invented specifics** (HIGH). A number, date, price, customer
  count, award, address, phone, testimonial or client logo the brief did not
  supply. Replace it with the real fact, a labelled placeholder (`[metric to
  confirm]`), or a different section shape. A page with no testimonials is
  honest; three invented ones are a legal problem, not a style one.
- **SC-HONEST-02 Hero-metric strip with nothing behind it** (HIGH). A row of
  big numbers (`+47%`, `10x`, `50,000+ teams`) where the brief gave none. Drop
  the strip; do not fill it.
- **SC-HONEST-03 Default people and promptonyms** (HIGH). Sarah Chen, Elena
  Vasquez, Elara Voss, John or Jane Doe, Acme, Lumina, Nexus, `johndoe@example.com`,
  `555` numbers. Use the owner's real names or a labelled placeholder.
- **SC-HONEST-04 Unearned badges** (MEDIUM). "Most popular", "Best value",
  "Trusted by" or a rating on something the brief did not single out.
- **SC-HONEST-05 Fact-diff after any rewrite** (HIGH). Every name, number,
  price, date and address in the text before your edit survives it unchanged,
  or the change is reported to the user. A rewrite pass is where facts drift.
- **SC-HONEST-06 Declining a claim** (MEDIUM). When the page needs proof you do
  not have, leave `[NEED: proof]` or say only what is true. Do not soften an
  unsupported claim into a vaguer one ("among the best").
- **SC-HONEST-07 Placeholders left** (WARN). Sweep for `[`, `[NEED:`, `[Phone]`,
  lorem ipsum, `example.com`, "Your Company", "Your Name". Keep them in the
  draft and list every one in the reply so the owner can fill them. Never a
  publish block.
- **SC-HONEST-08 Asset honesty** (HIGH). Every `src` is a URL a tool returned
  or the user supplied, never a made-up path. A found stock photo never stands
  in for the client's own product, room, team or food.
- **SC-HONEST-09 Generated images checked** (MEDIUM). A generated image is
  looked at before it ships: no garbled text, fake logos or broken hands. Never
  generate an icon or a logo; use the icon tool or the owner's mark.
- **SC-HONEST-10 Supplied copy verbatim** (HIGH). Text the user supplied ships
  as written, punctuation included. Fixing its dashes or tone is not yours to
  do unless asked.

## LINK: every control does something

Checkable from source without a render. List every dead control by its label.

- **SC-LINK-01 Anchor target exists** (HIGH). Every `href="#x"` has a matching
  `id="x"` on the same page.
- **SC-LINK-02 No empty action** (HIGH). No bare `href="#"`, no
  `javascript:void(0)`, no `onclick` that does nothing. Wire it or remove it.
- **SC-LINK-03 Internal paths resolve** (HIGH). Every link to another page
  points at a page that exists. A nav item for a page you did not build goes.
- **SC-LINK-04 Forms submit somewhere** (HIGH). Every form has a real submit
  target (the site's form endpoint or a tool-provided action) and a visible
  success state.
- **SC-LINK-05 Toggles change state** (MEDIUM). Menus, tabs, accordions and
  theme switches change something visible and expose it (`aria-expanded`,
  `aria-selected`).
- **SC-LINK-06 Footer and legal links** (MEDIUM). Every footer link is real.
  Privacy and terms appear where the business collects data, and point at
  pages that exist or are removed.
- **SC-LINK-07 Links say where they go** (LOW). No "click here" or bare "read
  more"; the label names the destination.

## COPY: generated-copy tells

Quote the exact string as evidence. Fix by restructuring around the concrete
point, never by swapping one tell for its neighbour. Applies to marketing
sections; legal, booking and form text stays plain and standard.

Strong, each one alone is a finding:

- **SC-COPY-01 Em dash in generated copy** (MEDIUM). Zero em dashes (U+2014)
  in any visible text you wrote: headings, body, buttons, captions, alt text,
  footer. Split the sentence or rewrite it; a colon or spaced hyphen in the same
  slot keeps the tell. An en dash belongs only inside a numeric range, where a
  plain hyphen also works. Supplied copy keeps its dashes (SC-HONEST-10).
- **SC-COPY-02 Negative parallelism** (MEDIUM). "It's not just X, it's Y",
  "Not only X, but Y", "Not a feature. A platform.", "Not another X. Finally
  Y.", and tacked-on fragments like "no guessing" or "zero friction".
- **SC-COPY-03 Chatbot residue** (HIGH). Text addressed to the requester left
  in the deliverable: "Certainly!", "Here is your", "I hope this helps", agent
  notes in HTML or CSS comments, a design declaration in the source, alt text
  that starts "Image of".
- **SC-COPY-04 Staged reveals** (MEDIUM). A question answered by the next
  fragment: "The result? ...", "The best part? ...", "The secret? Simple:".
  State the thing.
- **SC-COPY-05 Throat-clearing and bridges** (MEDIUM). "In today's fast-paced
  world", "Let's be honest", "Here's the thing", "That's where X comes in",
  "Enter X", "In this section we'll explore". Delete the sentence; the next
  one is usually the real start.
- **SC-COPY-06 Captions that narrate the design** (MEDIUM). "A clean, modern
  layout showcasing our work". Captions say what is in the picture.
- **SC-COPY-07 Owner's never-say words** (MEDIUM). Any word the owner said at
  intake they never use, or that their trade would not say.
- **SC-COPY-08 Aphorism formulas** (MEDIUM). "Trust is the currency of
  growth", "X is the language of Y". Say the point.
- **SC-COPY-09 Staccato runs** (MEDIUM). "No templates. No defaults. No
  limits." One short sentence for emphasis is fine; a run is theatre.
- **SC-COPY-10 Inflated stakes and weasel attribution** (MEDIUM). "A pivotal
  moment", "game-changing", "reshaping the landscape"; "experts agree",
  "studies show", "trusted by industry leaders" with no named source.
- **SC-COPY-11 Chat closers and scroll cues** (LOW). "Ready to get started?"
  closing every section; a `Scroll` label or bare down arrow.
- **SC-COPY-12 Keyword-stuffed headings, robotic meta** (LOW). A heading or a
  meta description that lists search terms ("Best Dentist Austin TX Family
  Dentist") instead of saying one thing.

Weak, only in clusters (see Severity and strength):

- **SC-COPY-13 LLM vocabulary** (LOW, weak). Measured excess words in
  2023-2026 model text: delve, showcase, underscore, tapestry, testament,
  seamless, elevate, unlock, empower, robust, leverage, navigate, realm,
  landscape, crucial, pivotal, vibrant, meticulous. Report at three or more in
  one section. Inflections count.
- **SC-COPY-14 Brochure words** (LOW, weak). Nestled, boasts, a haven for, in
  the heart of, look no further, one-stop shop, a cut above, passion for
  excellence, where X meets Y.
- **SC-COPY-15 Trailing -ing riders** (LOW, weak). A clause bolted on for
  significance: "..., ensuring every guest feels at home", "..., making it the
  perfect choice". End the sentence earlier.
- **SC-COPY-16 Forced triads** (LOW, weak). Every list three long because
  three sounds complete. Lists are as long as the content.
- **SC-COPY-17 Filler, hedges, vague extremes** (LOW, weak). "We're excited
  to announce", "may potentially help", truly, incredibly, the very best,
  world-class.
- **SC-COPY-18 Copula avoidance and distant narrator** (LOW, weak). "Serves
  as", "stands as", "boasts" where "is" or "has" works; "visitors will
  discover" where "you" works.
- **SC-COPY-19 Performative section labels** (LOW, weak). "The Problem", "The
  Solution", "Why Us", "Our Philosophy" as headings with no content of their
  own. Name the section by what it says.
- **SC-COPY-20 Repeated or ornamental labels** (LOW, weak). The same label in
  two slots of one card; dismissing a thing as "theater"; a locale, time or
  weather strip on a brand that is not about place.

Interface text, checked in the same pass: buttons are verb-first ("Book a
table", not "Submit"); one word per concept across the page; one
capitalization policy; errors say how to fix it, next to the field, with no
"Oops!"; empty states point at the action that fills them; a placeholder is an
example, never the label. Report these as SC-COPY-21 (LOW).

## LAYOUT: generated-layout tells

Strong:

- **SC-LAYOUT-01 Fake chrome** (MEDIUM). Hand-built browser bars (URL pill and
  traffic-light dots), phone frames, code or terminal windows, IDE chrome, or a
  fake terminal standing in for a product. Use a real screenshot in a
  `<figure>` or let the content stand alone.
- **SC-LAYOUT-02 Three equal icon cards** (MEDIUM). The three-up of icon,
  heading and two lines, identical in weight. Vary by content or use a list.
- **SC-LAYOUT-03 Template counts** (MEDIUM). Three steps in "How it works"
  because the template had three; a section the brief never asked for.
- **SC-LAYOUT-04 Repeated rhythm** (MEDIUM). Neighbouring sections in the same
  layout family, or image/text zigzag past two rows (screenshots dropped as
  flat alternating rows are the classic case). Change one neighbour's shape.
- **SC-LAYOUT-05 Accent word in a headline** (MEDIUM). One word italicised,
  coloured, underlined or greyed inside an upright headline. Italic stays for
  emphasis in running text.
- **SC-LAYOUT-06 Headline over two lines** (MEDIUM). At any width. Cut words
  first, then size.
- **SC-LAYOUT-07 Pricing and comparison alignment** (MEDIUM). Rows line up
  across columns and every column's CTA sits on one baseline.
- **SC-LAYOUT-08 Voids** (MEDIUM). An empty band taller than half a screen
  between sections, or a canyon between media and copy in a two-column block.
  Push media to the inner edge (`justify-items: end` on its cell), let space
  fall outside; use `fr` columns, never `calc(33% - 1rem)`.
- **SC-LAYOUT-09 Glass and pill nav** (MEDIUM). Glass on more than two
  elements, or a floating blurred pill nav the direction did not call for.

Weak:

- **SC-LAYOUT-10 Decorative numbering and index tags** (LOW, weak). `01 / 02 /
  03` on things with no order, `§2` or `[ Services ]` tags on section heads.
- **SC-LAYOUT-11 Meta strings and stamps** (LOW, weak). Middle-dot strings
  (`Design · Build · Grow`), "Est. 2024" stamps on a young business.
- **SC-LAYOUT-12 Bento without difference** (LOW, weak). A bento grid whose
  cells hold items of equal weight. Bento is for items that differ.
- **SC-LAYOUT-13 Offset block shadows** (LOW, weak). Hard offset shadows
  outside a neobrutalist direction.
- **SC-LAYOUT-14 Logo rows** (LOW). Logos monochrome, on one optical baseline,
  served from the site, never hotlinked, and only logos of real customers
  (SC-HONEST-01).

## A11Y: the floor, checked

The rules are `sites-craft` section 5. These are the checks.

- **SC-A11Y-01 Contrast measured** (HIGH). Body 4.5:1, large text and control
  edges 3:1, measured against the surface the element actually sits on. In
  REVIEW mode report the pair, the value and the threshold, and leave the
  colours alone: they are a design decision.
- **SC-A11Y-02 Text on images** (HIGH). Text over a photo or video sits on a
  scrim you measured at the worst point of the image, not on luck.
- **SC-A11Y-03 Focus ring** (HIGH). Visible on everything focusable, via
  `:focus-visible`, at least 2px on dark grounds. It appears instantly; a ring
  that fades or slides in is late for keyboard users.
- **SC-A11Y-04 Native elements and keyboard** (HIGH). `<button>` for actions,
  `<a>` for navigation, full keyboard reach, focus trapped and restored in any
  overlay.
- **SC-A11Y-05 Labels and errors** (HIGH). Every input has a programmatic
  label and the right `type` or `inputmode`; errors are announced and sit next
  to the field.
- **SC-A11Y-06 Structure** (MEDIUM). One `<h1>`, headings in order, `<nav>`,
  `<main>`, `<footer>` landmarks, and a skip-to-content link.
- **SC-A11Y-07 Alt text by purpose** (MEDIUM). Describe what the image does;
  `alt=""` on decorative images.
- **SC-A11Y-08 Decorative art hidden** (LOW). `aria-hidden="true"` on
  decorative SVG, icon-only flourishes and CSS art.
- **SC-A11Y-09 Reduced motion** (MEDIUM). `prefers-reduced-motion` gets a
  complete page, not a broken one; no content trapped behind an animation.
- **SC-A11Y-10 Carousels** (MEDIUM). Never autoplay without a visible pause.
- **SC-A11Y-11 Colour alone** (MEDIUM). Meaning is never carried by colour
  alone; pair it with a label, icon or shape.
- **SC-A11Y-12 Zoom** (MEDIUM). 200% zoom and text resize without clipping or
  horizontal scroll.

## MOBILE: phone widths, 320 to 414, and up to 1920

- **SC-MOBILE-01 No horizontal scroll** (HIGH). `overflow-x: clip` on `html`
  and `body`, never `hidden` (it creates a scroll container and breaks sticky).
- **SC-MOBILE-02 Shrinkable tracks** (HIGH). Grid tracks holding images use
  `minmax(0, 1fr)`; a bare `1fr` will not shrink below the image.
- **SC-MOBILE-03 Long words** (MEDIUM). Display headings carry `overflow-wrap:
  anywhere` and `min-width: 0`.
- **SC-MOBILE-04 One clean column** (MEDIUM). Every asymmetric layout collapses
  to one column at the width where its content stops fitting, not at a fixed
  768px.
- **SC-MOBILE-05 One-line controls** (MEDIUM). Buttons, nav links, footer
  links and CTAs stay on one line, or get shorter labels.
- **SC-MOBILE-06 Targets** (HIGH). Tap targets at least 44x44px; anything that
  only appears on hover has a tap path.
- **SC-MOBILE-07 Input size** (MEDIUM). Inputs at 16px or iOS zooms the page on
  focus.
- **SC-MOBILE-08 Phone register** (LOW). Section padding about half the desktop
  value, and in a media and copy block the picture comes first.
- **SC-MOBILE-09 Text off the edge** (MEDIUM). At least 16px side padding at
  every width, set once on the page wrapper.

## SCORE: the judgement pass

Run after the defect ids, in FIX mode only, privately in your reasoning.

- **SC-SCORE-01 Self-score** (MEDIUM). Score the page 1 to 5 on six axes.
  Philosophy: one idea, or a pile of sections? Hierarchy: does the eye land on
  the conversion? Execution: does it survive 1440 and 375? Specificity: could
  this copy belong to another business? Restraint: what could go with no loss?
  Variety: do neighbouring sections differ in shape? Anything below 3 means a
  revision pass before the draft is shown. Scores never go into the page.
- **SC-SCORE-02 Removal tests** (MEDIUM). Strip a card's border, background
  and radius: if nothing got worse, it was never a card. Hide the hero image:
  if the fold still works, the image does nothing. Hide the nav: if the brand
  is gone, it lives only in a logo. Delete 30% of the words: if it got better,
  keep deleting. Swap the logo for a coffee shop, a boutique hotel and a
  literary magazine: if the hero fits all three, nothing on it is specific yet.
  A "yes" means a decision was never made; make it.
- **SC-SCORE-03 If it passes, stop** (LOW). A section that passes every id is
  done. Say so and do not rewrite it to look busy.

## REVIEW mode: the protocol

**Evidence, not taste.** Every finding carries the evidence that produced it:
a quoted string, a selector and value, a measured pair, a screenshot frame.
Anything you could not observe says `not verified` and why (`no preview
taken`, `not measurable from a screenshot`). Wrapping, widows, truncation and
sticky timing only show at real content lengths in a real render, so look at
`preview_site` before claiming them. A short honest review beats a long
confident one.

1. **Resolve the scope.** One section, one page, or the site. If the user named
   a change, review the change, not the page around it.
2. **Recon first.** Read the tokens, type scale and conventions before judging.
   "Use a token" on a page with no token system is noise.
3. **Structure before vocabulary.** Check layout and sentence shapes before
   word lists; a page with clean words and a template skeleton still fails.
4. **Weak signals never convict alone.** Apply the strength rule above.
5. **Rank by user impact,** then by leverage: a token or section fix that clears
   eleven instances comes before eleven instance fixes.
6. **Consolidate.** The same defect nine times is one finding with a count.
7. **Cap pre-existing issues** at three, in their own section, outside the
   verdict.
8. **Do not mutate.** A review is read-only until the user asks for fixes.

Output:

```
## Scope
<what was reviewed, at what widths, in what render>

## Coverage
| Group | Status |
| --- | --- |
| BUILD | Reviewed |
| HONEST | Reviewed |
| LINK | Reviewed (source) |
| COPY | Reviewed |
| LAYOUT | Reviewed |
| A11Y | Reviewed (source only; focus order not verified) |
| MOBILE | Not reviewed: no mobile preview taken |
| Craft values (sites-craft 1-4) | Reviewed |

## Findings
| # | Id | Severity | Finding | Evidence | Fix |
| - | -- | -------- | ------- | -------- | --- |
| 1 | SC-A11Y-01 | HIGH | Body text on the tinted callout misses AA | 3.8:1 on `--surface-2`, needs 4.5:1 | Lift the ink step; leave the accent |
| 2 | SC-LINK-01 | HIGH | "Pricing" nav link goes nowhere | `href="#pricing"`, no `id="pricing"` | Add the section id or drop the link |

## Pre-existing (max 3)

## Verdict
<one line: ship, ship with the HIGH items fixed, or needs another pass>
```

Never mark a group `Reviewed` that you did not look at: write `Not reviewed`
and the reason.
