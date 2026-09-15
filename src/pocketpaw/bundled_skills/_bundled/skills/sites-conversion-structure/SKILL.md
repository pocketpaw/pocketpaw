---
name: sites-conversion-structure
description: |
  The STRATEGY layer for a Paw Site — what the page argues, in what order, and
  what single action it drives. Invoke it BEFORE writing any section markup on a
  marketing / landing site: "build a dentist landing page", "a page for my SaaS",
  "why is this page not converting", "what sections should this have". It decides
  the one offer / one audience / one action, runs the intake, SELECTS the
  sections this particular page needs, orders the argument, writes headline +
  CTA + benefit copy, handles objections, sets index / noindex + schema.
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
| Which sections this page needs, and in what order | **this skill** |
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

## 2. Page shape

The shape follows the offer. Settle it before the section list — this choice,
not the section count, is what makes two sites argue differently. It is a
working decision you hold in your head, never a label you read out to the user.

| Shape | Use when |
| --- | --- |
| **Hero-led** | The product is understandable from one hero visual. |
| **Long-form story** | You must educate and dismantle real skepticism before the ask. |
| **Minimal conversion page** | High-intent traffic (email to known users), or one narrow offer: waitlist, download, a single booking. |
| **Comparison page** | Search intent already includes alternatives ("X vs Y", "best X for Y"). |

**None of these is the default.** Hero-led is the one that gets picked by
reflex, so it is the one to justify hardest. A narrow offer wants the minimal
page, and a hero-led page over the top of it is padding: a waitlist page does
not need a benefits grid, a how-it-works and a twelve-question FAQ. It needs the
offer, one proof signal, and the field.

## 3. The argument

**Above the fold** - the part that is genuinely always there:
1. Headline - the outcome, plus who it is for.
2. Primary CTA - a verb plus what they get.
3. A subheadline where the headline needs a qualifier, not as a reflex.
4. One proof signal, only where the brief supplied one - a logo strip, one number, or one short quote. Omit the line rather than inventing proof (`pocketpaw-design-taste` MODULE 0).
5. A hero visual where you have a real one: real product, real place, real person.

**Everything below the fold is SELECTED, not filled in.** What follows is the
menu you select FROM, in the order these arguments generally land. It is not a
checklist, not a section count, and not a page template. Take what this offer
needs and leave the rest out. A section included because it appeared on a list
is the single most reliable way to produce a page that reads like every other
page.

- Problem -> solution, in the visitor's words.
- Benefits, outcome-led rather than feature-led.
- How it works, where the mechanism is genuinely unobvious.
- Social proof, placed **next to the claim it supports** rather than gathered
  into a wall - and only where the brief supplied real proof.
- Objections, as a section, where there are real ones worth answering.
- Risk reversal: trial, free plan, no card, cancel anytime, guarantee - where one
  of those is actually true of this business.
- Final CTA: the same promise and the same label as the top.

**The selection rule.** A section earns its place when it answers a question
this visitor would actually ask about THIS offer, or when it carries content the
brief handed you. If you can name neither, cut it. Three sections that each do a
job beat seven that fill a shape.

**Nothing on that menu is a fixture.** An FAQ ships when intake surfaced real
questions worth answering, at whatever number that turns out to be - not to
round the page out, and not at a target count. The same holds for a pricing
table, a testimonial band, a stats row, a logo strip and a newsletter box: each
ships where the brief asks for it or hands you the content that fills it, and
otherwise does not exist (design-taste MODULE 0).

This is the ARGUMENT, not a section-shape order. `pocketpaw-design-taste`
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

## 6. Copy craft

Section 5 gives the shapes. This is how the words inside them get written. All of
it applies to every visible string, including alt text, form labels, error text
and the footer.

**The order is Clarity, then Respect, then Character — always that way round.**
Without clarity copy fails; without respect it irritates; without character it is
merely forgotten. Most bad brand voice starts at character and never gets back to
clarity, which is why so many pages sound confident and say nothing.

**Dead words vs living words.** Dead copy is abstract, safe and interchangeable —
the signal is that it could sit on a thousand other sites unchanged.

| Dead | Living |
| --- | --- |
| "Innovative platform" | "Create a landing page in 20 minutes. No code." |
| "Next-generation solution" | "See where users drop off in checkout." |
| "Seamless experience" | "Export to PDF and Google Docs in one click." |
| "Powerful and flexible" | "Works with Gmail and Outlook. iCloud is next." |

The test: **if a word can be removed without changing the meaning, it was dead.**

**When a block feels empty, one of these six is missing** — context (where the
reader is), the job they are trying to do, what you offer, what changes for them,
the conditions or limits, and the next step. Check them in order before rewriting
by feel.

**Info-style.** Facts beat adjectives: "fast" becomes "in 10 seconds". Verbs beat
nouns: "configuration" becomes "set up". One sentence carries one idea. And
**the higher the stakes, the calmer the tone** — payments, deletion, security and
anything irreversible get plainer language, not more reassurance.

**Write scenes, not claims.** People remember images, not abstractions. "A better
workflow for teams" is a claim. "At 10:03 the PM drops a task. At 10:07 the
designer has real references and a draft" is a scene. The shape is *who + where +
what happens + what changes*. Use it where a benefit is hard to make concrete —
but never invent a customer, a name or a number to build one.

