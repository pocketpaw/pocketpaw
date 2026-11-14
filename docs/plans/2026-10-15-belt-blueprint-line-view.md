# Belt: one blueprint, one line, two views

Date: 2026-10-15. Status: draft for the captain. Size: L (two repos, 9 PRs).
Repos: pocketpaw (`cloud/belt/`), paw-enterprise (`/belt-next`). Data: creative-apps and -core on `dev`.

## The ask

The captain wants a blueprint view (a feature tree plus pseudo C4, built versus in the works), a
line view that stacks everything being built by layer, and the two tightly integrated. Here the
plan and the live work are two views of the same objects, linked both ways, with every status
derived. Nobody types a status by hand.

## What exists today

- One `BeltLine` per repo (`groupLines`), so creative-apps is one Line with about ten mandates.
  Its stage shows one run in Atlas, City, Strata or Floor. Strata stacks C4 levels for that run.
- `GET /belt/runs/{id}/blueprint` reads `docs/c4/model.json` (`_base_model`) and joins files
  with `orient.component_for`. The run blob holds the lead (`feature`, `issue`, `branch`,
  `packet`, `prs`, `stalls`, `contact_sheet`), but `_run_summary` drops it. Belt never reads
  `RECORD.md`.
- creative-apps plans as data: a `features/<f>.wave.json` per feature (wave, `feature_issue`,
  `waits_on`, `depends`; `owned` and `journeys` on Halo and Library only), a `c4-planned.json`
  per app (fine components with `paths`), a coarse assembled `docs/c4/model.json`, and each
  lead's `docs/leads/<f>/RECORD.md` (`Stage:`, `Next:`, Tasks) on `lead/<f>`.
- Ripple has the widgets: `C4Diagram` (draws `planned` dashed), `Tree`, `TreeTable`, `Table`,
  `TaskRows`, `ListRow`, `SectionHeader`, `StatusDot`, `Segmented`.

## 1. Object model

```
Blueprint      repo slug @ dev                         truth: repo files
 └ App         docs/blueprints/<app>.md + folder        truth: repo files
    └ Wave     "wave" in each wave.json                 truth: repo files
       └ Feature  features/<feature>.wave.json          truth: repo files (the plan)
          ├ Issue     feature_issue #N, its labels      truth: GitHub
          ├ Lead run  Belt run, recipe feature-lead     truth: Belt store (run blob)
          │  └ Record lead/<f>: RECORD.md, Tasks rows   truth: lead branch
          ├ PRs       Belt-Lead PRs, "Feature: <f>"     truth: GitHub (mirrored on the blob)
          ├ CI        dev CI on the feature-done merge  truth: GitHub Actions
          └ Contact sheet  Belt-Contact-Sheet: comment  truth: GitHub (mirrored on the blob)
C4 component   c4-planned.json id + paths              truth: repo files + dev tree
Journey        H-J5 id in <app>.md or wave.json         truth: repo files + UITests on dev
```

Identities: app `halo`; feature `halo/layer-keys`; lead run `act-1a13eb691a3-wg4n`; task
`halo/layer-keys#LK-12`; issue or PR `qbtrix/creative-apps#1122`; component `halo/halo-window`;
journey `halo/H-J5`.

The joins live in one server module:
- **Run to feature.** `lead.packet` names `docs/blueprints/<app>/features/<f>.md`
  (`_lead_feature` resolves it at launch). The newest run per feature wins.
- **Mandate to app.** Charter `issue_labels` hold `line:<app>`. This links the Blueprint view
  (keyed by app) to the Line view (keyed by repo and its mandates).
- **Feature to component.** An `owned` path that `component_for` maps into `c4-planned.json`,
  else the lead's RECORD task files. **Task to layer.** Its files, resolved against the dev tree.

**Feature status.** First rule that holds wins. The wave.json `status` is only the lead's claim.

