---
{
  "title": "SkillLoader: Discovery, Parsing, and Hot-Reload of AgentSkills from Disk",
  "summary": "The `SkillLoader` discovers and parses skills from two filesystem paths — the central AgentSkills directory and a PocketPaw-specific directory — by reading YAML frontmatter and Markdown content from `SKILL.md` files. It supports hot-reloading, invocability filtering, search, and a singleton accessor for shared use across the application.",
  "concepts": [
    "SkillLoader",
    "SKILL.md",
    "AgentSkills convention",
    "hot-reload",
    "YAML frontmatter",
    "skill discovery",
    "invocable skills",
    "search",
    "skill parsing",
    "singleton pattern",
    "filesystem scanning"
  ],
  "categories": [
    "skills system",
    "agent runtime",
    "file system"
  ],
  "source_docs": [
    "a326ff76a4f9fc93"
  ],
  "backlinks": null,
  "word_count": 485,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The AgentSkills File Convention

Skills follow a simple convention: a directory containing a `SKILL.md` file. `SKILL.md` combines YAML frontmatter (name, description, tags, invocable flag) with Markdown instructions that form the skill's prompt body. This plain-file format makes skills easy to author, version-control, and share.

## Two Search Paths

`SKILL_PATHS` defines two locations the loader checks:

1. `~/.agents/skills/` — The central AgentSkills ecosystem directory, populated by `npx skills add` or equivalent
2. `~/.pocketpaw/skills/` — PocketPaw-specific skills installed by the PocketPaw installer

Checking both paths allows PocketPaw to participate in the broader AgentSkills ecosystem while also supporting its own skill distribution channel. The `SkillLoader.__init__` also accepts `extra_paths` for non-standard locations.

## The `Skill` Dataclass

`Skill` represents a loaded, parsed skill. Its `build_prompt(args)` method combines the skill's static instruction body with runtime user arguments to produce the final prompt sent to the agent backend. This separation — static definition vs. runtime invocation — is fundamental: skill authors write instructions once; users provide context at invocation time.

## Invocability Filtering

`get_invocable()` returns only skills whose frontmatter marks them as directly invocable (i.e., they have a clear entry point and are not helper/partial skills). This distinction prevents the dashboard from surfacing internal utility skills that are not meant for direct user invocation.

## Search

`search(query)` performs a simple text match against skill names, descriptions, and tags. This powers the dashboard's skill search UI, allowing users to find skills by keyword without knowing exact names.

## Hot-Reloading

The loader supports hot-reloading when skills change on disk. This means a developer can add or modify a `SKILL.md` file and have it immediately available to PocketPaw without restarting the server. The implementation watches file modification times or re-scans directories on each access (exact mechanism determined by implementation).

## YAML Frontmatter Parsing

The loader uses the `yaml` library to parse frontmatter from `SKILL.md` files. This introduces a dependency on `pyyaml`, which must be available in the environment. The loader handles missing or malformed frontmatter gracefully — a skill with no frontmatter is still loaded as an unnamed, untagged skill rather than causing a crash.

## Singleton Access

`get_skill_loader()` returns a module-level singleton `SkillLoader` initialized with the default `SKILL_PATHS`. The singleton ensures skill discovery happens once and the parsed skill list is shared across the application, avoiding repeated filesystem scans per request.

## Known Gaps

- **No schema validation for frontmatter**: The YAML frontmatter is parsed but not validated against a schema. A skill with a misspelled frontmatter key (e.g., `invoceable: true` instead of `invocable: true`) would silently fail to be marked as invocable.
- **Hot-reload granularity**: Hot-reloading likely re-scans entire directories rather than watching individual files. On directories with many skills, this could be expensive if triggered on every access.
- **No skill deduplication**: If the same skill name exists in both `~/.agents/skills/` and `~/.pocketpaw/skills/`, the loader's precedence behavior is not documented — it may silently return one or the other, or both.