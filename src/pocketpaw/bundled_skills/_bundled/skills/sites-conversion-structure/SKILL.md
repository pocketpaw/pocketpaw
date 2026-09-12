---
name: sites-conversion-structure
description: |
  The STRATEGY layer for a Paw Site — what the page argues, in what order, and
  what single action it drives. Invoke it BEFORE writing any section markup on a
  marketing / landing site: "build a dentist landing page", "a page for my SaaS",
  "why is this page not converting", "what sections should this have". It decides
  the one offer / one audience / one action, runs the intake, picks a page
  archetype (A-D), orders the argument, writes headline + CTA + benefit copy to
  formula, handles objections, and sets the index / noindex + FAQ-schema call.
  It does NOT choose colors, type, or layout composition — `pocketpaw-design-taste`
  owns all of that and governs every visual decision. This skill owns the ARGUMENT;
  design-taste owns the LOOK. Use them together: structure first, then taste.
---

<!-- New file 2026-09-08: adapted from elayadesign/ai-design-skills (landing-page-design,
     Part A) into a Paw Sites strategy skill. Part B (its visual system) is deliberately
     NOT ported - pocketpaw-design-taste already governs the visual layer and the two
     disagree on several concrete values. See VENDORED.md for the divergence list. -->

# Sites: conversion structure

A landing page is not a homepage. A homepage serves many intents. A landing page
wins one:

**one offer -> one audience -> one primary action.**

Decide the argument here, in prose, before a single section is authored. Then hand
the look to `pocketpaw-design-taste`, which governs every visual value.

## Boundary with design-taste (read this first)

| Decision | Owner |
| --- | --- |
| Which sections exist, and in what order | **this skill** |
| What the headline claims, what the CTA promises | **this skill** |
| Which objections get a section | **this skill** |
| index / noindex, title, meta, FAQ schema | **this skill** |
| Palette, type, spacing, radius, materiality, motion | `pocketpaw-design-taste` |
| Layout composition and section-shape diversification | `pocketpaw-design-taste` |
| Anti-slop copy rules (no em-dash, organic metrics, no filler verbs) | `pocketpaw-design-taste` MODULE 4 |

Where this skill and design-taste appear to disagree on a visual value,
**design-taste wins** and this skill's version is out of scope. Where the user's
explicit brief disagrees with either, the user wins.

## 1. Intake

Gather these before designing. Ask only for what is genuinely missing, and ask in
ONE batch, never one question at a time.

**Purpose**
- What is the ONE primary action? (trial, demo, buy, waitlist, book, call)
- What is the offer, precisely - what does the visitor get?
- What counts as a conversion: a click, a form, a purchase?

**Audience and context**
- Who is the ideal customer?
- What problem are they solving today, and how?
- The top three objections - the real reasons they do not convert.
- Traffic source: ads, search, social, email, word of mouth.
- What does a visitor already know at the moment they land?

**Proof and assets**
- Proof points: logos, testimonials, numbers, case studies.
- Screenshots, demo video, product photography.
- Guarantees, refund terms, cancellation terms.

**Constraints**
- Brand voice, existing brand assets, mobile priority.

If the user cannot answer, **make a reasonable assumption, state it in one line,
and continue.** Do not stall the build on intake. On the /sites surface prefer the
`ask_user` chips for this rather than a wall of prose questions.

**Research the real business first.** Before assuming, use WebSearch / WebFetch on
the actual business. Real services, real neighbourhood, real hours and real
differentiators beat any invented intake answer, and they are what stops the page
reading as generic. Never fabricate a specific verifiable fact (an address, a
price, a named testimonial) - use an obvious placeholder and flag it.

## 2. Page archetype

Pick ONE and say why in the Design Read. This choice, not the section list, is
what makes two sites argue differently.

| Archetype | Use when |
| --- | --- |
| **A. Classic hero + sections** | The product is understandable from one hero visual. The common case. |
| **B. Long-form story** | You must educate and dismantle real skepticism before the ask. |
| **C. Minimal conversion page** | High-intent traffic (email to known users), or one narrow offer: waitlist, download, a single booking. |
| **D. Comparison page** | Search intent already includes alternatives ("X vs Y", "best X for Y"). |

Archetype C is routinely correct and routinely skipped. A waitlist page does not
need a benefits grid, a how-it-works and a twelve-question FAQ. It needs the offer,
one proof signal, and the field.

## 3. The argument, in order

**Above the fold (required)**
1. Headline - the outcome, plus who it is for.
2. Subheadline - how, with one specific.
3. Primary CTA - a verb plus what they get.
4. One proof signal, only where the brief supplied one - a logo strip, one number, or one short quote. Omit the line rather than inventing proof (`pocketpaw-design-taste` MODULE 0).
5. Hero visual - real product, real place, real person.

