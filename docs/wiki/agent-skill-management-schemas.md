---
{
  "title": "Agent Skill Management Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's skill management API — listing installed skills, installing new ones from a GitHub source, and removing existing ones. The install schema enforces a minimum source format to prevent obviously malformed installation requests.",
  "concepts": [
    "SkillInfo",
    "SkillInstallRequest",
    "SkillRemoveRequest",
    "skills",
    "slash commands",
    "GitHub source",
    "agent extensions",
    "skills.sh",
    "Pydantic validation",
    "skill lifecycle"
  ],
  "categories": [
    "api-schemas",
    "skills",
    "extensibility",
    "agent-capabilities"
  ],
  "source_docs": [
    "f240a20653bc2347"
  ],
  "backlinks": null,
  "word_count": 517,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Skills are installable extensions that add new slash-command capabilities to a PocketPaw agent. They are distributed from GitHub repositories and follow a naming convention of `owner/repo` or `owner/repo/skill`. This file defines the three models that form the skill management API's request and response surface.

## What Are Skills?

Skills are markdown or prompt files that teach the agent how to perform a specific task when a user invokes a slash command. For example, a `security-review` skill might guide the agent through a security checklist. Skills are installed from GitHub, stored locally, and made available to the agent's slash-command dispatcher.

## Models

### `SkillInfo`

```python
class SkillInfo(BaseModel):
    name: str
    description: str = ""
    argument_hint: str = ""
```

The metadata model for a single installed skill. `name` is the slash-command identifier (e.g. `"security-review"`). `description` is a short human-readable explanation shown in skill listings and help text. `argument_hint` provides usage guidance for skills that accept arguments — for example, `"<branch>"` or `"[--verbose]"`. Both optional fields default to empty strings so the list endpoint works even for minimal skill definitions that omit metadata.

### `SkillInstallRequest`

```python
class SkillInstallRequest(BaseModel):
    source: str = Field(..., min_length=3, description="owner/repo or owner/repo/skill")
```

The install endpoint accepts a GitHub source reference. The `min_length=3` constraint catches obviously invalid inputs — a valid `owner/repo` reference needs at least three characters (a single-char owner, a slash, and a single-char repo). While this doesn't validate the full `owner/repo[/skill]` format, it blocks empty strings and single-character garbage inputs before they reach the GitHub API.

The source format supports two variants:
- `owner/repo` — installs all skills from the repository.
- `owner/repo/skill` — installs a single named skill from the repository.

This mirrors how `skills.sh` (the PocketPaw skill distribution mechanism) resolves skill sources, keeping the API consistent with the CLI workflow.

### `SkillRemoveRequest`

```python
class SkillRemoveRequest(BaseModel):
    name: str = Field(..., min_length=1)
```

Removes an installed skill by name. `min_length=1` prevents empty-string removal requests that would either error ambiguously or (in a naive implementation) attempt to remove all skills. The name must match the installed skill's `name` field exactly.

## Design Observations

The skill management schema is deliberately minimal. Skills are essentially files — the complexity lives in the installation process (fetching from GitHub, parsing skill metadata, registering with the command dispatcher), not in the API contract. Keeping the schemas lean avoids over-specifying a surface that is primarily a thin REST wrapper around file operations.

The absence of a `SkillUpdateRequest` is intentional: skills are installed fresh from their source (which may have been updated on GitHub) rather than patched in place. Re-installing a skill effectively updates it.

## Known Gaps

- `SkillInstallRequest.source` has no regex validation for `owner/repo[/skill]` format. A source like `"not/a/valid/path/with/too/many/segments"` passes validation.
- No version pinning — skills are installed at the current HEAD of the source repository. There's no way to install a specific tag or commit, which could lead to skill behaviour changing unexpectedly after a repo update.
- `SkillInfo` has no `source` or `version` field on the read model, so users can't see where an installed skill came from or when it was last updated.