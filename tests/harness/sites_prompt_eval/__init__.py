"""SD-6 — the behavioural eval that gates every remaining /sites prompt cut.

New package 2026-09-08 (feat/sites-design-skills).

WHAT THIS IS FOR. ``docs/design/drafts/2026-09-08-sites-system-prompt-diet.md``
lists four slices that would take ~810 tokens out of a 14,094-token /sites create
turn, and gates all four on a measurement that did not exist: does the prompt
still produce the behaviour it claims after the cut? Its sibling plan for the chat
surface (2026-09-07, PO-1) gates its own three largest slices on the same missing
thing. Cutting a multi-model prompt on judgement alone is guessing, and on /sites
the thing being guessed about is the product.

So this package is the instrument. It does NOT change any prompt.

THE SPLIT THAT MAKES IT BUILDABLE WITHOUT A KEY. An eval has two halves and only
one of them needs a model:

  * PRODUCING the site  — needs a model, an API key, and money.
  * SCORING the site    — is deterministic string and CSS analysis, and is the
                          half that is easy to get quietly wrong.

Only the scorers live here as tested code. ``runner.run(produce)`` takes any
``produce(brief) -> str`` callable, so wiring a backend is one function and the
choice of provider stays outside this package — which is what lets the same
fixtures score every model in the routing table, as both plans require.

WHY THE SCORERS ARE THEMSELVES TESTED. A scorer that returns PASS on a defective
page is worse than no eval: it certifies a regression. ``test_scorers.py`` runs
every scorer against a hand-written PASSING sample and a hand-written FAILING
sample and asserts it separates them. That is the repo's "a gate is not a gate
until a mutation has been observed to break it" rule, applied to the gate itself,
and it runs in CI with no model and no key.

WHAT IT MEASURES. One behaviour per rule the prompt spends tokens producing, so a
cut that silently removes one is visible:

  vision-ledger        the Creative Direction Declaration is stated (MODULE 1)
  background           the page ground is not plain #fff / #000 (MODULE 2.B)
  no-em-dash           zero em/en dashes in VISIBLE text (MODULE 4)
  no-filler            no Elevate / Seamless / Unleash / Supercharge (MODULE 4)
  no-placeholder       no John Doe / Acme / Lorem ipsum (MODULE 4)
  measure-capped       body copy carries a max-width (design-taste 2.F, craft 1)
  not-centered-hero    the hero is not centred over a gradient (MODULE 5)
  no-three-equal-cards no `repeat(3, 1fr)` feature row (MODULE 5)
  floor-hit-area       interactive targets reach 44px (craft 5)
  floor-focus          a visible focus style survives (craft 5)
  floor-reduced-motion motion is gated on prefers-reduced-motion (craft 5)
  rotation             two briefs in one run do not resolve to the same accent
                       and the same nav (MODULE 2.G colour-consistency + the
                       repetition ban that `sites-theme-system` exists to check)

The last one is the only cross-fixture scorer, and it is the one a cut to
`sites-theme-system` or to the rotation language in PHASE 2 would break first.

HOW TO RUN IT. There is no baseline in this repo yet, deliberately: this branch
had no model credentials, and a fabricated baseline is worse than an absent one.

    from tests.harness.sites_prompt_eval import runner, fixtures
    report = runner.run(my_produce_fn)      # my_produce_fn(brief) -> site source
    runner.write_baseline(report, path)

Record the result as ``baseline.json`` beside this file, then re-run after each
cut and diff. A slice ships when its score is unchanged; it does not when the
score moves, however good the reasoning looked.
"""

from __future__ import annotations

__all__ = ["behaviors", "fixtures", "runner"]
