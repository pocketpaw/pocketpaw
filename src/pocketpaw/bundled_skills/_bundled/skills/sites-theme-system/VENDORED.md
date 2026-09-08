<!-- New file 2026-09-08: provenance + refresh recipe for the adapted sites-theme-system skill. -->
# Vendored: sites-theme-system

- **Source:** https://github.com/Nutlope/hallmark (MIT, Hallmark contributors) —
  `skills/hallmark/SKILL.md` plus `references/anti-patterns.md`
- **Vendored:** 2026-09-08 from commit `13ac0ec7e148`
- **License:** MIT. Attribution retained here; the skill body is a rewrite rather
  than a copy.

## What was ported

The machinery `pocketpaw-design-taste` asserts but cannot check:

- the **theme-difference test** on three measurable axes (paper band / display
  style / accent hue), tightened from "differ on at least one" to **two of three**;
- the **project-memory stamp** and the rule to read it before picking;
- **nav and footer archetype rotation**, with the "state the previous pick out loud"
  line that upstream identifies as its single most-violated rule;
- the **locked-token** rule (no inline hex/oklch/font-family mid-render);
- **honest copy** — the no-fabricated-metrics rule;
- the **re-drawn chrome** ban and the **no-italic-headings** rule;
- the **pre-emit self-critique** (six axes, below 3 triggers a revision pass).

## What was deliberately NOT ported, and why

- **The 21-theme catalog** (Specimen, Atelier, Brutal, Newsprint…). design-taste
  MODULE 2.E already ships six aesthetic-direction families with full token systems.
  A second, larger, differently-named catalog in the same turn would force the agent
  to reconcile two taxonomies. The three-axis *test* is the portable part; the theme
  names are not.
- **The 21 macrostructures and the nav/footer archetype files.** Upstream loads
  these from `references/macrostructures/*.md` and `references/components/*.md` on
  demand. **That mechanism cannot work on /sites**: `_SITES_BUILTIN_DENY` in
  `ee/pocketpaw_ee/cloud/surface/surface_registry.py` strips `Read`, `Glob` and
  `Bash` from every /sites mode, so a skill that says "load only the picked
  archetype file" would name files the agent physically cannot open. The nav and
  footer catalogues were therefore compressed to one-line-per-archetype tables that
  live inline.
- **The 53-gate slop test as a numbered list.** design-taste MODULE 6 already runs a
  pre-flight checklist. Only the gates with no design-taste equivalent were kept.
- **`hallmark audit` / `redesign` / `study` verbs.** `study` reads a URL's HTML and
  CSS via WebFetch to extract design DNA — genuinely useful, but it is a separate
  capability from diversification and it overlaps the sites import/regenerate work
  already in flight on `docs/sites-import-regenerate`. Left out on purpose; worth
  its own skill later.
- **The `.hallmark/log.json` project-memory file.** No filesystem on /sites. Replaced
  by the in-artifact CSS stamp, which the agent can read back from the pocket it is
  refining.
- **The mandatory three-question design-context gate.** design-taste MODULE 1.E is
  explicit that the agent must *infer* the look and not ask the user; upstream is
  equally explicit that it must always ask. design-taste wins on its own surface.

## Local edits

- Tightened the difference test from one axis to two.
- Rewrote the honest-copy section to say what to do when there is no proof (change
  the macrostructure, do not fill the section with invention).
- Added the "no previous stamp, say so" rule — an unverifiable variety claim is
  worse than an honest first build.

## Why it ships bundled

design-taste bans repetition but gives the agent no way to check whether it
repeated. This is the check.

## Not verified

The stamp round-trip is untested: whether an agent refining an existing pocket can
actually read back a stamp written by an earlier build has not been confirmed on a
live site. If it cannot, the memory rule degrades to session-scope only, and the
"first build, no prior stamp" path becomes the common one.

## To refresh

Re-fetch upstream `skills/hallmark/SKILL.md`. Re-check the axis definitions against
the theme comments in upstream `site/css/tokens.css`, and re-check whether /sites
still denies the file built-ins before considering porting any reference-file
mechanism.
