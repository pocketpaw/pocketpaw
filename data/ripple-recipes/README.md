# ripple-recipes — pattern recipe library for pocket creation

Source recipes for the `ripple-recipes` kb-go scope. Each markdown
file is one hand-authored recipe describing a polished rippleSpec
shape (sales-pipeline dashboard, customer-support app, recipe/how-to
viewer, etc.) with: when to use it, the full composition, anti-
patterns to avoid, and adjacent-domain variations.

The compiled scope lives at
`src/pocketpaw/bundled_kb/_bundled/ripple-recipes/` and auto-installs
to the user's `~/.knowledge-base/ripple-recipes/` on dashboard boot
via `pocketpaw.bundled_kb.install_bundled_kb_scopes()`. From there,
PocketPaw's existing `_get_kb_context` injection in
`bootstrap/context_builder.py` retrieves matching recipes at pocket-
creation time by intent.

## How retrieval works end-to-end

```
User: "Build me a sales pipeline dashboard for Q2"
  → bootstrap.context_builder._get_kb_context queries kb-go:
      kb search "sales pipeline dashboard Q2" --scope ripple-recipes
  → BM25 retrieval: 95% R@5 on intent matching
  → top-1 result: Sales Pipeline Dashboard recipe (~3KB of content)
  → spliced into agent system prompt under kb_context budget
  → agent drafts rippleSpec following the recipe's polished shape
  → result: pipeline-dashboard widget instead of hand-rolled grid
```

## Rebuilding the compiled scope

When recipes change, the compiled artifact at
`src/pocketpaw/bundled_kb/_bundled/ripple-recipes/` must be
regenerated. Two paths:

### Path A — direct LLM compile (needs ANTHROPIC_API_KEY)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
kb clear --scope ripple-recipes
kb build ./data/ripple-recipes --scope ripple-recipes --pattern "*.md"
cp -r ~/.knowledge-base/ripple-recipes \
      src/pocketpaw/bundled_kb/_bundled/
```

### Path B — agent mode (no API key, uses your local Claude Code)

```bash
kb clear --scope ripple-recipes
kb prepare ./data/ripple-recipes --scope ripple-recipes \
  --pattern "*.md" > /tmp/prepare.json
# Pipe /tmp/prepare.json prompts to Claude Code, capture compiled JSON
# (one article per prompt, format: {source, hash, raw_id, title,
#  summary, content, concepts, categories})
cat /tmp/compiled.json | kb accept --scope ripple-recipes
cp -r ~/.knowledge-base/ripple-recipes \
      src/pocketpaw/bundled_kb/_bundled/
```

The compiled artifact shipping with this PR was generated via
Claude Code as the compiling agent (Path B) — same loop the
agent-mode flow uses for any kb-go user without an API key.

## Adding a new recipe

1. Drop a new `<recipe-name>.md` in this directory
2. Rebuild the compiled scope (Path A or B)
3. Commit both the source `.md` and the regenerated
   `src/pocketpaw/bundled_kb/_bundled/ripple-recipes/`

Aim for ≤3KB per recipe body so it fits the kb_context budget
(~3000 chars per scope per turn).

## Currently shipping

| File | Pattern | Focal widgets |
| --- | --- | --- |
| `sales-pipeline-dashboard.md` | dashboard | pipeline-dashboard |
| `customer-support-app.md` | app | app-shell + sidebar + master-detail + comment-thread + notification-center |
| `espresso-recipe-viewer.md` | viewer | page-header + text + kv-table |

This POC ships 3 cornerstone recipes. A follow-up pass will add ~12
more covering the remaining pattern × domain combinations.
