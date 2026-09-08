---
name: sites-theme-system
description: |
  Makes two Paw Sites built by the same agent look like DIFFERENT SITES, not
  colour-swaps of one template. Invoke when building a second (or fifth) site,
  when a page "looks like the last one", or whenever you are about to pick a
  theme, a nav, or a footer. It supplies the three things `pocketpaw-design-taste`
  asserts but cannot check: a THEME-DIFFERENCE TEST on three measurable axes
  (paper band / display style / accent hue), a NAV + FOOTER archetype catalogue
  to rotate through, and a LOCKED-TOKEN rule that forbids mid-render colour and
  font improvisation. It also carries the honest-copy and pre-emit self-critique
  gates. It does NOT replace design-taste's palettes, families or layout rules —
  it is the diversification and token-discipline layer on top of them.
---

<!-- New file 2026-09-08: adapted from Nutlope/hallmark (SKILL.md design flow, steps
     2-2.6, plus its anti-patterns and slop-test gates) into a Paw Sites diversification
     and token-discipline skill. Hallmark's 21-theme catalog, 21 macrostructures and
     53-gate slop test are NOT ported wholesale - design-taste already carries six
     aesthetic families and a pre-flight checklist. What is ported is the machinery
     design-taste lacks: the measurable difference test, archetype rotation, and the
     project-memory stamp. See VENDORED.md. -->

# Sites: theme system and diversification

`pocketpaw-design-taste` tells you to vary. This skill is how you prove you did.

The failure it exists to stop: an agent picks the genre default every time, so
eight sites ship two navs, one footer and three palettes. Each site passes its own
review. Together they are one template.

## The rule underneath everything

**Variety is structural, not chromatic.** Two sites for two different briefs must
not share the same section rhythm. Recolouring one template is the failure, not
the fix.

## 1. Project memory (do this before picking anything)

Every site this skill builds carries a stamp in a comment at the top of its main
stylesheet or root component:

```css
/* Paw Site · theme: <name> · paper: <dark|mid|light> · display: <style> · accent: <hue>
 * nav: <archetype> · footer: <archetype> · macro: <section rhythm>
 * built: <YYYY-MM-DD>
 */
```

Before you pick, **read the stamp on the most recent site you built** (on the
/sites surface: the previous site in this conversation, or the pocket you are
refining). If you have built any other site in this session, the new pick must
differ from it. If you cannot see a previous stamp, say so in one line and pick
freely - an unverifiable claim of variety is worse than an honest "first build".

## 2. The theme-difference test (three axes)

Picking a different accent colour is not variety. Two consecutive sites must
differ on **at least two of these three axes**:

**Paper band** - the lightness of the page ground.
- `dark` - L < 30%
- `mid` - L 30-85%
- `light` - L > 85%

**Display style** - the character of the headline face.
- `high-contrast-serif` · `roman-serif` · `geometric-sans` · `grotesk-sans`
- `rounded-sans` · `mono` · `condensed-display` · `heavy-display`

**Accent hue** - the family of the one accent.
- `warm` (10-60°) · `cool` (200-300°) · `neutral` (no chromatic accent)
- `other-chromatic` (green, teal, phosphor)

**State the test out loud before writing markup:**

> Previous: dark · grotesk-sans · cool. This build: light · high-contrast-serif ·
> warm. Differs on all three.

If a candidate differs on only one axis, it is too close. Pick again. This maps
directly onto design-taste's six aesthetic families (MODULE 2.E) - use the family
for the full token system, and use these axes to check the family choice was
actually a change.

## 3. Nav archetypes - rotate, never default

The nav is a structural fingerprint, not chrome. Pick deliberately and record it.

| # | Archetype | Reach for it when |
| --- | --- | --- |
| N1 | Minimal 2-link | The page genuinely has two destinations |
| N2 | Canonical three-section (product / company / CTA) | A real multi-page product site |
| N3 | Floating chip | Editorial or portfolio, content-led |
| N4 | Side rail (vertical) | Long single-page scroll, lots of vertical room |
| N5 | Floating pill, detached from the top | Premium consumer, soft-premium family |
| N6 | Masthead (wordmark centred, links below a rule) | Editorial, publication, heritage |
| N7 | Brutal slab (full-width, hard border) | Brutalist / structural family |
| N8 | Terminal bar (mono, monospaced links) | Dark-tech / infra |
| N9 | Edge-aligned (logo hard left, CTA hard right, nothing centred) | Clean-tech, dense product |
| N10 | Scroll-morph (transparent over hero, solid after) | Any site with a full-bleed hero |

**Default away from N1.** A wordmark, two inline links and a right-hand button is
the single most recognisable generated-site fingerprint. Reach for N1 only when
the site really has two destinations.

