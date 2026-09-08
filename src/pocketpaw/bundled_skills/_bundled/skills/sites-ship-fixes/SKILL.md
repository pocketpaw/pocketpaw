---
name: sites-ship-fixes
description: |
  The catalogue of defects that come back as the FIRST revision on a Paw Site.
  Invoke it after authoring sections and before showing a draft, or when the user
  says a page is "off", "cramped", "the button is cut off", "the sticky thing
  overlaps", "there is a huge gap", "the nav is unreadable over the hero". Each
  entry is a concrete, measurable failure with the fix: the hero not fitting the
  fold, a sticky element bleeding into the next section, a transparent fixed nav
  with headings behind it, screenshots dropped as flat rows, dead gutters in
  two-column blocks, and motion that only exists when JavaScript runs. This is a
  BUILD-DEFECT skill, not a taste skill - `pocketpaw-design-taste` decides how the
  page should look, this decides whether what you built actually holds up.
---

<!-- New file 2026-09-08: adapted from blackbrainpy/designs-by-kore (its "Ship-it
     checklist - recurring first-draft fixes" plus the reduced-motion/cleanup items of
     its quality bar) into a Paw Sites defect catalogue. Its GSAP / Lenis / ScrollTrigger
     / React-Three-Fiber library patterns are deliberately NOT ported: Paw Sites prerender
     with the client bundle pruned unless keepsClientBundle is set, so those libraries do
     not run. Every fix here is expressible in CSS and survives with JS disabled.
     See VENDORED.md. -->

# Sites: ship fixes

Craft first, effects second. A page with a real type scale, honest grid tension and
one restrained accent beats a page drowning in motion. Motion amplifies good design;
it cannot rescue bad design.

Everything below is a defect that reliably comes back as revision #1. Run this list
**before** showing a draft, not after the user finds them.

## 0. The rule that gates every fix here

Paw Sites prerender. Unless this site explicitly keeps its client bundle, `onMount`,
`use:` actions, IntersectionObserver, scroll listeners and WebGL **never run**. So
every fix in this file is CSS, and every one of them must hold with JavaScript
disabled. If a fix you are reaching for needs JS the site does not keep, it is a
claim, not a page - rebuild it in CSS or drop it.

## 1. The hero must fit the fold

The single most common defect. The headline, the subhead and the **primary CTA**
must all sit within the first viewport at common laptop heights: test 1920x860,
1440x820 and 1366x768.

If the CTA falls below the fold:
- reduce the display `clamp()` maximum,
- tighten the hero's top padding and the gaps between its elements,
- cut a line of copy - a two-line headline and a one-line subhead is usually enough.

Use `min-height: 100dvh`, never `100vh` - on mobile `100vh` is taller than the
visible area and pushes the CTA under the browser chrome. Cap the hero to at most
four text elements (eyebrow **or** brand strip, headline, subtext, CTA row).

## 2. Sticky and pinned sections must not bleed

When a sticky element scrolls alongside a taller column of steps, its release timing
goes wrong and it overlaps the next section.

- Align the sticky element's height with each step's height. If the steps are
  `100vh`, size the sticky element in `vh` so it always fits inside one.
- Add a **trailing exit spacer** (about `40vh`) after the last step so the sticky
  element floats fully out of view before the next section's heading appears.
- Verify at the transition itself: the sticky element must be gone by the time the
  next heading is on screen.

`position: sticky` also silently does nothing when any ancestor has `overflow`
set to `hidden`, `auto` or `scroll`. If a sticky element is not sticking, walk its
ancestors before touching its own CSS.

## 3. A transparent fixed nav lets headings bleed through it

A fixed nav over a full-bleed hero is fine until a large heading scrolls behind it
and the two become unreadable.

- Past roughly 60px of scroll, make the bar opaque: a background at ~95% opacity
  plus a blur.
- **Drive that toggle from CSS**, not from a JS scroll listener - on a prerendered
  site the listener may never run. `animation-timeline: scroll()` handles it, and
  where that is unavailable the honest fallback is a nav that is opaque from the
  start rather than one that is unreadable half the time.
- The nav renders on **one line** at desktop, height 80px or less.

## 4. Screenshots are not flat alternating rows

Dropping raw app screenshots into alternating image/text rows is the most reliable
"generated" tell in a product page.

- Present a screen inside a frame that **floats**: a gentle CSS `translateY`
  keyframe (about 10px over 6s), a soft radial glow behind it, a rim light on the
  edge. All CSS, all working without JS.
- **Do not hand-build fake browser or phone chrome** - a URL pill with traffic-light
  dots is its own tell. A `<figure>` with a hairline border is enough.
