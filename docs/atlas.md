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
`surface` entries and the extracted `connector` / `sense` entries. Each
entry carries a stable `id` (`primitive:pocket`), a one-line `summary`, a
`narrative` (when to reach for it and what it pairs with), `how` (the
tool/verb/API that exercises it), and search `keywords`. The kinds
`capability`, `widget`, and `skill` are reserved for later slices.

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

## Agent tools (`pocketpaw_atlas` MCP server)

The `pocketpaw_atlas` in-process MCP server is registered on the Claude Agent
SDK backend alongside `pocketpaw_widgets` (same policy gate and allowlist
path), so it is ambient on every agent run. Two tools:

### `atlas_search`

- **Args:** `intent` (string, required) — what the agent is trying to do,
  e.g. `"approve agent actions"` or `"publish a website"`.
- **Returns:** ranked capability cards as JSON —
  `{"results": [{id, kind, name, summary, surface?}, ...]}` (top 5). Simple
  lexical scoring over name / keywords / summary / narrative; name and
  keyword hits rank highest.
- **When:** before guessing whether the OS can do something or which
  primitive fits an intent.

### `atlas_describe`

- **Args:** `id` (string, required) — a stable entry id, e.g.
  `"primitive:instinct"`.
- **Returns:** the full entry as JSON (`narrative`, `how`, `requires`,
  `surface`, ...). An unknown id returns an error envelope listing the known
  ids so the agent can self-correct.
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
store.describe("connector:stripe")              # full AtlasEntry or None

from pocketpaw.atlas import check_artifact, compile_atlas, write_artifact
compile_atlas()      # authored + extracted entries, sorted by id
write_artifact()     # what `pocketpaw atlas build` calls
check_artifact()     # (fresh, diff_summary) — what `--check` calls
```

Tests: `tests/atlas/` (`test_compile.py` pins byte-determinism, the
authored/compiled split, connector/sense extraction, `--check`, and the
drift warning).
