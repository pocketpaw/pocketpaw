---
{
  "title": "CreateSkillTool: Runtime Skill Generation with SKILL.md and Memory Persistence",
  "summary": "`CreateSkillTool` lets agents create new reusable skills at runtime by writing SKILL.md files with YAML frontmatter to `~/.claude/skills/`. After creation, it reloads the skill loader and persists the skill's description to long-term memory so the agent remembers it across sessions.",
  "concepts": [
    "CreateSkillTool",
    "SKILL.md",
    "YAML_frontmatter",
    "skill_loader",
    "memory_persistence",
    "name_validation",
    "overwrite_protection",
    "trust_level_high",
    "allowed_tools",
    "skill_directory"
  ],
  "categories": [
    "tools",
    "skill-generation",
    "extensibility",
    "memory"
  ],
  "source_docs": [
    "e42412b295dc4a64"
  ],
  "backlinks": null,
  "word_count": 510,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`skill_gen.py` (Phase 1 Quick Wins) implements a `create_skill` tool that makes PocketPaw's skill system self-extending: an agent can define a new skill — a named, reusable instruction set — on the fly and have it immediately available for invocation. This turns the agent from a fixed-capability system into one that can bootstrap new behaviors on demand.

## SKILL.md File Format

Skills are stored as markdown files with YAML frontmatter in `~/.claude/skills/<skill_name>/SKILL.md`. The frontmatter carries metadata:

```
---
name: summarize-pr
description: Summarize a GitHub PR in three bullets
user-invocable: true
allowed-tools:
  - web_search
  - url_extract
---

Instructions for the skill go here...
```

The `allowed-tools` list constrains which tools the skill can invoke, acting as a capability fence. `user-invocable` controls whether the skill appears in user-facing menus or is agent-internal only.

## Name Validation

```python
_VALID_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
```

Skill names must be lowercase alphanumeric with hyphens or underscores, starting with a letter, and capped at 64 characters. This constraint is enforced before any file I/O. The motivation is filesystem safety — skill names become directory names, and characters like `/`, `..`, or spaces would allow path traversal or create directories with names that are hard to manage programmatically.

## Overwrite Protection

```python
if skill_file.exists():
    return self._error(f"Skill '{skill_name}' already exists at {skill_file}. Delete it first.")
```

The tool refuses to silently overwrite an existing skill. Without this guard, an agent that calls `create_skill` twice with the same name would destroy the first version. The error message explicitly tells the agent how to proceed (delete first), which is important for an LLM caller that needs actionable feedback.

## Immediate Activation

```python
loader = get_skill_loader()
loader.reload()
```

After writing the file, the tool calls `loader.reload()` so the new skill is available in the same session without a restart. The reload is wrapped in a try/except — if the skill loader isn't initialized yet (e.g., in a test environment), the skill file is still written successfully and will be picked up on the next loader initialization.

## Memory Persistence

```python
await mm.remember(
    f"Created skill '{skill_name}': {description}. Saved at {skill_file}.",
    tags=["skill", skill_name],
    header=f"Skill: {skill_name}",
)
```

The skill's name and description are stored in long-term memory with a stable `header` key. Before writing, any stale memory entry for the same skill name is deleted (a search-and-delete loop). This prevents duplicate memories accumulating when a skill is recreated after deletion. The memory ensures the agent knows what skills exist even if the skill loader isn't queried directly.

## Trust Level: High

`create_skill` runs at `high` trust — one tier below `critical`. Writing to `~/.claude/skills/` is a persistent filesystem change that affects all future sessions, so it warrants elevated trust without requiring the Guardian check that `critical` tools trigger.

## Known Gaps

- No skill versioning: recreating a skill requires deleting the old file first, with no rollback.
- The stale-memory cleanup uses a semantic search (`mm.search(f"Skill: {skill_name}")`) which could match unrelated memories if the skill name appears elsewhere.
- Skills are global to the user's machine — there is no per-project or per-agent scoping.
