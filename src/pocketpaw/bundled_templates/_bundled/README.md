<!--
src/pocketpaw/bundled_templates/_bundled/README.md
Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — explains the
two-file sibling convention each template directory follows.
-->
# Bundled pocket templates

Each subdirectory here is one built-in pocket template. The installer
(`pocketpaw.bundled_templates.installer`) mirrors every directory plus
the top-level `index.json` into `~/.pocketpaw/templates/` on dashboard
boot, SHA-256 idempotent.

## The sibling-file convention

A template directory carries **exactly two files**:

| File | Format | Purpose |
|------|--------|---------|
| `template.pocket.yaml` | RFC 03 Pocket Template Schema | The publishable metadata: `name`, `version`, `vertical`, `shape`, `state`, `actions`, `connectors`, `skills`, `description`. This is the registry-facing artifact. |
| `ripple_spec.json` | rippleSpec JSON | A full, hand-authored, production-quality rippleSpec skeleton — the canvas the create specialist instantiates and customizes. **A local runtime artifact, not part of RFC 03.** |

`ripple_spec.json` is a PocketPaw runtime sibling — it is *not* an RFC 03
schema field. **RFC 03's registry linter must ignore `ripple_spec.json`.**
A template is published to the registry on the strength of its
`template.pocket.yaml` alone; `ripple_spec.json` is how *this* runtime
turns the template into a live pocket without a cold LLM generation. A
registry linter that walks a template directory should lint
`template.pocket.yaml` and skip every other file, `ripple_spec.json`
included.

## index.json

`index.json` is the registry: a flat list of `{slug, title, shape,
pattern, keywords, connectors_hint}` rows, one per template. The chat
agent's STEP 0 template-library check reads it, keyword-matches the
brief against `keywords` (case-insensitive substring), and on a match
sets the `template_id` hint so the create specialist instantiates the
matched template.

## Seed-template scope (Increment 2a)

The six seed templates ship `actions: []` — empty. Instinct and
Outcomes are not wired yet, and a dead action declaration is worse than
none. `outcomes`, `instinct_rules`, `triggers`, and `agents` are omitted
entirely for the same reason. Increment 2b adds per-backend API skills;
later increments populate `actions` once Instinct lands.

## Adding a template

1. Create `_bundled/<slug>/template.pocket.yaml` + `_bundled/<slug>/ripple_spec.json`.
2. Add a matching row to `_bundled/index.json`.
3. The installer discovers the directory by iteration — no installer code change.
