<!--
docs/atlas.md — the atlas primitive: the runtime OS self-model and its
two agent tools (atlas_search / atlas_describe).

Created: 2026-07-02 (feat/atlas-core, AT-1) — first end-to-end slice:
hand-authored paw.atlas/v1 seed (10 primitives), AtlasStore
loader/search/describe, and the pocketpaw_atlas in-process MCP server
registered on the Claude Agent SDK backend next to pocketpaw_widgets.
Updated: 2026-07-02 (feat/atlas-surface, AT-3) — surface map + primer:
the seed gains 21 kind="surface" entries (the paw-enterprise client's
user-facing routes), primitives with a natural home route carry it in
their `surface` field, and the context_builder injects an always-on
"Paw OS Primer" block generated from the store.
Updated: 2026-07-02 (feat/atlas-compiler, AT-4) — compiler: the data
file is now a COMPILED artifact built by `pocketpaw atlas build` from
hand-authored sources (atlas/authored/) + extracted connector and sense
entries; CI gates freshness via `atlas build --check`; the store logs a
stale-artifact WARNING on connector drift at first load.
Updated: 2026-07-02 (feat/atlas-overlay, AT-5) — live overlay +
fail-closed entitlement filter: atlas answers now reflect the CALLING
workspace via a per-run EntitlementProvider (atlas/overlay.py) —
connector cards carry `available`, unavailable connectors rank below
available ones at equal relevance and describe points them at the
integrations surface, and non-granted entries are absent everywhere.
Updated: 2026-07-02 (feat/atlas-widgets, AT-6) — widget + skill kinds:
the compiler extracts one `widget` entry per ripple catalog type (from
the bundled design-language module, offline — never the CDN manifest)
and one `skill` entry per BUNDLED skill; the store's lexical search
gains a cheap deterministic suffix normalizer (stemming) so inflected
query words match singular keywords. Installed (non-bundled) skills
stay with the system-prompt skills block.
Updated: 2026-07-02 (feat/atlas-fabric, AT-7) — live Fabric schema
introspection (EE-only, fail-closed): an optional per-run
FabricIntrospector (atlas/fabric.py) lets atlas_search surface
synthetic `fabric:<entity-type>` cards for the CALLING workspace's live
ontology and atlas_describe answer `fabric:<type>` with properties +
links. Never compiled into atlas.json; absent/erroring introspector →
gracefully absent (OSS default).
Updated: 2026-07-02 (feat/atlas-eval, AT-2) — added the "Eval" section:
intent→capability ranking-regression harness (tests/atlas/eval_cases.json
+ tests/atlas/test_eval.py), measured strict-hit baseline 16/18.
Updated: 2026-07-03 (integration/atlas) — re-baselined the eval against
the full compiled model (237 entries) + fixpoint stemmer: baseline now
21/22; fabric xfail promoted; three cases re-pointed at the more
specific compiled entries; four new cases cover widget / skill /
connector / sense intents; authored branch + soul keywords gained
"roll back" and "persist".
-->

# Atlas — the OS self-model

Atlas is the runtime's self-model: a hand-authored capability map the
product's runtime agents (chat agent, pocket specialist) query to learn what
the OS itself is and can do. The paw primitives carry paw-specific meanings
(Pocket = workspace app container, Instinct = human approval gate, Fabric =
typed knowledge graph, Belt = code assembly line, ...) that differ from LLM
default meanings — atlas gives an agent ground truth instead of priors.

The packaged model (`src/pocketpaw/atlas/data/atlas.json`, schema
`paw.atlas/v1`) is a **compiled artifact** (see "Compiler" below). It carries
10 hand-authored `primitive` entries — Pocket, Instinct, Fabric, Connector,
Ripple, Soul, Branch, workspace-jobs, Sites, Belt — plus 21 authored
`surface` entries and the extracted `connector` / `sense` / `widget` /
`skill` entries. Each entry carries a stable `id` (`primitive:pocket`), a
one-line `summary`, a `narrative` (when to reach for it and what it pairs
with), `how` (the tool/verb/API that exercises it), and search `keywords`.
The `capability` kind stays reserved for a later slice.

## Compiler (`pocketpaw atlas build`)

