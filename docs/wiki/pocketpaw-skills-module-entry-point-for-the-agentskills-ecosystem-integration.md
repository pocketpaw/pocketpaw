---
{
  "title": "PocketPaw Skills Module: Entry Point for the AgentSkills Ecosystem Integration",
  "summary": "The `skills` package `__init__.py` assembles and re-exports the three subsystems that make up PocketPaw's skill integration layer: loading, installation, and execution. By surfacing a clean public API here, consumers throughout the codebase can import skill primitives from a single location without knowing the internal module structure.",
  "concepts": [
    "skills module",
    "AgentSkills",
    "SkillLoader",
    "SkillExecutor",
    "SkillInstallError",
    "skill discovery",
    "skill installation",
    "skill execution",
    "public API",
    "package init",
    "extensibility"
  ],
  "categories": [
    "skills system",
    "agent runtime",
    "architecture"
  ],
  "source_docs": [
    "a7d8b5508f8a28a1"
  ],
  "backlinks": null,
  "word_count": 339,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Role of the Skills Package

PocketPaw's skills system integrates with the AgentSkills ecosystem — a convention where AI agents discover and execute reusable task templates (skills) stored as Markdown files with YAML frontmatter. The `skills/` package provides the full lifecycle: discovering skills on disk (`loader`), installing new skills from GitHub (`installer`), and running them through the agent backend (`executor`).

The `__init__.py` is the public surface of that package. Its job is simple but important: it collapses three internal modules into one import namespace, so callers write:

```python
from pocketpaw.skills import SkillLoader, install_skill_from_source, SkillExecutor
```

instead of navigating internal module boundaries.

## Exported Public API

```python
__all__ = [
    "SkillLoader",
    "SkillInstallError",
    "get_skill_loader",
    "install_skill_from_source",
    "install_skills_from_github",
    "load_all_skills",
    "SkillExecutor",
]
```

Each export maps to a distinct concern:

- **`SkillLoader` / `get_skill_loader` / `load_all_skills`**: Discovery and parsing of skills from `~/.agents/skills/` and `~/.pocketpaw/skills/`
- **`SkillInstallError` / `install_skill_from_source` / `install_skills_from_github`**: Secure installation of skills from GitHub repositories
- **`SkillExecutor`**: Execution of loaded skills through the configured agent backend

## Skill Search Paths

The module docstring specifies two search paths:
- `~/.agents/skills/` — the central AgentSkills location, populated by `npx skills add`
- `~/.pocketpaw/skills/` — PocketPaw-specific skills installed by PocketPaw's own installer

This dual-path design means PocketPaw interoperates with the broader AgentSkills ecosystem out of the box, while also supporting PocketPaw-native skills that users install directly.

## Architectural Significance

The skills system represents PocketPaw's extensibility surface — the mechanism by which the agent's capabilities can be expanded without modifying core code. Making this package's API clean and stable is important because both internal code and third-party integrations will depend on it.

## Known Gaps

- **No version pinning for skills**: Skills installed from GitHub track a branch or the default ref. There is no mechanism to pin to a specific commit or tag, meaning a skill update on the remote could change behavior unexpectedly.
- **No skill sandboxing**: Skills are Markdown instructions executed through the agent backend — there is no explicit isolation preventing a malicious skill from issuing dangerous commands through the agent.