**Rhythm.** Flat rhythm loses attention: vary sentence length every line or two,
and let one paragraph land one punch.

> Fewer clicks.
> Clearer decisions.
> Less arguing in Slack.

**Give the page one quotable line.** Every page worth building has at least one
line that survives being screenshotted out of context. If nothing on the page is
quotable, nothing about it is memorable. Shapes that work: "Not another X.
Finally Y.", "Less noise. More decisions." One per page — a page where every line
strains to be the sticky one is exhausting.

**Headings describe, they do not aspire.** This is the most common failure in
section headings and in any dashboard or status UI a dynamic site renders.

| Aspirational | Descriptive |
| --- | --- |
| "Unlock Your Growth Potential" | "Revenue this month" |
| "Your Journey Starts Here" | "Onboarding progress" |
| "Insights That Matter" | "Search metrics" |

The test: **if someone scans only the headings, labels and numbers, do they
understand the page?** If not, the headings are decoration. Marketing warmth has
four legitimate homes — an empty state, onboarding, an upgrade prompt and a
success moment — and even there it is one line, then back to function.

**Errors say what happened, why if it helps, and what to do next.** Three rungs,
and most pages ship the first:

| | |
| --- | --- |
| Bad | "Something went wrong" |
| Better | "Couldn't save. Check your connection and try again." |
| Best | "You're offline. Reconnect to save changes." |

**Words that are banned outright**, because each one is a claim with no content
behind it. `pocketpaw-design-taste` MODULE 4 carries a short version of this list
and is in your context unconditionally; that one is the floor and this is the
full catalogue, so they extend each other rather than compete:

-   **Hype** — revolutionary, seamless, cutting-edge, best-in-class,
    next-generation, world-class, game-changing, disruptive, state-of-the-art,
    groundbreaking, and *innovative* / *powerful* / *robust* whenever no specific
    follows.
-   **Filler** — very, really, just, actually, basically, literally, simply,
    easily, highly, incredibly, extremely, absolutely, truly, totally.
-   **Corporate zombie** — leverage, synergy, ecosystem, paradigm, holistic,
    end-to-end, mission-critical, value proposition, stakeholder, thought leader,
    empower, unlock (metaphorical), drive (as in "drive growth"), and
    optimize / streamline without a number attached.
-   **AI openers**, which are the loudest tell of all — "In today's fast-paced
    world...", "In an era of...", "Look no further", "Say goodbye to...",
    "Introducing the future of...", "Reimagine...", "Supercharge your...",
    "Elevate your...", "Take your X to the next level", "Harness the power
    of...".

**Then cut.** Read the finished page and ask whether 30% could come out without
losing meaning. If it could, it should — and if cutting 30% *improves* the page,
keep cutting. A stranger should get the offer in about three seconds, find one
concrete detail, see the limits stated honestly, and know the next step without
looking for it.

## 7. Build order

Author section by section, in the order your selected argument runs.
**Never rebuild the whole page on each iteration.** Section by section keeps the
diff reviewable and keeps a bad section from taking a good one down with it.

## 8. Search and answer engines

- **Do not index** ad-only campaign pages and time-boxed offers. Set `noindex`.
- **Do index** evergreen offers where search intent matches the promise.
- Indexed pages need a real `<title>`, a meta description, Open Graph and Twitter
  card tags, a canonical URL, and any FAQ in plain question-and-answer markup.
- Add `FAQPage` structured data when a genuine FAQ exists. For a product or app,
  `SoftwareApplication` is appropriate. **Encode only true facts.** Never fabricate
  a rating, a review count or a price into structured data.
- A local business page carries `LocalBusiness` with the real name, address and
  hours, or it carries none. Half-invented structured data is worse than none.

## 9. Settle this before any code

Work these out before you write markup. This is a WORKING NOTE you keep to
yourself, not a script to read out - see "What you say to the user" below.

1. The one offer, the one audience, the one action.
2. The page shape (section 2), and one line on why this offer needs it.
3. The selected sections (section 3): what is IN, and what you deliberately
   left OUT. Name the cut, so you can tell that you made one.
4. Hero copy: headline, CTA, and a subheadline only if the headline needs one.
5. The copy for each section you selected.
6. index or noindex, plus title and meta if indexed.
7. Stated assumptions, one line each, for anything intake could not answer.

Then build section by section per section 7, under `pocketpaw-design-taste`.

### What you say to the user

**Never narrate the framework.** The shapes in section 2, the numbered argument
in section 3, the "Design Read", the "Vision Ledger" and the aesthetic direction
families in `pocketpaw-design-taste` are internal vocabulary. They are how you
think, not what you report.

A user who asked for a website did not ask to be told their page is
"Archetype A (classic hero + sections)". That line is worse than noise: it is
the SAME line on nearly every site, so the one thing it successfully
communicates is that the page came off a template. The same goes for announcing
a direction family ("a clean-tech identity"), reading the section list back as
an inventory, or quoting a module number.

Say one plain sentence about THEIR business and what the page does for it, then
build. *"A one-page site for the studio that leads with the work and pushes to
the enquiry form"* is the whole announcement. No labels, no letters, no
taxonomy.

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
| Twelve sections on a waitlist page | Cut to the offer, one proof signal and the field |
