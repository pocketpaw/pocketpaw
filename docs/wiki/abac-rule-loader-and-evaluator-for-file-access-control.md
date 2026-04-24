---
{
  "title": "ABAC Rule Loader and Evaluator for File Access Control",
  "summary": "The `abac_config.py` module implements an Attribute-Based Access Control (ABAC) engine for the files domain, loading restriction rules from a YAML configuration file and evaluating them against per-request user attributes and file entry tags. Rules can only restrict access - they cannot grant permissions beyond the RBAC baseline.",
  "concepts": [
    "ABAC",
    "access control",
    "YAML rules",
    "tag-based filtering",
    "AbacRule",
    "AbacRuleSet",
    "user attributes",
    "restrictive policy",
    "RBAC complement",
    "file permissions"
  ],
  "categories": [
    "files",
    "cloud EE",
    "security",
    "ABAC"
  ],
  "source_docs": [
    "a67164794b2ad051"
  ],
  "backlinks": null,
  "word_count": 392,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Attribute-Based Access Control (ABAC) complements Role-Based Access Control (RBAC) by adding fine-grained, data-driven restrictions. Where RBAC says 'this user is an editor', ABAC says 'this editor can only see files tagged `internal` if their department is `engineering`'. The `abac_config.py` module implements the evaluation engine for these rules.

## Data Model

Rules are loaded from a YAML file (defaulting to `abac_rules.yaml` in the same directory):

```python
class AbacRule(BaseModel):
    tag: str
    require: dict[str, list[str]] = Field(default_factory=dict)
```

Each rule has a `tag` (e.g., `"confidential"`) and a `require` dict mapping user attribute keys to lists of allowed values.

## Evaluation Logic

The evaluation is **restrictive**: an entry passes the ruleset if and only if every rule whose tag appears on the entry is satisfied by the user's attributes. If the user lacks any required attribute, or if the attribute value is not in the allowed list, access is denied.

```python
def allows(self, *, tags: list[str], attributes: dict[str, object]) -> bool:
    for rule in self.rules:
        if rule.tag in tags and not rule.satisfied_by(attributes):
            return False
    return True
```

Rules with tags that do not appear on the entry are ignored entirely. An untagged entry is not affected by any rule - it relies solely on RBAC for access control.

## Why Restrict-Only?

Making ABAC purely restrictive simplifies the security model. Administrators cannot accidentally create ABAC rules that bypass RBAC. The permission hierarchy is fixed: RBAC baseline -> ABAC restriction -> derived capabilities.

## YAML Loading

```python
def load_rules(path: Path | None = None) -> AbacRuleSet:
    src = path or _DEFAULT_PATH
    raw = yaml.safe_load(src.read_text()) or {}
    return AbacRuleSet(**raw)
```

The `or {}` guard handles an empty YAML file (which `yaml.safe_load` returns as `None`) without raising a `TypeError`. A missing or empty rules file results in a permissive ruleset (no restrictions) rather than a crash.

## Integration in the Files Domain

`load_rules()` is called once at application startup by `bootstrap.py` and the resulting `AbacRuleSet` is passed to `build_router`. At request time, `browse.py` calls `rules.allows(tags=entry.tags, attributes=ctx.attributes)` for each file entry before including it in the response.

## Known Gaps

- Rule evaluation is binary (allow/deny). Future work could extend rules to restrict specific *capabilities* on allowed entries (e.g., allow listing but block download for certain tags).
- The YAML path is resolved at module load time using `__file__`, which may behave unexpectedly in editable installs with symlinked directories.