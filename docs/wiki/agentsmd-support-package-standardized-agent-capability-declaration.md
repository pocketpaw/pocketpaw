---
{
  "title": "AGENTS.md Support Package: Standardized Agent Capability Declaration",
  "summary": "The `agents_md` package implements support for the Anthropic AGENTS.md specification — a convention where repositories publish a markdown file describing constraints, capabilities, and instructions for AI agents operating on that codebase. PocketPaw both reads AGENTS.md from target repositories (to inject constraints into the agent system prompt) and publishes its own AGENTS.md for other agents to consume.",
  "concepts": [
    "AGENTS.md",
    "system prompt injection",
    "repository constraints",
    "AgentsMd",
    "AgentsMdLoader",
    "Anthropic agents-md spec",
    "project conventions",
    "agent capability declaration",
    "robots.txt analogy",
    "constraint injection"
  ],
  "categories": [
    "agents",
    "configuration",
    "standards",
    "system prompt"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 338,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

AGENTS.md is an emerging standard (defined at github.com/anthropics/agents-md) that allows repository maintainers to communicate directly with AI coding agents. Think of it as a `robots.txt` for AI agents: it can specify which directories agents should avoid, which test commands to run before committing, which coding conventions to follow, and any other project-specific constraints.

## Dual Role

PocketPaw's `agents_md` package serves two purposes:

**Consumer role**: When the agent backend is given a working directory (e.g., the user's project root), PocketPaw searches for an AGENTS.md file and injects its contents into the system prompt. This ensures that when a user's project says "always run `make lint` before committing," the agent knows to do so without the user having to repeat it every session.

**Publisher role**: PocketPaw maintains its own AGENTS.md at the repository root. External agents (other Claude Code sessions, third-party AI tools) that operate on the PocketPaw codebase will discover this file and learn PocketPaw's conventions — its test commands, directory structure rules, and any areas that require special care.

## Package Structure

The `__init__.py` is intentionally thin, re-exporting only `AgentsMd` and `AgentsMdLoader` from `loader.py`. This clean public surface means consumers only need:

```python
from pocketpaw.agents_md import AgentsMd, AgentsMdLoader
```

All implementation details (file search algorithm, caching, parsing) stay private inside `loader.py`.

## Why This Matters

Without AGENTS.md support, every project-specific constraint must be repeated in every conversation, or encoded in a general system prompt that grows unwieldy as it tries to cover all possible project types. AGENTS.md decentralizes this knowledge: each project maintains its own constraints, and any compliant agent runner picks them up automatically.

The design mirrors how `.editorconfig` works for code style — editors read it automatically, and developers only write it once per project.

## Known Gaps

- The package does not yet support AGENTS.md inheritance (a subdirectory AGENTS.md extending or overriding a root one), which some project structures would benefit from.
- There is no validation that PocketPaw's own published AGENTS.md stays in sync with its actual project conventions (no CI check).