| status | rule |
|---|---|
| done | the lead run landed (feature-done PR merged, dev CI green on it, contact sheet posted), or the issue closed and a merged `feature-done` PR names the feature |
| done | the issue's newest `Belt-Contact-Sheet:` / `Mac-Finish-Wanted:` line is the sheet, even when the run was released (supervisor, 8 h cap) |
| built | a `feature-done` PR merged; dev CI or the contact sheet is still pending |
| gap | the claim is ahead of the evidence, or the issue closed without done |
| waiting | issue labelled `needs-human` and its newest of those two lines is `Mac-Finish-Wanted:`: only the Mac finisher's work is left |
| stuck | issue labelled `needs-human`, 2+ stall rungs, or the run released without done |
| building | a pending lead run on the issue |
| filed | `feature_issue` is an open `#N` with no pending lead |
| planned | `feature_issue` is not a `#N` yet ("file when ready") |

The rules read GitHub first. Flare's features landed before Belt tracked leads, and they still
derive from issue and PR state. Comments are read only for an open issue that is `needs-human`
or whose newest run was rejected or failed, and again only when its `updatedAt` moves.

**Component status.** A component is built when each of its `paths` exists in
`git ls-tree -r origin/dev` (a path ending in `/` counts once any file sits under it). Some
paths present makes it partial, and none makes it planned. Matching by id cannot work, because
dev's model has only coarse `*-sources` components. Belt paints `changing` when a building
feature owns the component, amber `drift` when a waiting one does, and `failed` when a stuck
one does.

**Journey status.** Named: the id is in the blueprint's Journeys section or wave.json. Existing:
the id is in a `///` comment in a `UITests/*JourneyTests*.swift` on dev. Passing, for now: it
exists, its feature is built or done, and dev CI is green (question 3). **Gap kinds:**
`claimed_ahead`, `issue_closed_not_done`, `component_missing` (typed existing, paths absent),
`component_unowned`, `journey_no_test` (owner done) and `feature_no_owned`.

## 2. Blueprint view

The route is `/belt-next/lines/[line]/blueprint?app=<app>&tab=<tab>&sel=<kind>:<id>`. It mounts
the same LineWorkspace frame (top bar, works list, chat), and only the stage swaps. The top bar
gains a `Line | Blueprint` toggle. The lifecycle strip's "Blueprint" mark links here too.

```
┌ creative-apps │ [Line│Blueprint]  dev 62a8577 · read 40 s ago                       ▤ ▥ ┐
├ apps ─────────────┬ Halo · 5 of 9 built  [Features] [C4] [Journeys 18/24] [Gaps 3] ─┬ chat ┤
│ ▾ creative-apps   │ WAVE 1                                                         │      │
│   ● Flare    4/4  │ ● done     Layers and files     #777   lead ✓    PRs 12 merged │      │
│   ◉ Halo     5/9  │ WAVE 3                                                         │      │
│   ◉ Photo    3/7  │ ◉ building Key layers by drag   #1036  4 round 1  PRs 15       │      │
│   ○ Design   0/9  │            waits on #1125 (open)                               │      │
│   ◉ Library  6/13 │ ✕ stuck    History files        #1012  needs-human  PRs 11     │      │
│   ◉ Suite    1/5  │ ○ filed    Text layers          #1140  waits on layer-keys     │      │
│   ● SuiteUI  7/7  │ ○ planned  Precomps             file when ready                │      │
│ ▾ creative-core   │ ▲ gap      Easing panel  wave.json says built, no done PR      │      │
│   ◉ core     2/6  │                                                                │      │
└───────────────────┴────────────────────────────────────────────────────────────────┴──────┘
```

- **App tree.** Ripple `Tree`, one node per app with "built of total"; creative-core is a root.
- **Features.** Ripple `TreeTable`, waves over features: status, issue, lead stage (RECORD),
  PRs, and waits on (each ref resolved to a status).
- **C4.** Ripple `C4Diagram` on `c4-planned.json`: built solid, planned dashed, `changing`
  rings live. A click lists the component's files (✓ on dev, ○ missing) and its owners:

