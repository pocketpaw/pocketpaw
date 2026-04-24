---
{
  "title": "SkillLoader and SKILL.md Parsing Tests",
  "summary": "This test file validates `SkillLoader` and `parse_skill_md` from `pocketpaw.skills.loader`, covering SKILL.md frontmatter parsing, optional field handling, no-frontmatter rejection, directory-name fallback for skill naming, prompt argument substitution, loader lifecycle, skill retrieval, reload, multi-path override resolution, and integration with real skill paths.",
  "concepts": [
    "SkillLoader",
    "parse_skill_md",
    "SKILL.md",
    "YAML frontmatter",
    "skill name fallback",
    "argument substitution",
    "user-invocable",
    "disable-model-invocation",
    "argument-hint",
    "skill reload",
    "multi-path override",
    "agent skills"
  ],
  "categories": [
    "testing",
    "skills",
    "loader",
    "agent extensibility",
    "test"
  ],
  "source_docs": [
    "a4b4e73d3fcd6557"
  ],
  "backlinks": null,
  "word_count": 432,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_skills.py` tests the skill loading subsystem. Skills in PocketPaw are markdown files with YAML frontmatter that define agent capabilities — slash commands, prompt templates, and tool restrictions. `SkillLoader` discovers these files from one or more directories and makes them available to the runtime.

## SKILL.md Parsing

`TestSkillParsing` covers `parse_skill_md(path)`:

- **Basic skill** — a SKILL.md with `name` and `description` in frontmatter parses to a `Skill` object with `user_invocable=True` by default.
- **Optional fields** — `user-invocable: false`, `disable-model-invocation: true`, and `argument-hint: "[filename]"` are parsed and stored on the `Skill` object. `disable_model_invocation` is used to flag skills that should be executed directly without sending to an LLM.
- **No frontmatter** — a SKILL.md without `---` delimiters returns `None` rather than raising. This allows directories with stray markdown files to be safely scanned.
- **Fallback name** — if the frontmatter lacks a `name` field, the skill's name defaults to the parent directory name (`"my-custom-skill"`). This means skill authors can omit the name without breaking discovery.

```python
def test_parse_skill_fallback_name(self, tmp_path):
    skill_md.write_text("---
description: Skill without name field
---
Content")
    skill = parse_skill_md(skill_md)
    assert skill.name == "my-custom-skill"
```

## Prompt Building with Argument Substitution

`TestSkillPromptBuilding` tests `Skill.build_prompt(args)`:

- **No args** — prompt content is returned as-is.
- **With arguments** — named `{arg}` placeholders in the content are replaced.
- **Positional args** — positional substitution via `{0}`, `{1}`, etc.

Argument substitution allows skill authors to write parameterized prompts: `/summarize <URL>` substitutes the URL into the prompt template.

## SkillLoader Lifecycle

`TestSkillLoader` tests `SkillLoader` with a temp directory pre-populated with synthetic SKILL.md files:

- **Load skills** — `loader.load_skills()` discovers all SKILL.md files in the configured paths.
- **Get by name** — `loader.get_skill("test-skill")` returns the correct `Skill`.
- **Get nonexistent** — returns `None`.
- **List names** — `loader.list_skill_names()` returns all loaded skill names.
- **Reload** — after adding a new SKILL.md, `loader.reload()` picks it up without restarting.

## Integration Test

`TestSkillLoaderIntegration` — `test_loads_from_agents_skills` instantiates a `SkillLoader` with the real agent skills path and asserts at least one skill is loaded. This is a smoke test against the actual installed skills directory.

## Multi-Path Override Resolution

`TestSkillLoaderOverride` — when the same skill name exists in two configured paths, the later path's version wins. This is the override mechanism: user skills in `~/.config/pocketpaw/skills/` override built-in skills in the package directory.

```python
def test_later_path_overrides_earlier(self, tmp_path):
    # Two paths, same skill name → later path's version is returned
```

## Known Gaps

No `TODO` or `FIXME` markers. Tests don't cover invalid YAML in frontmatter (syntax errors in the `---` block), skills with identical names across more than two paths, or concurrent reload behavior.