## 4. Footer archetypes - same discipline

| # | Archetype | Reach for it when |
| --- | --- | --- |
| Ft1 | Statement (one large line + one CTA) | Marketing pages with a single action |
| Ft2 | Two-column (contact left, links right) | Local business, service business |
| Ft3 | Four-column link farm + social row | A genuine docs root or hub, and almost nowhere else |
| Ft4 | Hairline minimal (wordmark + copyright on one line) | Minimal / archetype-C conversion pages |
| Ft5 | Oversized wordmark (brand set huge, links small above) | Brand-led, soft-premium, portfolio |
| Ft6 | Contact card (address, hours, map link) | Anything with a physical location |

**Default away from Ft3.** Four columns of links plus a social row plus a tiny
copyright is the footer equivalent of N1.

**Before writing any nav or footer markup, state one line:**

> Previous nav: N9. This build: N5, because the family is soft-premium and the
> hero is full-bleed.

This one line is the most-violated rule in practice and the cheapest to keep.

## 5. Locked tokens - no mid-render improvisation

Once the theme is chosen, **every colour and every `font-family` in the artifact
references a named token.**

```css
/* correct */
color: var(--ink);
background: var(--surface-raised);
font-family: var(--font-display);

/* violations */
color: #2f3437;                  /* inline hex bypassing the token block */
background: oklch(0.21 0.02 250);/* inline oklch */
font-family: "Space Grotesk";    /* font declared outside the token block */
```

If a value is needed that has no token, **lift it into the token block as a new
named variable first**, then reference it. The reason is not tidiness: an inline
value is invisible to any later restyle, so a refine pass that changes the accent
leaves orphan colours scattered through the page, and the site drifts every time
it is edited.

## 6. Honest copy - no fabricated proof

If the user did not supply a number, do not invent one. This is stricter than a
style rule because an invented metric is a false claim shipped on a real business's
domain.

- Stat bars, comparison rows and proof strips use **real numbers**, a labelled
  placeholder (`—` with "metric to confirm"), or **a different section shape**.
- `+47% conversion`, `trusted by 50,000+ teams`, `10x faster` are fabrications the
  moment they are invented, however plausible they read.
- Same rule for testimonials, customer logos and case-study counts. A logo strip
  of companies that are not customers is not a design choice.
- When the brief has no proof, **change the macrostructure** rather than filling
  the proof section with invention. A page with no testimonials section is honest;
  a page with three invented ones is not.

This composes with design-taste MODULE 4 (organic metrics, no "John Doe", no
"Acme"): that rule says make numbers *look* real, this one says do not make them
up at all.

## 7. Re-drawn chrome is forbidden

Do not hand-build fake browser bars (a URL pill plus traffic-light dots), fake
phone frames, fake code windows (a mock title bar wrapping a `<pre>`), or fake IDE
chrome. Use a real screenshot in a `<figure>` with at most a hairline border, or
let the content stand on its own. Fake chrome is a reliable generated-site tell and
it dates instantly.

## 8. Typography purity - no italic headings

Headings and display type are roman (`font-style: normal`). An italicised emphasis
word inside an upright heading (`Built to <em>think</em>`) is one of the most
reliable tells, and so is an all-italic display face. Carry emphasis with weight,
accent colour, or a drawn underline. Italic survives only as body-copy emphasis
inside running paragraphs.

## 9. Pre-emit self-critique

Before handing back the page, score it 1-5 on six axes and **state the scores**:

| Axis | Asks |
| --- | --- |
| Philosophy | Is there one idea, or a pile of sections? |
| Hierarchy | Does the eye land where the conversion is? |
| Execution | Would this survive a look at 1440 and at 375? |
| Specificity | Could this copy belong to any other business? |
| Restraint | What could be removed with no loss? |
| Variety | Does it pass the section-repetition ban? |

Anything scoring **below 3 triggers a revision pass before emit**, not a note in
the summary. Stamp the scores into the CSS comment beside the theme stamp.

## Before you finish

| Mistake | Fix |
| --- | --- |
| Theme differs from the last on one axis only | Pick again; two of three axes minimum |
| Nav/footer picked as the family default | Read the stamp, rotate, state the line |
| Inline hex or font-family in a section | Lift it into the token block, reference the token |
| An invented stat in a proof bar | Real number, labelled placeholder, or a different section |
| Fake browser chrome around a screenshot | Real screenshot in a `<figure>`, or no frame |
| Italic display heading | Roman; carry emphasis with weight or accent |
| Self-critique skipped or scored after emit | Score before emit; below 3 means revise first |
| No previous stamp, variety claimed anyway | Say "first build, no prior stamp" and pick freely |