```
 ┏━━━━━━━━━━━━━┓  ┌┄┄┄┄┄┄┄┄┄┄┄┐    halo-window · partial, 9 of 11 files
 ┃ halo-window ┃  ┆ halo-text ┆    ✓ Sources/HaloApp/HaloWindow.swift       halo/spine (done)
 ┃ ◉ changing  ┃  ┆ planned   ┆    ○ Sources/HaloApp/HaloWindow+Text.swift  halo/text-layers
 ┗━━━━━━━━━━━━━┛  └┄┄┄┄┄┄┄┄┄┄┄┘
```

- **Journeys.** Ripple `Table`: id, name, feature, test (or "no test yet"), passing. Header:
  "named 24 · existing 18 · passing 15". **Gaps.** One `ListRow` per gap; a click selects it.

## 3. Line view: a stack by layer

**The reading chosen.** The layers are architecture layers from the C4 containers. They are
not C4 levels and not pipeline stages. The captain said "pseudo c4" in the same breath as
"stack". C4 levels (Strata's planes) can't separate parallel work, since every feature crosses
all four. Lead stages and waves are a pipeline over time, and each row already shows them.
Containers answer the question the captain asked: what is being built at each layer now?

Container to layer, first match wins: (1) Engine: an external system's containers
(`creative-core`), and all of creative-core's own line. (2) Journeys and tests: ids ending
`-tests`, `-journeys`, `-snapshots`, plus `app-audit`; any file under `Tests/` or `UITests/`
goes here first. (3) Shell: `suiteui*`, `suite-shell`, `afterglow-*`. (4) Model: `*-model`,
`suite-logic`. (5) App UI: `*-app`, `apps`. (6) Other: always shown, so a misfit is visible.

A task sits on the layer most of its non-test files map to, or on tests if it has only test
files. A feature appears once on each layer it touches, and each row counts only that layer's
tasks. The bands read top to bottom as a dependency stack: each builds on the ones below, so a
waits-on arrow points down.

```
┌ creative-apps │ [Line│Blueprint]  ✓─✓─◉─○ Building  ● 7 running · 2 need you  [Review 2] ▤ ▥ ┐
├ works ──────┬ In flight by layer   [All] Flare Halo Photo Library Design SuiteUI ───┬ chat ┤
│ Needs you 2 │ JOURNEYS AND TESTS                                    2 in flight     │      │
│ Now running │  ◉ halo/layer-keys  LK-12 ✓ LK-13 ✓ LK-14 ✓     3/3  4 round 1    │      │
│ Mandates    │       waits on #1125 (viewer-playback keyedFrame6)                   │      │
│ Landed      │ APP UI                                                3 in flight     │      │
│             │  ◉ halo/layer-keys  LK-9 ✓ LK-10 ✓ LK-11 ✓      3/3  4 round 1    │      │
│             │  ◉ photo/photo-crop CP-3 ◉ CP-4 ○               1/4  3 wave 2     │      │
│             │  ◉ task #1146 SuiteUI slider ticks              develop · Belt-Run │      │
│             │ SHELL (SuiteUI)                                       1 in flight     │      │
│             │  ◉ suiteui/inspector-rows  SR-2 ◉ check         2/5  3 wave 3     │      │
│             │ MODEL                                                 2 in flight     │      │
│             │  ◉ halo/layer-keys  LK-1 to LK-8 ✓              9/9               │      │
│             │  ✕ halo/history-files  stalled twice            8/9  needs-human  │      │
│             │ ENGINE (creative-core line)                           1 in flight     │      │
│             │  ◉ core #216 SetShapeStroke  review   waited on by flare/live-keys    │      │
│             │ OTHER                                                 0               │      │
└─────────────┴─────────────────────────────────────────────────────────────────────┴──────┘
```

- Each band is a `SectionHeader` (layer, in flight, stuck) over Ripple `TaskRows`; done tasks
  fold into a count. A row shows the lead's RECORD stage, that layer's tasks done, and waits-on.
- Engine shows runs on the dependency repo's line (model system id = repo folder name) and the
  engine issues this line waits on. `Segmented` chips filter by app.
- **Default changes.** With no run in the URL, the Line page shows the stack, not the newest
  run. A row opens `/belt-next/runs/[id]`, which keeps today's RunFrame.

## 4. Integration

- **One selection.** One URL param, kind first, like `?review=`: `?sel=feature:halo/layer-keys`,
  `run:<id>`, `component:halo/halo-window`, `journey:halo/H-J5` or `pr:1153`. `selectItem` sits
  beside `openSheet` in `sheet-url.ts` and reuses `withParam`, so Back and deep links just work.
- **Resolution.** `resolveSel(sel, graph)` turns any selection into its feature, run,
  components, PRs and journeys. Each view lights its own form: rows on every band, the Features
  row, C4 nodes (`selectedId`, markers). The chat banner's context card links the blueprint,
  the line, the lead run, its PRs and its contact sheet.
- **Toggle and names.** `Line | Blueprint` and every cross link keep `?sel`. WorksList and Today
  rows read the feature from `run.lead`.

## 5. API contract

Two new reads, both RBAC `belt.read`, and one new field on the run rows.

**`lead` on every run row** (`GET /belt/runs`, `GET /belt/runs/{id}`). No new I/O; `null` on a
task run:
```json
"lead": {"feature": "layer-keys", "app": "halo", "issue": 1036, "branch": "lead/layer-keys",
  "prs": [{"number": 1122, "state": "MERGED", "done": false}], "sessions": 1, "stalls": 0,
  "contact_sheet": null}
```

**`GET /belt/blueprints?repo=<path>`**, the whole graph for one repo:
```json
{"repo": "/…/creative-apps", "slug": "qbtrix/creative-apps", "sha": "62a8577", "stale": false,
 "read_at": "2026-10-15T12:00:00Z", "dev_ci": {"sha": "62a8577", "state": "green", "url": "…"},
 "layers": ["tests", "app", "shell", "model", "engine", "other"],
 "apps": [{"id": "halo", "title": "Halo", "counts": {"total": 9, "done": 4, "built": 1,
   "building": 1, "waiting": 0, "stuck": 1, "filed": 1, "planned": 1, "gap": 0},
   "journeys": {"named": 24, "existing": 18, "passing": 15}}],
 "features": [{"id": "halo/layer-keys", "app": "halo", "title": "…", "wave": "3", "order": 10,
   "status": "building", "claimed": "planned",
   "issue": {"number": 1036, "state": "open", "labels": ["feature-lead", "line:halo"]},
   "waits_on": [{"ref": "qbtrix/creative-apps#1125", "state": "open"}],
   "run": {"action_id": "act-…", "stage": "4 round 1", "next": "…", "record_at": "…"},
   "tasks": [{"id": "LK-12", "wave": "7", "layer": "tests", "pr": 1122, "state": "merged"}],
   "components": ["halo/halo-window"], "journeys": ["halo/H-J5"]}],
 "components": [{"id": "halo/halo-window", "container": "halo-app", "layer": "app",
   "status": "partial", "present": 9, "total": 11, "features": ["halo/spine"]}],
 "journeys": [{"id": "halo/H-J5", "test": "testDragLayerToKeyPosition",
   "file": "UITests/HaloJourneyTests+Drag.swift", "state": "existing", "features": ["halo/layer-keys"]}],
 "in_flight": [{"action_id": "act-…", "kind": "lead", "feature": "halo/layer-keys",
   "layers": ["tests", "app", "model"]}],
 "gaps": [{"kind": "claimed_ahead", "ref": "feature:halo/easing-panel", "text": "…"}]}
```

**`GET /belt/blueprints/c4?repo=<path>&app=<app>`** returns `{app, model: {scope, model},
status: {<element id>: planned | landed | changing | failed}}`. The model has the same shape as
`BeltRunBlueprint.model`, so the existing adapters read it.

**Reads.** Files committed to dev come from git on the bound clone, which spends no REST quota.
Git runs with `_READ_GIT` flags, and `_re_resolve_repo` keeps it inside the allowlist.
- git: one `git fetch --no-tags origin dev` per TTL, then `ls-tree -r origin/dev` (built
  state), `cat-file blob` per blueprint file (cached by blob sha, which never goes stale) and
  `git grep -E '/// [A-Z]+-[JP][0-9]+' origin/dev -- UITests/` (existing journeys).
- gh, through the patrols runner (`_default_gh`, `_github_slug`, cached like `_gh_rows`):
  `issue list --label feature-lead --state all`, `pr list --label feature-done --state merged`,
  `run list --branch dev --limit 5`, `api git/matching-refs/heads/lead/` (all lead heads in one
  call), `api contents/docs/leads/<f>/RECORD.md?ref=lead/<f>` only for leads whose head moved,
  and one `issue list` on the dependency repo for `waits_on` states.

**Cache and refresh.** The server caches per (workspace, repo) for 60 s behind a single-flight
lock, and reads only on request. One read is about 6 REST calls plus one per moved lead, so the
worst case is about 400 an hour per open repo, out of the token's 5,000 (the trackers spend most
of it). The client re-reads on `belt_run_updated` for this repo (debounced 5 s) and on focus. A
failed read (git ref lock, gh rate limit) serves the last graph with `stale: true`; 503 only
when there is no graph yet.

