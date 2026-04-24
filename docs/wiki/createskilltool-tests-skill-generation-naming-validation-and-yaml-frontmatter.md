---
{
  "title": "CreateSkillTool Tests — Skill Generation, Naming Validation, and YAML Frontmatter",
  "summary": "This test file validates `CreateSkillTool`, which allows agents to generate new PocketPaw skill files (SKILL.md) at runtime. Tests cover tool metadata, skill name regex validation, YAML frontmatter format, overwrite protection, skill loader reload triggering, and the allowed-tools and user-invocable flags.",
  "concepts": [
    "CreateSkillTool",
    "skill generation",
    "SKILL.md",
    "YAML frontmatter",
    "skill name validation",
    "_VALID_SKILL_NAME",
    "allowed tools",
    "user-invocable",
    "overwrite protection",
    "skill loader reload",
    "trust level high",
    "meta-tool"
  ],
  "categories": [
    "testing",
    "tools",
    "skills",
    "agent extensibility",
    "test"
  ],
  "source_docs": [
    "f6ef2bc89f2c7692"
  ],
  "backlinks": null,
  "word_count": 408,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_skill_gen.py` (created 2026-02-06) tests `CreateSkillTool` from `pocketpaw.tools.builtin.skill_gen`. This is a meta-tool: it generates new tool skill definitions that extend PocketPaw's capability set without code changes. The output is a `SKILL.md` file in a dedicated directory, following the skill YAML frontmatter format.

## Tool Metadata

`TestCreateSkillTool` verifies:

- `name == "create_skill"`.
- `trust_level == "high"` — higher than standard because this tool writes files and modifies the runtime skill registry. An agent with only standard trust cannot self-extend.
- Parameters schema includes `skill_name`, `description`, `instructions`, `allowed_tools`, and `user_invocable`.

## Skill Name Validation

`_VALID_SKILL_NAME` is a module-level regex that enforces naming rules:

- **Valid** — lowercase letters, digits, hyphens, underscores; must start with a letter.
- **Invalid** — uppercase letters, leading digits, spaces, leading hyphens, empty string.

```python
assert _VALID_SKILL_NAME.match("my-skill")
assert _VALID_SKILL_NAME.match("summarize_pr")
assert not _VALID_SKILL_NAME.match("My-Skill")   # uppercase
assert not _VALID_SKILL_NAME.match("123start")   # starts with digit
assert not _VALID_SKILL_NAME.match("-starts-with-dash")
```

The regex is enforced at `execute` time: `test_invalid_skill_name_rejected` asserts that passing an invalid name returns an error message without creating any files.

## Skill Creation — Happy Path

`test_create_skill_success` patches `_get_skills_dir` to return `temp_skills_dir` and calls `tool.execute(skill_name="test-skill", description="...", instructions="...")`. Assertions:

- Result contains `"created successfully"`.
- `temp_skills_dir / "test-skill" / "SKILL.md"` exists.
- The file contains YAML frontmatter with `name`, `description`, and `user-invocable: true`.
- The instructions body is present.

## Allowed Tools and User-Invocable Flags

`test_create_skill_with_allowed_tools` — passing `allowed_tools=["read_file", "web_search"]` causes those tools to appear in the frontmatter's `allowed-tools` list.

`test_create_skill_not_user_invocable` — passing `user_invocable=False` writes `user-invocable: false` in the frontmatter. This creates internal/agent-only skills that don't appear in the `/` command menu.

## Overwrite Protection

`test_overwrite_protection` — if a skill directory already exists, a second `execute` call with the same name returns an error and does not overwrite the existing file. This prevents accidental destruction of manually customized skills.

## Skill Loader Reload

`test_skill_loader_reload_called` — after creating a skill, the tool attempts to trigger a reload of the skill loader so the new skill is immediately available without restarting PocketPaw.

## YAML Frontmatter Format

`test_yaml_frontmatter_format` explicitly checks the structure of the generated file:

- `---` delimiters are present.
- `name:`, `description:`, and `user-invocable:` keys are present.
- The instructions appear in the body after the closing `---`.

## Known Gaps

No `TODO` or `FIXME` markers. Tests do not cover: creating skills with the same name in different paths (path resolution priority), generating skills with complex multi-step instructions that include code blocks, or behavior when `_get_skills_dir` returns a read-only path.
