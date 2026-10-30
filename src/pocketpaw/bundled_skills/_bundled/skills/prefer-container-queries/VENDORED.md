<!-- New file 2026-08-21: provenance + refresh recipe for the vendored prefer-container-queries skill. -->
# Vendored: prefer-container-queries

- **Source:** https://github.com/flornkm/skills (MIT, Florian Kiem)
- **Vendored:** 2026-08-21 from commit `cf1c0094e182`
- **Local edits:** one "PocketPaw note" blockquote added under the H1, naming
  where Tailwind is actually in the pipeline (generated Paw Sites, code-next,
  app UI) and cautioning that the site tracks otherwise author scoped `<style>`
  blocks, so this is for the reusable pieces rather than a utilities rewrite.
  Everything below that note is unmodified upstream text.
- **What it does:** makes container queries the default for responsive Tailwind
  work, with the variant table, the cases where viewport breakpoints are still
  right, a migration recipe, and the review catches (`@md:` with no `@container`
  ancestor silently never applies; an element cannot query itself).
- **Why it ships bundled:** the generated site `package.json` carries
  `tailwindcss` + `@tailwindcss/vite` ^4.2.2 and `app.css` opens with
  `@import 'tailwindcss'`, so end-user sites have container queries in core.
- **Not verified:** no site or app migration has been run under it yet.
- **To refresh:** re-fetch upstream `skills/prefer-container-queries/SKILL.md`,
  re-apply the PocketPaw note, keep this file, note the new commit here.