**Security.** `app` and feature ids must match `^[a-z0-9][a-z0-9-]*$` before any path or ref.
Files are parsed as data, capped at `MAX_MODEL_BYTES`, never run. `in_flight` lists only the
caller's workspace runs.

## 6. Build chunks

Every chunk stays within 400 lines and 15 files, tests included. All PRs target `dev`.

**B1 · `lead` on run rows** (pocketpaw, ~60 lines; no deps; parallel). Owns
`ee/pocketpaw_ee/cloud/belt/service.py` (`_run_summary`), `tests/cloud/test_belt_lead_runs.py`.
Reuses the blob fields the tracker already writes. Accept: a lead run's row carries
`lead.feature`, `lead.prs`, `lead.contact_sheet`; a task run carries `lead: null`.

**B2 · pure derivation** (pocketpaw, ~380 lines; no deps; parallel). Owns
`ee/pocketpaw_ee/cloud/belt/app_blueprint.py` (parsers for wave.json, c4-planned and RECORD by
header name; layer, status and gap rules), `tests/cloud/test_belt_app_blueprint.py`,
`tests/cloud/fixtures/app_blueprint/` (data fixtures, not counted as code). Reuses `orient.load_model`,
`path_index`, `component_for`. Accept: on the fixtures `halo/layer-keys` is `building`, with
LK-12 on tests, LK-9 on app, LK-1 on model; a built claim with no done PR yields
`claimed_ahead`; an unreadable RECORD gives `stage: null`, never a guess.

