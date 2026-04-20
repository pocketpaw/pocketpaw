from pathlib import Path

from ee.cloud.files.abac_config import AbacRule, AbacRuleSet, load_rules


def test_load_rules_empty(tmp_path: Path):
    p = tmp_path / "r.yaml"
    p.write_text("rules: []\n")
    rs = load_rules(p)
    assert rs.rules == []


def test_load_rules_parses_shape(tmp_path: Path):
    p = tmp_path / "r.yaml"
    p.write_text(
        "rules:\n"
        "  - tag: confidential\n"
        "    require:\n"
        "      role: [admin, owner]\n"
    )
    rs = load_rules(p)
    assert len(rs.rules) == 1
    r = rs.rules[0]
    assert r.tag == "confidential"
    assert r.require == {"role": ["admin", "owner"]}


def test_ruleset_allows_entry_when_untagged():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    assert rs.allows(tags=[], attributes={})


def test_ruleset_allows_when_attribute_matches():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    assert rs.allows(tags=["confidential"], attributes={"role": "admin"})


def test_ruleset_denies_when_attribute_mismatches():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    assert not rs.allows(tags=["confidential"], attributes={"role": "member"})


def test_ruleset_deny_overrides_multiple_tags():
    rs = AbacRuleSet(
        rules=[
            AbacRule(tag="confidential", require={"role": ["admin"]}),
            AbacRule(tag="pii", require={"clearance": ["high"]}),
        ]
    )
    assert not rs.allows(
        tags=["confidential", "pii"], attributes={"role": "admin", "clearance": "low"}
    )