- **Crop to the real frame bounds.** If the source image already has rounded
  corners, do not re-round with `border-radius` - it clips them flat. Use
  `filter: drop-shadow(...)` for the shadow rather than `border-radius` plus
  `box-shadow`. Watch for dead space on one side and clipped top or bottom bezels.
- Set `width`/`height` or `aspect-ratio` on every image so the page does not jump
  as assets load.

## 5. Dead gutters in two-column blocks

In a media-plus-copy block, a large empty gap between the two columns reads as a
mistake rather than as whitespace.

Align the media toward the **inner** edge (`justify-items: end` on the media cell)
so it sits near the copy, and let the negative space fall to the outer edge. The
page should have generous margins, not a canyon down its middle.

Use `grid-template-columns` with `fr` units. `width: calc(33% - 1rem)` breaks at
the first unexpected content length.

## 6. Motion that only exists when JS runs

Every animated element's **resting state lives in the markup**. Never set the final
state in `onMount` - the prerendered HTML then bakes the *start* frame: the empty
hero, the `$0` counter, the collapsed accordion.

Ask of every section: *with all JavaScript off, does this look finished?* If not,
move the final state into the markup and let motion enhance it.

- Animate only `transform` and `opacity`. Never `top`, `left`, `width` or `height`.
- Wrap non-essential motion in `@media (prefers-reduced-motion: no-preference)`, or
  reveal immediately on opt-out. Never trap content behind an animation.
- No `window.addEventListener('scroll')` - it re-runs every frame and kills mobile
  performance. Use `animation-timeline: view()` instead.
- No scroll-hijacking, no custom cursors, no mouse-follow.
- Enter animations are quick and confident. Nothing should make a visitor *wait* to
  read.
- One marquee per page, maximum.

## 7. Mobile floor

Verified at 320, 375, 414 and 768px. These are hard, not aspirational:

- **No horizontal scroll.** Set `overflow-x: clip` on both `html` and `body`, never
  `hidden` - `hidden` silently creates a scroll container and breaks `position:
  sticky` further up the page.
- **No two-line clickable text.** Buttons, nav links, footer links and CTAs wrap to
  one line or get shorter labels.
- **Image-bearing grid tracks use `minmax(0, 1fr)`**, never a bare `1fr` - a bare
  `1fr` refuses to shrink below the image's intrinsic width and forces a horizontal
  scroll.
- **Display headings need `overflow-wrap: anywhere` and `min-width: 0`** or a long
  unbroken word overflows the viewport.
- **Every asymmetric layout collapses to one clean column below 768px.**
- Tap targets are at least 44x44px, and hover-only affordances have a non-hover path.

## 8. The things that make a page feel unfinished

- A branded favicon, `<title>`, meta description, `og:image` and Twitter card tags.
- Alt text on every meaningful image; `alt=""` on decorative ones.
- Semantic landmarks: `<nav>`, `<main>`, `<section>`, `<footer>`.
- A skip-to-content link for keyboard users.
- Privacy and terms links in the footer where the business needs them.
- No dead links. A button pointing at `#` is either wired or visually disabled.
- Client-side validation on any form: email format, required fields, an error
  message that says how to fix it.
- Full state coverage on every interactive element: hover, `:focus-visible`,
  `:active`, disabled, and where relevant loading, empty and error.
- Fonts load with `font-display: swap` or are preloaded. No layout shift.

## 9. Look at it before calling it done

Most of the defects above are only caught by looking. Where a preview is available,
view the hero, every major section, and **every section-to-section transition** at a
desktop and a mobile width. The transitions are where sticky bleed and spacing
collapse actually show up, and they are the frames nobody screenshots.

If no preview is reachable, say that plainly rather than implying the page was
checked. An unverified page reported as verified is the one defect on this list that
costs trust rather than a revision.

## Before you finish

| Defect | Check |
| --- | --- |
| CTA below the fold | Hero fits at 1366x768; `100dvh` not `100vh` |
| Sticky element overlapping the next section | Heights aligned + ~40vh exit spacer |
| Sticky not sticking at all | An ancestor has `overflow` set |
| Headings unreadable behind a fixed nav | Opaque past ~60px, driven from CSS |
| Screenshots as flat alternating rows | Floating frame, real crop, no fake chrome |
| Canyon between media and copy | `justify-items: end`; space goes to the outer edge |
| Section empty with JS disabled | Resting state moved into the markup |
| Horizontal scroll on mobile | `overflow-x: clip`, `minmax(0, 1fr)`, `overflow-wrap: anywhere` |
| "Verified" without looking | Say it was not previewed |
