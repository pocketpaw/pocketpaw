---
name: sites-restraint
description: |
  The QUIET direction for a Paw Site, with real numbers behind it. Invoke when the
  brief asks for calm, minimal, monochrome, Nordic, Scandinavian, restrained,
  editorial-quiet, "clean and spacious", or when a page is loud and the fix is
  subtraction rather than addition. It supplies a measured alpha-ink ladder, the
  correct DARK-CANVAS INVERSION (the light percentages do not carry across, and
  reusing them makes a dark page louder), and the judgment rules for what must
  NOT be neutralized - brand marks, signature data colours, third-party marks
  acting as row headers, and legally-placed disclosures. Use it as the token
  system for `pocketpaw-design-taste`'s Warm-Minimalist (D) and
  Editorial-Luxury (C) families. Do NOT reach for it when the brief wants loud,
  brutalist, atmospheric or high-energy - restraint is a direction, not a default.
---

<!-- New file 2026-09-08: adapted from ericzakariasson/scandinavian-design into a Paw
     Sites restraint direction. Its recon/review/prototype invocation modes and its
     browser-verification harness are NOT ported - /sites denies Read/Bash/Glob, so a
     source-inspection workflow cannot run there. What is ported is the measured colour
     system, the dark inversion math, and the do-not-neutralize judgment rules, which
     are values and prose rather than tooling. See VENDORED.md. -->

# Sites: restraint

Calm, functional, refined, intentionally simple. A neutral foundation, natural
hierarchy, and imagery carrying the expression while the interface stays quiet.

**Simplicity is not emptiness.** Remove what is unnecessary so the primary task
becomes obvious, then add back context, labels, boundaries or density wherever they
make the page easier to understand. Quiet must never mean vague or inert.

## When restraint is the wrong answer

Some pages are correctly loud, and every instruction in this file points toward
restraint whether or not restraint is an improvement. A brand whose visual volume
*is* its identity can be improved structurally - fixing an inverted hierarchy,
spending size on the thing that matters - while being made worse in voice.

**Where that tension exists, say which of the two you chose and why.** Do not apply
the rules and report the aggregate as an improvement by default.

## The ink ladder

Build intermediate tones with **alpha black over the canvas**, never with warm or
cool grey hexes. Light-canvas defaults:

