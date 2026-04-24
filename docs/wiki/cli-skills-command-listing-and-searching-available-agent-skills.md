---
{
  "title": "CLI Skills Command: Listing and Searching Available Agent Skills",
  "summary": "The `skills` CLI command discovers and displays all skills available to the PocketPaw agent, optionally filtering by search term. It delegates loading and search to the skill loader, rendering results with invocability status and file paths so operators can quickly find and verify which skills are active.",
  "concepts": [
    "skills",
    "skill loader",
    "user-invocable",
    "SKILL.md",
    "agent capabilities",
    "CLI discovery",
    "JSON output",
    "search",
    "skill paths"
  ],
  "categories": [
    "CLI",
    "Skills and Tools"
  ],
  "source_docs": [
    "e910f6133f407edb"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/skills.py` implements the `pocketpaw skills` subcommand. Skills in PocketPaw are discrete capability modules — defined in `SKILL.md` files — that extend what the agent can do. This command provides discovery and inspection of those modules without requiring access to the file system directly.

## Skill Loader Delegation

```python
loader = get_skill_loader()
if search:
    skills = loader.search(search)
else:
    skills_dict = loader.load()
    skills = list(skills_dict.values())
```

The command does not implement skill discovery itself — it delegates entirely to `get_skill_loader()`. This keeps the CLI thin and ensures that any future change to how skills are discovered (new directories, new file formats) is handled in one place. The separation also makes the loader independently testable.

## User-Invocable Flag

Each skill has a `user_invocable` boolean that indicates whether it can be triggered by the user during a conversation (e.g., `/skill-name`) versus being available only to the agent internally. The CLI highlights user-invocable skills with a green `(invocable)` badge:

```python
invocable = f" {GREEN}(invocable){RESET}" if s.user_invocable else ""
```

This distinction matters for operators configuring a deployment: knowing which skills end users can trigger helps reason about the attack surface and the user experience.

## Empty State Guidance

When no skills are found, the command prints the search paths the loader checked rather than a generic "nothing found" message:

```python
for p in loader.paths:
    print(f"    {DIM}{p}{RESET}")
```

This is a defensive UX pattern: the most common reason for no skills is that the user has not created any or has placed them in the wrong directory. Showing the expected paths turns an opaque failure into actionable information.

## JSON Output

The JSON output serializes path as a string (`str(s.path)`) since `pathlib.Path` objects are not directly JSON-serializable. This is a standard conversion pattern but worth noting — the JSON consumer receives an absolute path string, not a relative one.

## Search Semantics

The `loader.search(search)` call is a pass-through to the skill loader's own search implementation. The CLI itself applies no additional filtering or ranking. This means the search behavior (substring match, fuzzy, etc.) is entirely determined by the loader implementation, which may evolve independently.

## Known Gaps

- **No detail view**: The command shows name, description, path, and invocability, but not the full SKILL.md content. An operator who wants to understand exactly what a skill does must open the file manually.
- **No enable/disable toggle**: Skills are discovered passively from the file system. There is no `pocketpaw skills disable <name>` command to temporarily deactivate a skill without deleting its file.
- **Search is loader-defined**: The CLI documentation cannot accurately describe search behavior without knowing the loader implementation. A `--exact` or `--regex` flag at the CLI level would give operators more control.
