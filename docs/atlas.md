<!--
docs/atlas.md — the atlas primitive: the runtime OS self-model and its
two agent tools (atlas_search / atlas_describe).

Created: 2026-07-02 (feat/atlas-core, AT-1) — first end-to-end slice:
hand-authored paw.atlas/v1 seed (10 primitives), AtlasStore
loader/search/describe, and the pocketpaw_atlas in-process MCP server
registered on the Claude Agent SDK backend next to pocketpaw_widgets.
-->

# Atlas — the OS self-model

Atlas is the runtime's self-model: a hand-authored capability map the
product's runtime agents (chat agent, pocket specialist) query to learn what
the OS itself is and can do. The paw primitives carry paw-specific meanings
(Pocket = workspace app container, Instinct = human approval gate, Fabric =
typed knowledge graph, Belt = code assembly line, ...) that differ from LLM
default meanings — atlas gives an agent ground truth instead of priors.

The v1 seed (`src/pocketpaw/atlas/data/atlas.json`, schema `paw.atlas/v1`)
ships 10 `primitive` entries: Pocket, Instinct, Fabric, Connector, Ripple,
Soul, Branch, workspace-jobs, Sites, Belt. Each entry carries a stable `id`
(`primitive:pocket`), a one-line `summary`, a `narrative` (when to reach for
it and what it pairs with), `how` (the tool/verb/API that exercises it), and
search `keywords`. The kinds `capability`, `surface`, `connector`, `widget`,
and `skill` are reserved for later slices.

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

## Python surface

```python
from pocketpaw.atlas import get_atlas_store

store = get_atlas_store()          # lazy singleton over the packaged seed
store.search("approve agent actions", limit=5)  # ranked AtlasEntry list
store.describe("primitive:instinct")            # full AtlasEntry or None
```

Tests: `tests/atlas/`.