| Role | Value |
| --- | --- |
| Canvas / surface | `#FFFFFF` |
| Primary ink | `#000000` (or design-taste's tuned off-black) |
| Secondary ink | `rgb(0 0 0 / 64%)` |
| Tertiary ink | `rgb(0 0 0 / 44%)` |
| Border | `rgb(0 0 0 / 10%)` |
| Strong border | `rgb(0 0 0 / 18%)` |
| Hover fill | `rgb(0 0 0 / 5%)` |
| Pressed fill | `rgb(0 0 0 / 9%)` |
| Scrim | `rgb(0 0 0 / 44%)` |

Hierarchy comes from opacity, not from decoration:

- **90-100%** primary text, critical icons
- **60-70%** supporting text
- **40-50%** metadata and non-essential glyphs
- **8-12%** borders and separators
- **4-6%** hover surfaces
- **8-10%** pressed or selected surfaces

**The 40-50% rung does not reach 4.5:1 on white.** It is for glyphs and genuinely
optional text. The moment real reading sits there - a date, a struck-through price,
a caption, a timestamp - **lift it to about 56%**. Expect to do this often: the
range is written for editorial surfaces where metadata really is optional, and most
pages are not that.

**Set ink with alpha colours, never the `opacity` property.** On a container
`opacity` fades every descendant; on a leaf it multiplies against the rung the
element already sits on, quietly pushing supporting text below readable contrast.

**Rungs sitting on a tint have less contrast than the table implies.** An
alternating row fill, a raised card, a callout - re-measure the quiet rungs against
the surface they actually land on, not against the page.

**Do not flatten a working ramp.** If the product already has neutrals with zero
channel spread and enough rungs, leave them. Re-expressing a working hex ramp in
alpha is risk at no visual gain, and it breaks inverted contexts where the same
token paints text over dark media.

## Dark canvas: the inversion is not one-for-one

This is the part most often got wrong. **The same alpha buys more contrast on a
dark canvas than on a light one** - 64% white on `#0A0A0A` reaches 8.2:1 where 64%
black on white reaches 6.7:1. Reusing the light percentages makes supporting text
and chrome *louder*, which is the opposite of the intent.

| Role | Dark value | Note |
| --- | --- | --- |
| Canvas | `#0A0A0A` | near-black, not `#000` |
| Primary ink | `#FFFFFF` | |
| Secondary ink | `rgb(255 255 255 / 56%)` | not 64% |
| Tertiary ink | `rgb(255 255 255 / 36%)` | not 44% |
| Border | `rgb(255 255 255 / 10-12%)` | borders invert almost exactly |
| Hover fill | `rgb(255 255 255 / 9%)` | ~1.5x the light value |
| Pressed fill | `rgb(255 255 255 / 14%)` | ~1.5x the light value |

**The tertiary rung is canvas-sensitive.** At `#0A0A0A`, 46% reaches 4.66:1 and is
enough. At `#1F1F1F` it falls to 4.53:1 with no margin, and by `#262626` it fails.
Dark pages routinely ship surfaces in that range, so **above about `#1A1A1A` use
50%**.

**A resting wash is not a resting fill.** About 5% is right for a row or surface
tint, but a button that must read as a control at rest needs roughly 9% - 5% white
on near-black moves the surface about twelve values out of 255 and does not read as
filled at all.

**Alpha-black fills stop being available.** A recessed well built from
`rgb(0 0 0 / 25%)` is invisible on near-black, so a surface that must sit *below*
the page has to go **lighter** than it. That is the opposite of the instinct.

**Give the focus ring at least 2px on a dark canvas**, and never fewer than it had.
A hairline ring has no surrounding light field to register against.

**Do not mix alpha-black and alpha-white neutrals in one surface.** A deliberately
inverted surface - a light card on a dark page - is fine and often useful, provided
it carries its own consistent ladder throughout.

## Judge a colour cast by channel spread, not by eye

Correct a cast at roughly **eight points of spread or more**; leave it below five;
treat the band between as a judgment call and correct it where it costs nothing.
Spread is far more visible in light ink than in a dark surface: a canvas two or
three points off neutral is invisible, while ink whose channels sit ten or more
points apart reads distinctly cool next to white.

**Correct ink before surfaces.** Neutralizing the canvas first makes every
remaining cast in the ink more obvious, and the page looks worse mid-job.

**Sweep `border-color` alongside backgrounds and text.** A design system's default
outline is often a tinted grey that reads as coloured beside neutral ink, and it
survives a neutralizing pass because the border itself looks intentional.

## What must NOT be neutralized

This is the section that separates a restraint pass from a decolorizing pass.

**A brand mark is not chrome.** Keep the site's own logo as it shipped, in full
colour, while everything around it goes neutral. Where a brand publishes several
colorways, "as shipped" has more than one answer - pick the published variant that
survives the surface it now sits on. On an otherwise monochrome page, one small
mark of signature colour does more identity work than it did in the original.

**A signature datum colour is a brand mark.** The gold on a rating, the green on a
score. It stays wherever the datum appears, including down two hundred rows - being
recognised repeatedly is exactly what a signature colour is for. Ration it only
where the same hue has spread onto things that are *not* the datum.

**A third-party mark can be a data label.** Where a mark identifies the subject of
a record - a team crest beside a score, a flag beside a competitor, an exchange
mark beside a quote - it is the row header, and it keeps its colour regardless of
who owns it. The test is the mark's job in that position, not its ownership.
Third-party marks appearing as *social proof* (customer logos, partner badges,
payment methods) do go monochrome.

**Small marks lose recognition when desaturated.** "The shape carries it" is true
of a logo band at 120px and false at 16px, where hue does most of the
discrimination. Desaturating small marks does not quiet them, it collapses distinct
ones into each other. Check the rendered size first.

**Do not quiet a disclosure below the prominence it shipped with.** Sponsored
labels, legal notices and accessibility affordances sit at a contrast their
publisher chose and may be obliged to hold. Demoting them to the metadata rung is a
compliance failure, not a design one.

**Media keeps its colour; chrome does not.** Product photography, screenshots and
narrative imagery may be expressive - contain that colour inside the media rather
than letting it leak into the surrounding chrome. But **decoration is not protected
by being media**: a panel that is nine parts saturated gradient to one part
screenshot is a decorative gradient, and shipping it inside an image frame does not
change what it is. Judge by the proportion of the frame doing informational work.

**A rebuilt mockup is still a screenshot.** Marketing pages often reconstruct their
own interface in markup, and because that mockup reads the same tokens as the page,
a token-level change reaches inside and repaints the depicted product - dimming an
app's own body text, greying a dashboard that is supposed to look real. Give the
mockup its own scope rather than exempting selectors one at a time.

**Merchandising colour is neither.** Loyalty badges, campaign fills and promotional
flashes are brand-adjacent but encode no state and identify nothing. Neutralize
them, at any volume.

## Layout and type under restraint

- **Left-align by default** - headings, labels, metadata, footers. Centred text is a
  rare deliberate exception, never a leftover, and never mixed with left-aligned
  siblings in one group. A symmetric full-width band whose every member is centred
  (a closing lockup, a copyright row) is a coherent group; flushing one element of
  it left strands it on a wide viewport.
- **Judge clutter by the number of visual weights, not the number of elements.** In
  any region decide which elements are peers and render peers identically: one size,
  one rung, one baseline.
- **A control's weight tracks how often it is used.** The least-used control in a
  region must never be its heaviest element.
- **One icon family, one stroke weight, one optical size.** Icons are monochrome ink
  on the same rung ladder as text. **Emoji are not an icon family** - they carry
  their own colour, weight and optical size and no filter brings them onto the
  ladder. Remove them and let the labels carry the meaning. This governs the
  interface's own iconography only: emoji inside a screenshot, a quoted post or a
  display name are content, and editing them is the same mistake as repainting a
  photograph.
- **Build long pages as a sequence of distinct, spacious chapters**, not a stack of
  similar cards.
- **One accent, at most.** A single established brand accent may carry the primary
  action where it materially improves focus. Omit it entirely when the brief asks
  for strict monochrome. Never invent a second decorative accent.

## Before you finish

| Mistake | Fix |
| --- | --- |
| Light alpha percentages reused on a dark canvas | 56% / 36% secondary / tertiary, 9% and 14% for fills |
| Tertiary rung carrying a real date or price | Lift to ~56% light, or 50% on canvases above `#1A1A1A` |
| `opacity` used to set ink | Alpha colour on the property itself |
| Quiet rung measured against the page, not its tint | Re-measure against the surface it lands on |
| Brand logo desaturated with the chrome | Restore the shipped colourway |
| Team crest or flag greyed in a data row | It is a row header; keep its colour |
| Sponsored or legal label pushed to metadata rung | Restore the prominence it shipped with |
| Working hex ramp rewritten in alpha | Leave it; the rewrite is risk at no gain |
| A loud brand made quiet and reported as improved | Name the trade: structure gained, voice lost |
