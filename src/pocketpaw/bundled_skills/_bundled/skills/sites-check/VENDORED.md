<!-- Provenance and refresh recipe for sites-check, the merged defect skill. Not served to the /sites agent. -->
# Vendored: sites-check

`sites-check` replaces two skills, `sites-ship-fixes` (the author's fix pass) and
`sites-interface-review` (the read-only review), plus the self-score section of
`sites-theme-system`. They checked the same page against the same defects, one
as the author and one as a reviewer, so they are now one file with two modes
and one stable id per defect (`SC-<GROUP>-<NN>`). The ids exist so that a
review, the eval harness and the planned deterministic checks in `verify_site`
can all name the same rule.

Every body line is a rewrite in our voice. Nothing is copied verbatim. All
sources below are MIT, Apache-2.0, CC BY / CC BY-SA, or cited for an idea only.

## Sources carried over from the merged skills

| Source | Licence | Commit | What it gave |
|---|---|---|---|
| github.com/blackbrainpy/designs-by-kore (Designs by Kore / KORE INDUSTRIES), `plugins/designs-by-kore/skills/designs-by-kore/SKILL.md` | MIT | `5a82b1ee85ed` | The ship-it checklist: hero fits the fold and its test viewports, sticky bleed and the ~40vh exit spacer, fixed-nav bleed-through, screenshot presentation, the dead gutter, the mobile floor, motion hygiene, verify by looking (SC-BUILD-02 to 09, SC-BUILD-15, SC-MOBILE-01 to 05, SC-LAYOUT-08) |
| github.com/referodesign/refero_skill (Refero), `skills/refero-design/references/anti-ai-slop.md` | MIT | `a9b54a3e62a6` (v1.0.2) | The removal litmus tests (SC-SCORE-02) |
| github.com/jakubkrehel/skills (Jakub Krehel), `better-interface`, `better-accessibility`, `better-writing` | MIT | `267330e1adfc` | The review protocol (scope, recon, rank, cheaper fix, consolidate, cap pre-existing at three, read-only), the evidence and `not verified` rule, the coverage table and severity scale, the accessibility checks (SC-A11Y-*), the interface-writing line (SC-COPY-21) |
| github.com/miqdadbadjuber/anti-slop (Miqdad Badjuber), R-24/R-26 and `skills/antislop-copywriting` | MIT | `439a2ef5b9dd` | Dead controls (SC-LINK-01 to 05) and the generated-copy tells: negative parallelism, aphorisms, staccato runs, forced triads, inflated stakes, weasel attribution, chat voice, fabricated specifics (SC-COPY-02, 08 to 11, 16; SC-HONEST-01) |
| github.com/Nutlope/hallmark (Hallmark contributors), `skills/hallmark/SKILL.md` | MIT | `13ac0ec7e148` | The six-axis self-score with below-3-means-revise (SC-SCORE-01), the fake-chrome ban (SC-LAYOUT-01), honest proof sections (SC-HONEST-01/02) |

## Sources added with the merge

Rules marked with a catalog id (P, H, L, T, V, I, M, MB, A, S, R) come from the
verified external catalog in the workspace design doc
`docs/design/drafts/2026-10-21-sites-anti-slop-skill-redesign.md`, section 4.3.

| Source | Licence | What it gave |
|---|---|---|
| github.com/blader/humanizer | MIT | Strength ranking, weak signals never convict alone, fact preservation on rewrite (Severity and strength, SC-HONEST-05) |
| github.com/hardikpandya/stop-slop | MIT | Staged reveals and the throat-clearing list (SC-COPY-04, 05) |
| github.com/coreyhaines31/marketingskills, ai-tells | MIT | `[NEED: proof]` and declining a claim, keyword-stuffed headings and robotic meta (SC-HONEST-06, SC-COPY-12) |
| en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing | CC BY-SA 4.0, paraphrased and attributed | Era-dated vocabulary, copula avoidance, captions that narrate, chatbot residue (SC-COPY-03, 06, 13, 18) |
| Kobak et al. 2025, arxiv.org/abs/2406.07016 | CC BY | The measured excess-vocabulary cluster (SC-COPY-13) |
| github.com/sam-paech/antislop-sampler | Apache-2.0 | Promptonym names and brochure phrases (SC-HONEST-03, SC-COPY-14) |
| The Ghost Couple, arxiv.org/abs/2606.02184 | cited | The default-name list (SC-HONEST-03) |
| a16z crypto, "AI writing hallmarks for founders", and immeasurablematt/a16z-anti-slop | article reserved (paraphrase only); skill MIT | "If it passes, stop", legal text stays plain, announcement filler and hedges (SC-SCORE-03, SC-COPY-17, the COPY scope line) |
| github.com/discountry/ritmex-skills, anti-ui-slop | MIT | Rule ids with thresholds, fix in leverage order, structure before vocabulary (id scheme, REVIEW protocol steps 3 and 5) |
| github.com/pbakaus/impeccable | Apache-2.0 | Stable antipattern ids; the build vs audit mode split (the two modes) |
| github.com/JCarterJohnson/vibecoded-design-tells | MIT | The "cleared by the data" list (Severity and strength) |
| github.com/MengTo/Skills, web-design | MIT | Logo-row rules (SC-LAYOUT-14) |
| FTC rule on fake reviews and testimonials (2024), ftc.gov | public domain | Why an invented testimonial is a legal problem (SC-HONEST-01) |
| Blogs (scanaislop, Admin Bar, ContentBeta, Superdesign) | unlicensed, paraphrase only | Local-business brochure words, builder provenance (SC-COPY-14, SC-BUILD-12) |

Catalog gaps placed here without a single external source: performative section
labels (P21), three equal icon cards (L02), bento only when items differ (L04),
repeated layout family and zigzag (L06), comparison alignment (L14), voids
(L15), index tags (L16), decorative numbering (T05), headline length (T07),
meta strings (T13), glass and pill nav (V03), offset shadows (V09), generated
image checks (I04), found photos (I05), scrims (I07), hover gating (M07), hero
video (M08), form control heights and error slots (MB06), phone register
(MB02), focus ring timing (A01), carousels (A03), decorative `aria-hidden`
(A04), page basics (S05). The per-rule citations live in the design doc's
catalog.

## What was dropped as duplicate

- interface-review Domains 1 to 4 (typography, colour, layout, UI polish):
  near-verbatim copies of the same jakubkrehel rules already in `sites-craft`
  sections 1 to 4. Replaced by a pointer; REVIEW mode checks against craft.
- ship-fixes 8B "editorial test" wording that pointed at `sites-theme-system`:
  kept as the logo-swap test in SC-SCORE-02, without the pointer.
- theme-system's variety axis "against the last site": the agent cannot read
  prior sites, so the Variety axis asks only whether neighbouring sections
  differ.
- The full accessibility rule text: owned by `sites-craft` section 5; the A11Y
  ids are the checks against it.

## Not ported

- Every library-specific pattern from designs-by-kore (GSAP, Lenis, Three.js):
  a defect fix that needs JavaScript fails with JavaScript off.
- anti-slop's 38-rule core, Delivery Gate and contrast script: mostly duplicates
  of design-taste; /sites denies `Bash`.
- hallmark's archetype catalogs and project-memory stamp: no filesystem on
  /sites, and shipped stamps are agent notes in public source (SC-COPY-03).
- The catalog's unsourced percentages and the "everyday words" ban.

## Not verified

The upstream test viewports (1920x860, 1440x820, 1366x768; the skill now names the 1280x800 and 390x844 frames `preview_site` renders), the ~40vh spacer, the ~60px
nav threshold and the three-weak-tells threshold are carried from upstream and
have not been re-derived against a live Paw Site build.

## To refresh

Re-fetch the sources above at a newer commit and diff only the rules cited by
id. Keep ids stable: a rule that is retired keeps its number and is not reused.
Keep the file single and self-contained, because /sites denies `Read`, so
nothing outside `SKILL.md` reaches the agent. Budget: 26,000 bytes
(`tests/test_sites_check_skill_budget.py`).