**B3 · readers and cache** (pocketpaw, ~350 lines; after B2). Owns
`ee/pocketpaw_ee/cloud/belt/app_blueprint_read.py`, `tests/cloud/test_belt_app_blueprint_read.py`.
Reuses executor `_run` and `_READ_GIT`, `_re_resolve_repo`, the patrols gh runner, `_gh_rows`,
`service.list_runs`, `_touched_files`. Accept, with fake git and gh: a second read within 60 s
makes no calls; a moved lead head re-reads only that RECORD; a rate limit serves `stale: true`.

**B4 · routes and docs** (pocketpaw, ~200 lines; after B3). Owns
`ee/pocketpaw_ee/cloud/belt/router.py` (two routes, header list), `tests/cloud/test_belt_console.py`,
pocketpaw `docs/c4/model.json` (C4 touch-time rule). Accept: a MEMBER gets the graph; a repo
outside the allowlist is 404; `app=../x` is 422; the c4 read returns model plus status map.

**U1 · data layer** (paw-enterprise, ~350 lines; builds on fixtures of this contract; parallel).
Owns `src/lib/core/belt/types.ts` (`AppBlueprint*`, `BeltRun.lead`), `api.ts`
(`getAppBlueprint`, `getAppBlueprintC4`; 404 reads as null), `core/belt/app-blueprint.svelte.ts`
(store), `core/belt/app-blueprint.ts` (`stackBands`, `parseSel`, `resolveSel`),
`components/belt/next/sheet-url.ts` (`selectItem`), `__tests__/app-blueprint.test.ts`. Reuses
`resource()`, `bus`, `registerReconciler`, `withParam`, `TONE_COLOR`; the `AppBlueprint` prefix
keeps it apart from the run's `BeltRunBlueprint`. Accept: `stackBands` puts `halo/layer-keys` on
three bands, each counting its own tasks; `resolveSel('run:act-x')` returns feature,
components and PRs.