Since AT-4 the data file is built, not hand-edited:

- **Authored sources** live in `src/pocketpaw/atlas/authored/` —
  `primitives.json` (10 entries) and `surfaces.json` (21 entries), same
  paw.atlas/v1 entry schema. Edit THESE, never `data/atlas.json`.
- **Extracted `connector` entries** — the compiler
  (`src/pocketpaw/atlas/compile.py`) parses every YAML in the repo's
  `connectors/` dir and emits one `connector:<name>` entry per connector:
  summary (display name, type, action count), a narrative listing every
  ACTION (name + one-liner + param names) and the declared senses, and
  search keywords from name/actions/senses. This is the slice that lets an
  agent discover e.g. that Stripe supports `list_invoices` via
  `atlas_search` instead of guessing.
- **Extracted `sense` entries** — one `sense:<sense-id>` entry per
  `CORE_SENSES` vocabulary item (`src/pocketpaw/senses/vocabulary.py`),
  cross-linking the connectors that declare the sense (via
  `connectors_for_sense`) in both narrative and `requires`.
- **Extracted `widget` entries (AT-6)** — one `widget:<type>` entry per
  ripple canvas widget. Source is the BUNDLED design-language module
  (`src/pocketpaw/ripple/_design.py`), never the CDN widget manifest —
  `ripple/manifest.py` is network-only and the compiler must stay
  offline-deterministic. Three bundled constants feed the card:
  `WIDGET_CATALOG` (the type list + category; the control-flow grammar
  `if`/`each` is excluded), `USE_THE_WIDGET_RULE` (intent phrases like
  "kanban / board / sprint board" → search keywords), and `WIDGET_SHAPES`
  (key prop NAMES for the high-traffic widgets, mined from the canonical
  examples). The card is a **discovery pointer, not the prop contract**:
  its `how` and narrative route the agent to the existing
  `get_widget_spec` tool for the full, live prop schema.
