<!--
src/pocketpaw/bundled_templates/_bundled/README.md
Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — explains the
two-file sibling convention each template directory follows.
Modified: 2026-06-11 (feat/triage-member-templates) — documented the two
generic vertical templates (applications-triage, member-360) that carry
the richer v2 surface (needs / actions / outcomes / data_sources) the
seed templates omit, and the gated-action binding point.
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

## Vertical templates (applications-triage, member-360)

Two generic, install-ready templates carry the full v2 surface the seed
templates leave empty:

| Template | What it is | v2 surface it uses |
|----------|-----------|--------------------|
| `applications-triage` | A review queue: status/summary/score/age list, a Q&A detail panel for the selected item, a backlog burndown stat row, and an approve / reject / needs-review action row. | `needs: [paw.db.v1]`, `data_sources`, three gated `actions` (`instinct_policy: require_approval`), `outcomes`. |
| `member-360` | A single-pane, read-only view of one member: header, key-value profile, membership panel, ticket/order/note lists, attendance/spend stats. | `needs: [paw.db.v1]`, `data_sources`. No actions. |

Both are fully generic — no client names, no domain-specific fields
beyond what any application-triage or member-view needs. They declare
`needs: [paw.db.v1]` (the generic database Sense) so a deployment is
prompted to connect a data source; with none, the seeded sample rows
render. Their seed-only field-set assertions are intentionally excluded
in `tests/unit/test_bundled_templates.py`; their richer shape, compile,
and service-create round-trip are covered by
`tests/unit/test_vertical_templates.py` and
`tests/cloud/pockets/test_vertical_template_create.py`.

### Action-row binding point (applications-triage)

The approve / reject / needs-review buttons do **not** execute. Each
button sets `state.pending_proposal` to a generic proposal shape
`{action, application_id, summary}`. That is the seam a deployment wires
to the Instinct gate so a human confirms in The Tray: read
`state.pending_proposal` and call
`pocketpaw_ee.cloud.external_actions.propose.propose_external_action(...)`
with the bound connector name + action + params, which files an Instinct
`Action` (`kind="external_action"`) and opens the `agent.proposed`
decision chain. The executor performs the connector call only on human
approval. Nothing approves inline — the button only proposes. For a
Fabric write instead of a connector call, the pocket-write proposal
bridge is the parallel seam.

## Adding a template

1. Create `_bundled/<slug>/template.pocket.yaml` + `_bundled/<slug>/ripple_spec.json`.
2. Add a matching row to `_bundled/index.json`.
3. The installer discovers the directory by iteration — no installer code change.
