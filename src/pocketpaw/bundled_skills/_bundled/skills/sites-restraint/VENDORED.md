<!-- New file 2026-09-08: provenance + refresh recipe for the adapted sites-restraint skill.
     Updated 2026-09-08: the licensing question was put to the captain and answered
     (option 2, keep as written). The section below is kept as the record of what was
     checked and what was decided, not as an open blocker. -->
# Vendored: sites-restraint

- **Source:** https://github.com/ericzakariasson/scandinavian-design —
  `skills/scandinavian-design/SKILL.md`
- **Vendored:** 2026-09-08 from commit `295742307b97`

## License — no license upstream, decided 2026-09-08

**Checked 2026-09-08 on commit `295742307b97`:** the repository has **no `LICENSE`
file, no `COPYING` file, and no `license` field in `package.json`.** Under default
copyright that is all-rights-reserved, not permissive. The other four sources in this
batch are MIT and carry attribution; this one does not have that footing.

**Decision: keep it as written** (option 2 of the three below). Put to the captain on
2026-09-08 with the exposure spelled out, and cleared. Recorded here rather than
deleted, because the reasoning is what a later reader needs and because the facts
change the moment upstream adds a license.

The options as they were put:

1. **Ask upstream to add a license** (an issue or a PR adding MIT). Cleanest, and
   still worth doing — it would move this from a judgement to a fact.
2. **Keep it as written.** ← chosen. The body was authored as a rewrite: the
   *structure* is ours, and what is carried over is largely measured values and
   functional rules (contrast ratios, alpha percentages, the channel-spread
   threshold), which are facts rather than protected expression. Several judgment
   rules are close paraphrases, and that is the exposed part.
3. **Drop the skill** and keep only the numeric ladder inside
   `pocketpaw-design-taste` MODULE 2.E family D.

**What that decision does not do:** it does not make the source permissive. If this
material is ever published outside the product — a docs site, a public skill
marketplace, a blog post quoting it — re-open option 1 first. Attribution to
ericzakariasson stays in this file either way.

## What was ported

- The **alpha-ink ladder** for a light canvas, and the rule to set ink with alpha
  colours rather than the `opacity` property.
- The **dark-canvas inversion**, which is the most valuable part and the part most
  often got wrong: the same alpha buys more contrast on dark, so 64%/44% become
  56%/36%, interaction fills go up about 1.5x, and the tertiary rung is sensitive to
  how dark the canvas actually is (fine at `#0A0A0A`, failing by `#262626`).
- The **channel-spread test** for colour casts (correct at ~8 points, leave below 5).
- The **do-not-neutralize rules** — brand marks, signature datum colours,
  third-party marks acting as row headers, small marks below the recognition size
  floor, and legally-placed disclosures. This is the section that separates a
  restraint pass from a decolorizing pass, and nothing in design-taste covers it.
- The "some surfaces are correctly loud" caveat, which keeps the skill from being
  applied as a default.

## What was deliberately NOT ported, and why

- **Its Invocation Modes (Apply / Review / Prototype / Deep) and its Recon step.**
  These assume the agent can inspect an existing codebase. `_SITES_BUILTIN_DENY`
  strips `Read`, `Glob` and `Bash` on every /sites mode, so a source-inspection
  workflow cannot run there. The review axis lives in `sites-interface-review`
  instead.
- **Its browser-verification harness** (`scripts/lines.js`, `scripts/tints.js`,
  `scripts/run-eval.js`, and the Browser verification section). Those are Node
  scripts run against a live page; /sites has no shell and bundled-skill scripts are
  not executed by pocketpaw's installer.
- **Its Escalation Triggers / Remediation Order / Review Output sections**, which are
  shaped around redesigning an existing third-party site rather than authoring a new
  one.

## Local edits

- Restructured into ladder → dark inversion → cast test → do-not-neutralize →
  layout, and rewritten in our own words.
- Added the explicit "when restraint is the wrong answer" opening so it reads as a
  *direction* to be chosen, not a default to be applied.
- Added the pointer to design-taste families C and D so it composes rather than
  competes.

## Why it ships bundled

design-taste family D (Warm-Minimalist) is four lines and carries no numbers. A
quiet page is the most commonly requested register and the easiest to get subtly
wrong, and the dark-inversion math in particular is not something a model reliably
derives.

## Not verified

No page has been built under it. The contrast figures quoted (8.2:1, 6.7:1, 4.66:1,
4.53:1) are carried over from upstream and **have not been independently
re-measured** — re-measure before treating any of them as a compliance claim.

## To refresh

Re-fetch upstream `skills/scandinavian-design/SKILL.md`. **Re-check the license
status first** — if a LICENSE has appeared, record it here and the section above
becomes history rather than a judgement call.