**U2 · Line stack** (paw-enterprise, ~350 lines; after U1). Owns
`components/belt/next/LineStack.svelte`, `LineWorkspace.svelte` (stack by default, context
card), `WorksList.svelte` (feature in row meta), `__tests__/LineStack.test.ts`. Reuses
`SectionHeader`, `TaskRows`, `StatusDot`, `Chip`, `Segmented`, `EmptyState`, `InlineAlert`,
`runStatus`, `lineWork`. Accept: bands render in order; a building lead shows "Round 1" and
"waits on #1125"; a click sets `?sel=feature:…` and marks that feature on every band.

**U3 · Blueprint frame and Features** (paw-enterprise, ~380 lines; after U2, as both edit
LineWorkspace). Owns `src/routes/belt-next/lines/[line]/blueprint/+page.svelte`,
`core/auth/route-auth.ts`, `core/chat/surfaces.ts`, `LineWorkspace.svelte` (`mode`),
`LineTopBar.svelte` (toggle), `components/belt/next/blueprint/BlueprintView.svelte`,
`BlueprintFeatures.svelte`, stubs for `BlueprintC4`, `BlueprintJourneys`, `BlueprintGaps`.
Reuses `Tree`, `TreeTable`, `Tabs`, `StatusDot`, `Chip`. Accept: `?app=halo` lists Halo's waves
with derived statuses and "5 of 9 built"; Line and back keeps `?sel`.

**U4 · C4 tab** (paw-enterprise, ~250 lines; after U3; parallel with U5). Owns
`BlueprintC4.svelte` and its test. Reuses `C4Diagram` (`status`, `selectedId`, `markers`),
`liveScenario` (run-atlas), `modelOf` (belt-lab timeline), `fullDiagram` (atlas-map), `ListRow`.
Accept: unbuilt components draw `planned` (dashed), one owned by a building feature draws
`changing`, and a click lists its files with their owners.

**U5 · Journeys and Gaps** (paw-enterprise, ~250 lines; after U3; parallel with U4). Owns
`BlueprintJourneys.svelte`, `BlueprintGaps.svelte` and their test. Reuses `Table`, `ListRow`,
`InlineAlert`, `StatusDot`. Accept: counts match the fixture; each gap row selects its object.

**Order.** B1, B2, U1 in parallel; then B3 and U2; then B4 and U3; then U4 and U5 in parallel.
B4 must merge before U2 reaches the Lines page.

## 7. Risks and questions for the captain

1. **Join coverage (top risk).** 32 of 54 features have no `owned` list (Flare, Design, Photo,
   Suite and SuiteUI). For those, C4 links and layer placement come only from agent-written
   RECORD task files, and only once a lead runs. Question: should `scripts/waves check` require
   `owned` and `journeys`? That is a creative-apps change. Recommendation: yes.
2. **Status authority.** Belt derives status. The wave.json `status` stays a claim, and it shows
   as a gap when it runs ahead. Question: should leads stop editing that field?
   Recommendation: keep it one release as a cross-check, then drop it.
3. **Per-journey passing.** Today passing is feature-level. Per-journey results need
   `scripts/ui-test` to write a small `journeys.json` from the xcresult into `ui-screens`.
   Question: add it? Without it, one red journey shows its whole feature as CI red.
