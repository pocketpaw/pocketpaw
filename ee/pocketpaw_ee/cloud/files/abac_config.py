"""ABAC rule loader + evaluator.

Rules restrict access only. An entry passes the ruleset IFF every rule whose
`tag` is in entry.tags has its `require` dict satisfied by ctx.user.attributes.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_DEFAULT_PATH = Path(__file__).parent / "abac_rules.yaml"


class AbacRule(BaseModel):
    tag: str
    require: dict[str, list[str]] = Field(default_factory=dict)

    def satisfied_by(self, attributes: dict[str, object]) -> bool:
        for attr, allowed in self.require.items():
            value = attributes.get(attr)
            if value not in allowed:
                return False
        return True


class AbacRuleSet(BaseModel):
    rules: list[AbacRule] = Field(default_factory=list)

    def allows(self, *, tags: list[str], attributes: dict[str, object]) -> bool:
        for rule in self.rules:
            if rule.tag in tags and not rule.satisfied_by(attributes):
                return False
        return True


def load_rules(path: Path | None = None) -> AbacRuleSet:
    src = path or _DEFAULT_PATH
    raw = yaml.safe_load(src.read_text()) or {}
    return AbacRuleSet(**raw)
