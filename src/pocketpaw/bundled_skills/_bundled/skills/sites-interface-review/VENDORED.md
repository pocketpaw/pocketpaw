<!-- New file 2026-09-08: provenance + refresh recipe for the adapted sites-interface-review skill. -->
# Vendored: sites-interface-review

- **Source:** https://github.com/jakubkrehel/skills (MIT, Jakub Krehel) — the
  `better-typography`, `better-colors`, `better-layout`, `better-ui`,
  `better-accessibility`, `better-writing` and `better-interface` skills
- **Vendored:** 2026-09-08 from commit `267330e1adfc`
- **License:** MIT. Attribution retained here; the skill body is a rewrite rather
  than a copy.

## What was ported

Seven upstream skills across 40+ files, consolidated into **one** self-contained
`SKILL.md`:

- the **six domain rulesets**, with their exact numeric values kept verbatim because
  upstream is explicit that they are values and not ranges — `scale(0.96)` on press,
  icon transitions at scale `0.25`→`1` / blur `4px`→`0`, image outlines at
  `oklch(0 0 0 / 0.1)`, the 2x inter-group to intra-group spacing gap, 1.5px icon
  stroke beside 400-weight text and 2px beside 600, ~100ms entrance stagger,
  60–75ch measure, 44x44px hit areas;
- the **review protocol** from `better-interface`: resolve scope, recon before
  judgment, rank by user impact, prefer the cheaper fix, consolidate systemic
  findings, cap pre-existing issues at three, do not mutate by default;
- the **evidence rule and the review output format**, including the coverage table
  and the `Not reviewed` status;
- the severity definitions (`HIGH` / `MEDIUM` / `LOW`);
- the "measure the rendered pair, report it, then leave the colours alone" rule,
  which is the right posture for a skill reviewing someone else's brand.

## What was deliberately NOT ported, and why

- **The seven-skill structure and its 33 reference subfiles**
  (`css-cheat-sheet.md`, `palette-generation.md`, `contrast.md`,
  `surfaces.md`, `enter-exit.md`, …). Upstream cross-references these constantly and
  routes between skills by domain ownership. **Neither mechanism survives on
  /sites**: `_SITES_BUILTIN_DENY` in
  `ee/pocketpaw_ee/cloud/surface/surface_registry.py` strips `Read`, `Glob` and
  `Bash` from every /sites mode, so a subfile is unreachable, and pocketpaw's
  bundled-skill installer flattens one `SKILL.md` per directory. Every cross-skill
  "belongs to `better-colors`" pointer was therefore resolved inline, and the rules
  each subfile carried were either lifted into the body or dropped.
- **The `break`, `variant` and `explain-interface` skills.** `break` renders a
  component in every state on a temporary harness page and `explain-interface`
  reverse-engineers a live page — both need a filesystem and a browser. `variant` is
  a generation skill, not a review one.
- **`better-accessibility`'s full checklist.** Compressed to the eleven items that
  actually bite on a marketing page. The upstream skill is written for product UI
  with dialogs, menus and live regions; a landing page has fewer of those.
- **The `agents/openai.yaml` files** in each upstream skill — a different runtime's
  packaging.

## Local edits

- Foregrounded the **honesty rule** as the first section rather than a mid-file
  principle. Upstream assumes a browser is usually available; on /sites it usually is
  not, and the failure mode this skill most needs to avoid is a confident review of a
  page nobody looked at. `Not reviewed: no render available` is now the expected
  outcome for whole domains rather than an edge case.
- Added the pointer framing it as the corrective counterpart to
  `pocketpaw-design-taste` so the two are not read as competing.

## Why it ships bundled

The sites stack had no review axis at all. design-taste's MODULE 6 is a self-check
run by the same agent that just authored the page, in the same turn — useful, but it
is not an independent read, and it produces a pass/fail rather than ranked findings
with evidence.

## Not verified

No review has been run under it. In particular, the coverage table's usefulness
depends on the agent honestly marking domains `Not reviewed` when it could not
observe them, and that behaviour has not been exercised.

## To refresh

Re-fetch the seven upstream `skills/better-*/SKILL.md` files plus
`better-interface/review-format.md`. Check upstream's numeric values against the ones
inlined here — upstream states they are exact, so a drift matters. Before porting any
reference subfile, confirm whether /sites still denies the file built-ins.
