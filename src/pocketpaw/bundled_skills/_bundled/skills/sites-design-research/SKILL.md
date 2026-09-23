---
name: sites-design-research
description: |
  Ground a page in REAL shipped design systems before designing it, using the
  Refero research tools. Invoke when you are about to choose a visual direction
  and the brief gives you room to choose — a new landing page, a redesign, a
  "make it look premium / editorial / technical" ask, or a second site that
  must not resemble the first. It is the RESEARCH step that feeds
  `pocketpaw-design-taste`: this skill finds and locks a direction with
  evidence, that one builds the page from it. Also the honest answer to "what
  should this look like?" when the brief does not say. Do NOT invoke it for a
  copy edit, a single value change, or when the user supplied a design to match
  — there is nothing to research. Returns nothing when Refero is unconfigured,
  which is a normal outcome, not a failure.
---

# Design research

Adapted from the Refero Skill (`referodesign/refero_skill`, MIT) — the
research-first methodology, rewritten for this surface and this toolset. The
craft half of that skill is deliberately NOT reproduced here: typography,
colour, motion, icons, copy and the AI-tell index already live in
`pocketpaw-design-taste`, `sites-craft`, `sites-theme-system` and
`sites-restraint`, and a second competing design system in the same prompt is
worse than none.

## Where this sits

This skill is **not** the design authority. It supplies evidence; the authority
stays where it is.

| Question | Owner |
| --- | --- |
| What should this look like, and on what evidence? | **this skill** |
| How is the page built, scoped and kept from looking AI-made? | `pocketpaw-design-taste` (MODULE 0 still governs scope) |
| What does the page SAY, and in what order? | `sites-conversion-structure` |
| How do I hand-write this ground / shader / type choice? | `sites-design-sources` |

Research first, then hand the locked direction to the design system. Never let
a reference override the brief: if the brief did not ask for a section, no
reference justifies adding one.

## The tools

Two research layers. Names differ by backend — use whichever you actually have:

| Layer | MCP surface | elsewhere |
| --- | --- | --- |
| Visual direction | `search_styles`, `get_style` | `refero_design_styles` |
| Concrete UI | `search_screens` | `refero_design_screens` |

**Styles are the layer that matters.** A search result is only a description;
`get_style` is the call that returns something to build from — a north-star
thesis, colours WITH THEIR ROLES, a type scale, spacing, elevation, component
treatments, imagery guidance, and explicit do/don't rules.

There is no flows layer on this surface. Do not go looking for one.

### When it returns nothing

Refero needs a paid plan, so an empty result is the **normal** outcome on an
unconfigured deploy — not an error and not something to retry. Proceed on your
own design judgement under `pocketpaw-design-taste`, and say nothing about
Refero to the user. Never cite a reference you did not receive, and never name
a company as your source because it sounded plausible.

## The loop

1. **Search several angles before choosing.** Three to five for a new page.
   Vary the axis, do not re-word the same query: one aesthetic
   (`editorial monochrome SaaS landing page`), one domain
   (`warm trustworthy healthcare marketing`), one named product
   (`Linear dark developer tool`). Stopping at the first good hit is how every
   page ends up in the same house style.
2. **Expand the strongest two or three** with `get_style`.
3. **Add screens only when structure is the question** — "what goes on a
   pricing page", "how is a testimonial wall usually built". Search by what is
   ON the screen, not by adjective.
4. **Lock one direction** (below) before writing any markup.

Depth follows risk. A small visual improvement earns two or three searches and
one expanded style. A new landing page or a redesign earns three to five
searches, three or four expanded styles, and screen research for the sections
you are unsure about.

## The three rules that make research worth doing

**Do not copy one reference.** A single style reproduced is a clone of someone
else's brand wearing your client's name.

**Do not average.** When two references disagree, blending them produces the
safe centroid — which is exactly the generic output the research was meant to
escape. Pick ONE dominant direction, keep its sharp traits, and borrow only
narrow, named details from the others.

**Preserve roles.** This is the rule most often lost. If a style marks a colour
CTA-only, a face display-only, or a surface elevated-only, it keeps that role
or it does not ship. A palette flattened into "five nice hex values" smeared
across the page is not the reference you researched. Same for media: if the
direction depends on photography or illustration, honour it with real or
generated assets or an intentional art-directed placeholder — never fake it
with a decorative gradient box.

## Lock it before you build

Write the lock down before authoring. Two short artefacts, in your reasoning —
not on the page:

**Reference lock** — one line naming the primary direction and the signature
traits that must survive contact with the brief.

```
Primary: Depot (dark, high-contrast developer tool) — keep the near-black
canvas, the thin cool borders, and code-set monospace as an accent only.
Borrowed: framed product-screenshot panels from a second reference. Nothing else.
```

**Decision ledger** — every major choice traced to something.

| Decision | Source | Role to preserve | Why |
| --- | --- | --- | --- |
| Near-black canvas | primary style | surface, not accent | brief asks for "serious, technical" |
| One accent hue, CTA only | primary style `dos` | CTA-only | keeps the single page action loud |
| Framed screenshot panels | secondary style | media role | the product IS the proof here |

**If a major choice has no source, it is not a design decision yet.** Research
further, tie it to something the user actually said, or drop it. That test is
the whole point of this skill: it converts taste into something reviewable.

## Before you hand the page over

- Can I name the references that shaped this, and what came from each?
- Did I keep one direction's sharp traits rather than averaging several?
- Did every colour, face and surface keep the role its source gave it?
- Does every major choice trace to a reference, the brief, or a craft rule?
- Did I stay inside the brief — no section a reference talked me into?

A "no" anywhere means research or cut, not ship.

Keep the user-facing summary short when the task was non-trivial: the direction
you locked, what you borrowed, and why it fits their product. Do not paste the
ledger or dump search results at them.
