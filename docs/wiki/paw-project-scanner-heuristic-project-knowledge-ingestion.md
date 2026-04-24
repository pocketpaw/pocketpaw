---
{
  "title": "Paw Project Scanner: Heuristic Project Knowledge Ingestion",
  "summary": "scan.py bootstraps a soul's project knowledge by reading common configuration files (README, pyproject.toml, package.json, Cargo.toml, go.mod, .env.example) and storing facts via soul.remember(). A full agent-powered scan is defined but currently delegates to the heuristic fallback pending AgentRouter wiring.",
  "concepts": [
    "heuristic_scan",
    "run_agent_scan",
    "SCAN_PROMPT",
    "soul.remember",
    "project ingestion",
    "pyproject.toml",
    "env.example",
    "project bootstrapping",
    "importance"
  ],
  "categories": [
    "paw",
    "project-scanning",
    "soul-protocol"
  ],
  "source_docs": [
    "ac92955603a31319"
  ],
  "backlinks": null,
  "word_count": 456,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When `paw init` runs with `--scan` (the default), it calls `heuristic_scan()` to bootstrap the soul with foundational project knowledge. The soul starts with zero context about the project it lives in; the scan reads key files and stores structured facts so the agent can answer questions like "what language does this project use?" immediately.

## Why Heuristic Scan First?

A full agent-powered scan would be ideal — it could read arbitrary files, follow import chains, and store nuanced architectural facts. But wiring `AgentRouter` into a standalone function requires provider credentials, event loop management, and error handling that would significantly increase the complexity of the `paw init` flow. The heuristic scan trades coverage for reliability: it reads a fixed set of well-known files and always works offline.

```python
async def run_agent_scan(soul, project_path, provider) -> None:
    # For now, delegate to heuristic scan + remember.
    # Full agent-powered scan requires wiring AgentRouter which is a bigger lift.
    logger.info("Running heuristic project scan (agent scan coming in next version)")
    await heuristic_scan(project_path, soul)
```

## Files Examined

| File | Importance | Why |
|------|-----------|-----|
| README.md/rst/txt | 8 | Primary project documentation |
| pyproject.toml | 8 | Python project metadata, dependencies |
| package.json | 8 | Node.js project metadata |
| Cargo.toml | 8 | Rust project metadata |
| go.mod | 6 | Go module info |
| .env.example | 6 | Required environment variables (keys only, not values) |
| Top-level directories | 6 | Project structure overview |

The `.env.example` handling is security-conscious: it reads only the key names from `KEY=value` lines, not the values, and explicitly skips `.env` itself to prevent secrets from being stored in the soul.

## Soul Storage

Each fact is stored via `soul.remember(fact, importance=importance)`. Failed stores are caught and logged as warnings rather than exceptions — a partial scan is better than no scan.

```python
for fact in facts:
    importance = 8 if "README" in fact or "project config" in fact else 6
    try:
        await soul.remember(fact, importance=importance)
    except Exception as e:
        logger.warning("Failed to store scan fact: %s", e)
```

## SCAN_PROMPT Template

`SCAN_PROMPT` is a formatted string template intended for the future agent-powered scan. It is defined but not yet passed to a live agent.

## Known Gaps

- **`run_agent_scan` is not yet implemented**: The function body immediately calls `heuristic_scan()`. The `SCAN_PROMPT.format(project_path=project_path)` call formats the prompt but discards the result — a clear indicator the implementation is incomplete.
- **No re-scan detection**: Running `paw init --scan` twice stores duplicate facts. There is no deduplication or "scan was already run" check.
- **File size cap is per-file**: Each file is capped at 2000 characters (README) or 1500 (pyproject.toml, package.json). Large files will be truncated mid-sentence, potentially storing incomplete facts.