**The middle (the argument itself)**
6. Problem -> solution, as one section, in the visitor's words.
7. Benefits: three to five, outcome-led, not feature-led.
8. How it works: three steps, no more.
9. Social proof: testimonials or one case study, placed **next to the claim it
   supports**, not gathered into a wall at the bottom.

**The bottom (objections)**
10. FAQ: six to twelve real questions. Move it EARLIER for a high-friction offer.
11. Risk reversal: trial, free plan, no card, cancel anytime, guarantee. At least one.
12. Final CTA: the same promise and the same label as the top.

This is the ARGUMENT order, not a section-shape order. `pocketpaw-design-taste`
MODULE 3.A / 3.B still governs how each is composed, and its section-repetition ban
still applies: no two sections share a layout, no three-equal-card feature row,
no eyebrow on every section. On a page of five or more sections that works out
to four or more layout families; on a shorter page distinct shapes are the whole
requirement and a family count is not (MODULE 0).

## 4. Conversion rules

- **Match the message to the source.** If the traffic comes from an ad, the hero
  mirrors that ad's promise and visual tone. A mismatch here loses more
  conversions than any styling choice.
- **One primary action.** Never place competing CTAs above the fold. A secondary
  action may exist, visually subordinate, never equal in weight.
- **Benefit first, feature second.** A feature is what it does; a benefit is what
  that means for them. "Two-way calendar sync" is a feature. "Never double-book a
  chair again" is what it buys.
- **Be specific.** "Save time and streamline" says nothing. "Cut weekly reporting
  from four hours to fifteen minutes" is the same claim, made checkable.
- **Reduce risk explicitly**, beside the CTA, not in the footer.
- **Objections are a section, not a footnote.**
- **One label per intent.** "Get in touch" + "Contact us" + "Let's talk" on one page
  is a failure. Pick one and use it in the nav, the hero and the footer.

## 5. Copy formulas

**Headline**
- `{Outcome} without {pain}`
- `The {category} for {audience}`
- `{Result} in {time}`

Two lines at desktop, maximum. The headline carries the outcome; the subheadline
carries the qualifier. Do not stack both jobs into one sentence.

**Subheadline.** One or two sentences: what it is, who it is for, one specific.

**CTA.** A verb plus what they get. Never "Learn more", never "Submit".
"Start free trial", "Book a consultation", "Get the checklist".

**Benefit bullets.** Bold the benefit, then the proof or the mechanism:
**Faster iteration** - three layout variants from one brief.

All of `pocketpaw-design-taste` MODULE 4 applies to every string written here:
zero em-dashes, organic metrics, no filler verbs, no "John Doe", no "Acme".

## 6. Build order

Author section by section: hero, benefits, how it works, proof, FAQ, final CTA.
**Never rebuild the whole page on each iteration.** Section by section keeps the
diff reviewable and keeps a bad section from taking a good one down with it.

## 7. Search and answer engines

- **Do not index** ad-only campaign pages and time-boxed offers. Set `noindex`.
- **Do index** evergreen offers where search intent matches the promise.
- Indexed pages need a real `<title>`, a meta description, Open Graph and Twitter
  card tags, a canonical URL, and the FAQ in plain question-and-answer markup.
- Add `FAQPage` structured data when a genuine FAQ exists. For a product or app,
  `SoftwareApplication` is appropriate. **Encode only true facts.** Never fabricate
  a rating, a review count or a price into structured data.
- A local business page carries `LocalBusiness` with the real name, address and
  hours, or it carries none. Half-invented structured data is worse than none.

## 8. The output, before any code

When building a page from scratch, return these in order **before writing markup**:

1. Page archetype (A / B / C / D) and one line on why.
2. The one offer, the one audience, the one action.
3. Hero copy: headline, subheadline, CTA, proof line.
4. Benefits: three to five, outcome-led.
5. How it works: three steps.
6. FAQ: six to twelve real questions.
7. index or noindex, plus title and meta if indexed.
8. Stated assumptions, one line each, for anything intake could not answer.

Then build section by section per step 6, under `pocketpaw-design-taste`.

## Pitfalls that kill conversion

- Competing CTAs above the fold.
- A vague value proposition: "streamline", "optimize", "empower".
- A long feature list with no outcome attached to any of it.
- Proof buried at the bottom, away from the claim it supports.
- A FAQ that answers questions nobody asked and dodges the price.
- No clear next step at the end of the page.
- A form asking for six fields when the offer justifies one.

## Before you finish

| Mistake | Fix |
| --- | --- |
| Sections chosen before the offer was named | Name the one action first, then cut every section that does not serve it |
| Benefits that restate features | Rewrite each as what it means for the visitor |
| Proof gathered into one wall | Move each proof beside the claim it supports |
| FAQ that avoids the real objection | Put the top-three objections from intake into it, near-verbatim |
| Structured data with invented numbers | Remove the field, or ship the page without schema |
| A campaign page left indexable | Set `noindex` |
| Twelve sections on a waitlist page | Switch to archetype C and cut to four |