- **Extracted `skill` entries (AT-6)** — one `skill:<slug>` entry per
  BUNDLED skill (`src/pocketpaw/bundled_skills/_bundled/skills/`),
  frontmatter parsed with the runtime's own `parse_skill_md`: summary =
  first sentence of the description, narrative = the full description
  (that's where the capability vocabulary lives) plus argument hints,
  `how` = slash-command + Skill-tool invocation. **Installed-skills
  caveat:** workspace-installed skills (`~/.agents/skills`,
  `~/.claude/skills`, `~/.pocketpaw/skills`) vary per machine and are
  deliberately NOT baked into the compiled artifact — their discovery
  stays with the system-prompt skills block for now.
- **Deterministic output** — authored + extracted entries are sorted by id
  and serialized with sorted keys, indent 2, and a trailing newline, so the
  same inputs always produce byte-identical output. The compiled artifact
  carries a `"generated": true` provenance header (authored files omit it).

Commands (run from the repo root — the compiler reads `./connectors/`):

```bash
pocketpaw atlas build           # recompile and write data/atlas.json
pocketpaw atlas build --check   # compile to memory; exit 1 + diff summary if stale
```

`data/atlas.json` is checked in like a lockfile. CI runs
`uv run pocketpaw atlas build --check` in the lint job
(`.github/workflows/ci.yml`), so a PR that edits authored files or connector
YAMLs without regenerating the artifact fails with a summary of the
added/removed/changed entry ids.

**Startup drift warning:** the first `get_atlas_store()` call in a process
(MCP server, primer build, CLI) compares the artifact's `connector:*` name
set against the live connector YAML scan (the same home-dir + `connectors/`
dirs the ConnectorRegistry reads). On mismatch it logs a WARNING — "atlas is
stale — run `pocketpaw atlas build`" with the missing/extra names — and
keeps serving. Name-set compare only, no recompile, and it never raises.

## Surface entries (`kind: "surface"`)

The authored set also maps the paw-enterprise client so agents know the frontend
exists and can tell users where to go. Each `surface:*` entry mirrors a REAL
user-facing route in `paw-enterprise/src/routes/` and carries the route path
in its `surface` field:

| id | route | what the user does there |
|----|-------|--------------------------|
| `surface:home` | `/` | OS home: widget grid, activity river, chat pill |
| `surface:chat` | `/chat` | rooms rail (DMs, agents, groups, channels, entity rooms) |
| `surface:pockets` | `/pockets` | pocket list + live canvas detail |
| `surface:sites` | `/sites` | published-sites gallery, create→publish flow |
| `surface:belt` | `/belt` | develop-station console: runs + diff viewer |
| `surface:paw-print` | `/paw-print` | org-wide decision feed (Instinct corrections) |
| `surface:decisions-graph` | `/decisions-graph` | visual query layer over decision history |
| `surface:mission-control` | `/mission-control` | operator work feed (cycles, analytics) |
| `surface:agents` | `/agents` | agent list + per-agent editor |
| `surface:settings` | `/settings` | personal settings (profile, security, API keys, …) |
| `surface:integrations` | `/settings/workspace/integrations` | third-party credentials |
| `surface:workspace-admin` | `/settings/workspace` | workspace admin incl. plan info |
| `surface:knowledge` | `/knowledge` | KB browser |
| `surface:files` | `/files` | file browser |
| `surface:studio` | `/studio` | media generation gallery |
| `surface:code` | `/code` | in-browser IDE |
| `surface:foresight` | `/foresight` | scenario rehearsal |
| `surface:calendar` | `/calendar` | workspace calendar |
| `surface:meetings` | `/meetings` | meetings timeline |
| `surface:activity` | `/activity` | workspace activity feed |
| `surface:audit` | `/audit` | audit log |

Primitives with a natural home route cross-link it in their own `surface`
field so `atlas_describe` answers include where to see the result:
`primitive:pocket` → `/pockets`, `primitive:instinct` → `/paw-print`,
`primitive:connector` → `/settings/workspace/integrations`,
`primitive:sites` → `/sites`, `primitive:belt` → `/belt`. (There is no
dedicated billing route in the client today; plan info lives on
`/settings/workspace`.)

## Search ranking rules (`atlas/store.py`)

Search is deliberately simple lexical scoring — no embeddings, no external
deps, fully deterministic:

- **Field weights** — each query token scores once per entry, at its best
  field: name hit `5.0` > keyword hit `3.0` > summary hit `1.5` > narrative
  hit `1.0`. Zero-overlap entries are dropped; ties keep artifact (id)
  order via a stable sort. These weights are unchanged since AT-1.
- **Suffix normalizer (AT-6)** — both index and query tokens are stem
  normalized so inflected query words match singular keywords
  ("competitors" now hits a `competitor` keyword; "meetings" and
  "meeting" both reach the `meet` stem). Exact rules, repeated to a
  fixpoint on a lowercased token:
  1. `ies` → `y` when the stem keeps ≥ 3 chars (companies → company);
  2. strip a trailing `s` (never `ss`/`us`/`is`) when the stem keeps
     ≥ 3 chars;
  3. strip `ing` when the stem keeps ≥ 4 chars (meeting → meet);
  4. strip `ed` when the stem keeps ≥ 4 chars (connected → connect).
  There is deliberately NO trailing-`e` strip — it collided with real
  catalog vocabulary (state → stat, notes → not, sites → sit).
  It is intentionally not a full stemmer (approve/approved still differ) — cheap
  and deterministic over linguistically complete. Known remaining
  weakness (documented by the atlas eval): generic review/approve
  vocabulary can still pull sibling primitives close together; fixing
  that is a vocabulary/authoring question, not a weight change.

## Live overlay + entitlement filter (`atlas/overlay.py`)

The compiled artifact is global — the same entries in every workspace. Since
AT-5 the MCP tools view it through a per-run **overlay** so answers reflect
the calling workspace's reality:

- **`EntitlementProvider` protocol** — a small structural protocol (duck
  typed, like the repo's other protocols) answering two per-context
  questions: `connected_connector_names() -> set[str]` (which connectors
  are connected in THIS context) and `is_granted(entry) -> bool` (whether
  this context may see the entry at all).
- **Availability annotation** — connector entries gain `available: bool`:
  connected in this context or not. Availability is a live fact, not an
  entitlement — an unavailable connector stays visible, it just tells the
  agent it isn't wired up yet.
- **Fail-closed filtering** — an entry whose grant check does not answer a
  literal `True` (returns `False`/`None`, or the provider raises) is
  REMOVED: absent from search results, not describable (`atlas_describe`
  answers exactly like an unknown id), and absent from the known-ids error
  listing. No "upgrade to see this" leakage in v1. If
  `connected_connector_names` raises, every connector is annotated
  `available: false` (unavailable, not filtered).
- **Re-ranking** — search results get a stable re-sort so an available
  connector ranks above an unavailable one at EQUAL relevance; the store's
  base scoring is untouched (an unavailable-but-more-relevant entry still
  wins). Filtering happens before the result limit, so non-granted entries
  never eat result slots.
- **No mutation** — the overlay wraps store entries in `OverlaidEntry`
  (result layer); the shared `AtlasStore` singleton's entries are never
  modified, and the primer / drift check keep the unfiltered OS-level view.

**`DefaultEntitlementProvider` (OSS default)** gates nothing —
`is_granted` is always `True`, so primitives, surfaces, senses, and
connectors all stay visible in OSS. Availability comes from the real
connector seam: `ConnectorRegistry.status(scope_key)` (the same
durable-state view the `connector_list` builtin reports, on the same shared
registry), resolved per call so mid-session connects are reflected. The
scope key is per run, never a process-global: the Claude Agent SDK backend
builds the provider with `ws:<POCKETPAW_WORKSPACE_ID>` when an isolated
cloud run attached tenancy via `attach_subprocess_env`, else the OSS
single-user `"default"` scope. Tenancy attached with a BLANK workspace id
fails closed to a sentinel scope that matches no rows — never the shared
`"default"` bucket.

**Cloud availability caveat (honest scope of v1):** the `ws:<id>` plumbing
is in place, but the cloud connector state store intentionally does not
enumerate tenant rows, so under a real cloud scope every connector currently
reports `available: false` (conservative — nothing leaks, hints still point
at the integrations surface). Live per-tenant availability lands with the EE
provider slice: implement the same two-method protocol against the EE
workspace-connector view and pass it to
`build_atlas_context_server(provider=...)`. The OSS `"default"` scope reads
live file-store state and is fully functional today.

## Live Fabric schema introspection (`atlas/fabric.py`, EE-only)

Fabric — the typed workspace ontology (typed objects like Customer /
Competitor, typed links like `competes_with`) — is EE cloud and **per
tenant**: its entries are dynamic, so they are NEVER compiled into
`atlas.json` (which stays global and byte-deterministic). Since AT-7 the
atlas tools can still answer "what entity types exist in THIS workspace?"
live, through an optional per-run introspector:

- **`FabricIntrospector` protocol** — structural, like
  `EntitlementProvider`: `list_entity_types() -> list[str]` and
  `describe_entity_type(name) -> dict | None` (properties + link types).
  `build_atlas_context_server(provider=..., introspector=...)` accepts it
  next to the provider; the tool closures capture it per run.
- **Search** — with an introspector, `atlas_search` additionally matches
  the intent against live entity-type names and their property names
  (exact/stemmed token match, camel-case split, the store's own suffix
  normalizer) and APPENDS up to 3 synthetic cards — id
  `fabric:<entity-type>`, tool-layer-only kind `fabric` (not part of
  `AtlasKind`; fabric cards are built at answer time, never `AtlasEntry`
  rows) — **after** the compiled-entry results. Compiled results are never
  displaced or re-ranked by fabric hits.
- **Describe** — `atlas_describe("fabric:<type>")` returns the live schema:
  sorted `properties`, `links` (`{name, from_type, to_type}` dicts),
  `workspace_scoped: true`, and a narrative pointing back at
  `primitive:fabric`.
- **OSS absence** — without an introspector (the OSS default) nothing about
  live Fabric exists: `fabric:*` ids answer as unknown ids (and never
  appear in the known-ids listing), no fabric cards ever surface, and the
  only Fabric knowledge is the compiled `primitive:fabric` narrative.
- **Fail-closed** — an introspector that raises (listing OR describing) is
  treated exactly like an absent one: DEBUG log, no crash, no partial
  leak. Malformed shapes (non-string names, non-list properties) are
  dropped, not propagated.

**EE wiring** (`claude_sdk.py` `_get_mcp_servers`): the introspector is
built only when the run's tenant scope is a real `ws:<id>` (via
`_tenant_scope_key()` — the OSS `"default"` scope and the blank-id
sentinel get none; there is no ambient OSS fabric registry, the
`JSONFileFabricRegistry` mock needs an explicit registry file) AND
`pocketpaw_ee.fabric` imports. `build_workspace_fabric_introspector(ws_id)`
binds a `WorkspaceFabricRegistry` (SQLite-backed, workspace-scoped) to the
run's workspace id — per run, never a process-global — and degrades to
`None` on import or construction failure (DEBUG log). Tests:
`tests/atlas/test_fabric_introspection.py` (fake + raising introspectors;
the real EE adapter test is import-guarded and skips when `pocketpaw_ee`
isn't installed).

## Agent tools (`pocketpaw_atlas` MCP server)

The `pocketpaw_atlas` in-process MCP server is registered on the Claude Agent
SDK backend alongside `pocketpaw_widgets` (same policy gate and allowlist
path), so it is ambient on every agent run. Two tools:

### `atlas_search`

- **Args:** `intent` (string, required) — what the agent is trying to do,
  e.g. `"approve agent actions"` or `"publish a website"`.
- **Returns:** ranked capability cards as JSON —
  `{"results": [{id, kind, name, summary, surface?, available?}, ...]}`
  (top 5). Simple lexical scoring over name / keywords / summary /
  narrative with suffix normalization (see "Search ranking rules"); name
  and keyword hits rank highest. `available` appears on
  connector cards only (overlay, AT-5); available connectors rank above
  unavailable ones at equal relevance, and non-granted entries are absent.
  With a fabric introspector (AT-7, EE), up to 3 synthetic
  `fabric:<entity-type>` cards (kind `fabric`) ride AFTER the compiled
  results when the intent matches live workspace entity types.
- **When:** before guessing whether the OS can do something or which
  primitive fits an intent.

### `atlas_describe`

- **Args:** `id` (string, required) — a stable entry id, e.g.
  `"primitive:instinct"`.
- **Returns:** the full entry as JSON (`narrative`, `how`, `requires`,
  `surface`, ...). An unknown id returns an error envelope listing the known
  ids so the agent can self-correct. With the overlay (AT-5): a connector
  entry also carries `available`, an UNAVAILABLE connector adds a
  `connect_hint` pointing at the integrations surface route (looked up from
  the seed's `surface:integrations` entry, not hard-coded), a filtered
  (non-granted) id answers exactly like an unknown id, and the known-ids
  listing carries only ids the calling context may see. With a fabric
  introspector (AT-7, EE), `fabric:<type>` ids answer with the live
  workspace schema (properties + links); without one they are unknown ids.
- **When:** after `atlas_search` picks a candidate, or before explaining /
  exercising a primitive.

## Always-on primer block (context_builder)

`AgentContextBuilder._build_atlas_primer()`
(`src/pocketpaw/bootstrap/context_builder.py`) injects a compact "Paw OS
Primer" block (block #8b, `atlas_primer`) into every built system prompt:

- one-paragraph OS identity ("you run inside paw-os …"),
- one line per primitive, generated at build time from the atlas store (a
  seed edit can never drift from the prompt),
- the standing instruction to call `atlas_search` before guessing about OS
  capabilities and to include the `surface` route ("see it at /sites") when
  pointing a user somewhere after an action.

It renders at ~1.6K chars (~400 est. tokens) with a hard 2000-char
(`_INJECTION_CAPS["atlas_primer"]`, ~500-token) ceiling, ships at the same
MEDIUM priority as the skills block (#8), and is wrapped in try/except so an
atlas load failure never breaks prompt building. Tests:
`tests/atlas/test_primer_block.py`.

## Python surface

```python
from pocketpaw.atlas import get_atlas_store

store = get_atlas_store()          # lazy singleton over the compiled artifact
store.search("approve agent actions", limit=5)  # ranked AtlasEntry list
store.search_scored("approve agent actions")    # (score, AtlasEntry) pairs (AT-5)
store.describe("connector:stripe")              # full AtlasEntry or None

from pocketpaw.atlas import AtlasOverlay, DefaultEntitlementProvider
provider = DefaultEntitlementProvider(scope_key="default")  # or "ws:<id>"
AtlasOverlay.search(store, "invoices", provider, limit=5)   # OverlaidEntry list
AtlasOverlay.describe(store, "connector:stripe", provider)  # None if filtered

from pocketpaw.atlas import FabricIntrospector, build_workspace_fabric_introspector
introspector = build_workspace_fabric_introspector("ws-1")  # None on OSS installs
# build_atlas_context_server(provider=..., introspector=...) serves fabric:* live

from pocketpaw.atlas import check_artifact, compile_atlas, write_artifact
compile_atlas()      # authored + extracted entries, sorted by id
write_artifact()     # what `pocketpaw atlas build` calls
check_artifact()     # (fresh, diff_summary) — what `--check` calls
```

Tests: `tests/atlas/` (`test_compile.py` pins byte-determinism, the
authored/compiled split, connector/sense extraction, `--check`, and the
drift warning; `test_overlay.py` pins the fail-closed filter, availability
annotation + re-ranking, no-leak describe, and the MCP handlers under
stubbed providers; `test_widgets_skills.py` pins widget/skill extraction,
intent-phrase search without exact names, the `get_widget_spec` routing in
`how`, the bundled-only skill guarantee, and the exact stemming rules;
`test_fabric_introspection.py` pins the AT-7 fabric seam: fake-introspector
search/describe, compiled results never displaced, OSS absence, raising →
absent fail-closed, and the import-guarded real EE adapter).

## Eval — intent→capability ranking regression

The permanent quality gate for atlas ranking (same pattern that proved out
on loom): a fixed set of real user intents, each mapped to the atlas entry
that should win, replayed against `AtlasStore.search` on every test run.

**Run it:**

```bash
uv run pytest tests/atlas/test_eval.py -q
```

**How it works.** `tests/atlas/eval_cases.json` holds the cases; each is
`{"intent", "expected_id", "rank_within"}` — the expected id must appear
within the top `rank_within` (1 = strict hit, 3 = top-3) results.
`tests/atlas/test_eval.py` runs one parameterized test per case, plus a
summary test that computes the strict-hit score (expected id at rank 1
across ALL cases) and pins it to `STRICT_HIT_BASELINE`.

**The baseline** is a measured fact, not a target: as of 2026-07-03,
measured against the full compiled model (237 entries) with the fixpoint
stemmer, the weighted lexical ranking puts the expected id at rank 1 for
**21 of 22** cases, so `STRICT_HIT_BASELINE = 21`. The summary test fails
in both directions — a ranking change that drops below 21 is a regression,
and one that beats 21 must bump the constant in the same PR so the gain is
locked in. The single current miss is documented next to the constant:
branch ranks #3 behind instinct and the edit-pocket skill for "review the
agent's edit before it goes live" (generic review/approve vocabulary
overlaps instinct's core keywords — a vocabulary/authoring question, not a
weight change). The 2026-07-03 re-baseline also promoted the original
fabric xfail ("who are our competitors linked to" — the fixpoint stemmer
now matches "competitors"/"linked" to the "competitor"/"links" keywords
and fabric ranks 1), re-pointed three cases at the genuinely better
specific entries the full model carries (`widget:chart`, `widget:kanban`,
`connector:gcalendar` over the generic primitives), and fixed two authored
vocabulary gaps at the source (`primitive:branch` gained "roll back",
`primitive:soul` gained "persist"); each decision is recorded in the
case's `"note"` field. A case can carry a **strict xfail**: never delete a
case to go green — when a ranking upgrade fixes it, the xfail flips to a
loud failure and the case and baseline get promoted.

**Adding a case:** append to `eval_cases.json` with the intent phrased the
way a real user would say it (not with the entry's own keywords), set
`rank_within: 1`, and run the harness. If the case honestly lands at rank
2–3 today, relax it to `rank_within: 3`; if the ranking misses it
entirely, keep it with an `"xfail": "<reason>"` field. Then re-measure and
update `STRICT_HIT_BASELINE` (and the seed's keywords — in a separate
change — if the miss reveals a vocabulary gap).
