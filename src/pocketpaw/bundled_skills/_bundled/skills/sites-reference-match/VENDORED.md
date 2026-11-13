<!-- Provenance for sites-reference-match: where the method came from, what was
     left out, and how to refresh it. Not served to the /sites agent. -->
# Vendored: sites-reference-match

`sites-reference-match` is the on-demand skill the /sites agent loads when the
user supplies a design to build to (a screenshot or mockup today, a Figma frame
once the Figma tools land). The method is adapted from an upstream skill; every
body line is a rewrite in our voice against the /sites toolset. Nothing is
copied verbatim.

## Sources

| Source | Licence | Commit | What it gave |
| --- | --- | --- | --- |
| github.com/Atharvsinh-codez/skills (Atharv Jadav), `design-to-code/SKILL.md` and `references/reference-forensics.md`, `verification.md`, `typography-and-fonts.md`, `interface-craft.md` | MIT, (c) 2026 Atharv Jadav | `f56c173a3c95` | Source modes and the authority order, the reference inventory, the observed / inferred / substituted ledger, the fidelity contract, glyph-based font identification and the commercial-to-open starting table, the seven responsive transforms, the correction order, the two-pass minimum, the four verdicts, the worst-case content table, and the product-track rules that went into `sites-craft` |
| github.com/emilkowalski/skills (Emil Kowalski), via `design-to-code/references/motion.md`, which credits it | MIT | via `f56c173a3c95` | Motion needs a purpose, and frequently triggered actions do not animate (contract item 7) |

## Where the rest landed

- `pocketpaw-design-taste`: one sentence in the precedence paragraph, "a
  supplied design outranks this skill".
- `sites-check`: the correction order, the two-pass minimum and the verdict ids
  (SC-REF-01 to 03).
- `sites-craft`: product-surface rules (work surface over chrome, the state
  matrix, stale-response guard), the responsive transform vocabulary, and the
  font ledger.

## Not ported

- The Geist Serif default. It would become a new house style, and Sites load
  faces from Google Fonts only, so the substitution table names Google faces.
- The image-generation art-direction flow. Refero and Inspo cover reference
  research on /sites.
- Every `.ref` script and the live-site measuring steps (`getComputedStyle`,
  `document.fonts`, SVG lifting): /sites denies `Bash` and the agent never
  copies a live site's assets.
- The six example sites under `websites/`. They reproduce third-party brands,
  so they never become templates or examples.
- The evaluation scenarios. They belong in our eval harness, not the skill.
- Greenfield and variant modes: an open brief is `pocketpaw-design-taste`'s job.

## Not verified

The font substitution table is a set of starting candidates, not measured
metric matches; the skill makes the agent compare line breaks before picking.
The correction order and the two-pass minimum are carried from upstream and
have not been re-derived against live Paw Site builds.

## To refresh

Re-fetch the upstream files at a newer commit and diff the sections named
above. Keep the file single and self-contained: /sites denies `Read`, so only
`SKILL.md` reaches the agent. Budget: 10,240 bytes
(`tests/test_sites_reference_match_skill_budget.py`).
