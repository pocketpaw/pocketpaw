---
# Updated 2026-09-27 (feat/sites-visual-research): the loop now OPENS the
# screenshots (view_reference / view_reference_screenshot), researches per
# section, lets the locked reference set the look with roles kept, and ends
# with preview_site on the draft. Matches PHASE 1b in the /sites preamble.
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

Research first, and the locked reference sets the look; the design system
fills what it leaves open and supplies the craft. Never let a reference
override the brief: if the brief did not ask for a section, no reference
justifies adding one.

## The tools

Two archives. Names differ by backend — use whichever you actually have:

| Job | Refero (when configured) | Inspo (always there) |
| --- | --- | --- |
| Find a direction | `search_styles`, then `get_style` | `research_page_design` |
| Find real screens for one section | `search_screens` | `research_page_design` with a section brief |
| **See it** | `view_reference` (`screen_id` or a style `preview_url`) | `view_reference_screenshot` (`slug`, `view`) |
| Real values from the locked reference | `get_style` | `get_reference_design_system` |

**The picture is the layer that matters.** A search result is a description, and
descriptions miss exactly the things that decide whether a page looks designed:
how the fold is built, whether the product is shown, how light the page is,
where colour is actually spent. A rejected page and an approved one were built
from the same brief; the difference was that the second time the screenshots
were opened, and they all shared one pattern no write-up mentioned.

### When it returns nothing

Refero needs a paid plan, so an empty Refero result is normal; fall back to
Inspo. If both are unavailable, proceed on your own judgement under
`pocketpaw-design-taste`, and say nothing about research to the user. Never cite a
reference you did not receive, and never name a company as your source because
it sounded plausible.

## The loop

1. **Search several angles before choosing.** Two or three for the overall look,
   varying the axis rather than rewording: one aesthetic
   (`modern light waitlist, bold typography`), one about products like this one
   (`AI creator tool landing page with product visual`), one named product the
   brief evokes.
2. **Search per section** you are unsure about, by what is ON the screen:
   `join the waitlist hero`, `social proof avatars counter`,
   `bento features video editing`.
3. **Open the 3-5 strongest** with the view tool and look for what they share.
4. **Lock one direction** (below), then pull real values from it with
   `get_style` / `get_reference_design_system`.
5. **Look at your own draft** with `preview_site` once it verifies, at desktop and
   phone width, next to the references. Fix what is off and look again.

Depth follows risk. A small visual change earns a search or two and one opened
reference. A new page or a redesign earns the whole loop.

## The three rules that make research worth doing

**Do not clone one reference.** Lead with one, but a single site reproduced
whole is someone else's brand wearing your client's name.

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

- Did I open the references, not just read about them?
- Can I name the references that shaped this, and what came from each?
- Did I keep one direction's sharp traits rather than averaging several?
- Did every colour, face and surface keep the role its source gave it?
- Does every major choice trace to a reference, the brief, or a craft rule?
- Did I stay inside the brief — no section a reference talked me into?
- Did I look at my own draft with `preview_site`, and does it hold up next
  to the references?

A "no" anywhere means research or cut, not ship.

Keep the user-facing summary short when the task was non-trivial: the direction
you locked, what you borrowed, and why it fits their product. Do not paste the
ledger or dump search results at them.
