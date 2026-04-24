---
{
  "title": "Agent Soul Configuration and OCEAN Personality Schema Tests",
  "summary": "This module tests the `AgentConfig` model's soul-related fields — default values, persona assignment, custom OCEAN personality vectors, and absence of a `soul_path` attribute — and verifies that `CreateAgentRequest` correctly propagates `persona` and soul OCEAN customization fields.",
  "concepts": [
    "AgentConfig",
    "soul_enabled",
    "soul_persona",
    "soul_archetype",
    "soul_values",
    "soul_ocean",
    "OCEAN",
    "Soul Protocol",
    "CreateAgentRequest",
    "personality model",
    "agent configuration"
  ],
  "categories": [
    "agents",
    "soul protocol",
    "testing",
    "personality",
    "test"
  ],
  "source_docs": [
    "e7791acc689a0c41"
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

The `tests/cloud/test_agent_soul.py` module validates the integration between PocketPaw's agent runtime and the Soul Protocol personality model. In PocketPaw, every agent can have a persistent soul — a set of personality parameters that shape its behavior across conversations. The `AgentConfig` model holds these parameters, and this module tests that the defaults, validation, and schema propagation are correct.

## AgentConfig Soul Defaults

`test_agent_config_soul_defaults` instantiates an `AgentConfig` with no arguments and asserts:
- `soul_enabled` is `True` by default — souls are opt-out, not opt-in.
- `soul_persona` and `soul_archetype` default to empty strings, meaning no custom persona text is applied.
- `soul_values` defaults to `["helpfulness", "accuracy"]` — the baseline value set for any PocketPaw agent.
- `soul_ocean` contains the five OCEAN dimensions with `conscientiousness=0.85` as the key default (reflecting PocketPaw's design goal of reliable, accurate agents).

These defaults mean agents work out of the box with a coherent personality without any explicit configuration.

## OCEAN Personality Vector

The OCEAN model (Openness, Conscientiousness, Extraversion, Agreeableness, Neuroticism) is a psychological personality framework adapted for AI agents in the Soul Protocol. Each dimension is a float between 0 and 1. `test_agent_config_custom_ocean` verifies that all five dimensions can be set independently:

```python
config = AgentConfig(
    soul_ocean={
        "openness": 0.9,
        "conscientiousness": 0.5,
        "extraversion": 0.8,
        "agreeableness": 0.3,
        "neuroticism": 0.1,
    }
)
assert config.soul_ocean["extraversion"] == 0.8
```

Custom OCEAN vectors enable specialized agents — a creative writing agent might have high openness, while a compliance checker might have high conscientiousness and low neuroticism.

## Persona Field

`test_agent_config_with_persona` confirms that `soul_persona` accepts an arbitrary string and that other fields (`model`) retain their defaults. The persona is injected into the agent's system prompt, shaping how it presents itself to users.

## No soul_path Attribute

`test_agent_config_no_soul_path` asserts that `AgentConfig` does not have a `soul_path` attribute. This test guards against a design drift where a file-system soul path might be added to `AgentConfig` — in the PocketPaw architecture, soul files are managed by the Soul Protocol SDK and are not a runtime concern for `AgentConfig`.

## CreateAgentRequest Schema Tests

The two schema tests confirm that `CreateAgentRequest` propagates `persona` and `soul_ocean` into the underlying config. This is the API surface that operators and developers use when creating agents via the REST API. The tests verify the round-trip from request schema to model field without requiring a database.

## Known Gaps

No TODO or FIXME markers. The tests do not cover `soul_values` customization through `CreateAgentRequest`. The boundary between `soul_enabled=False` and the soul fields being ignored at runtime is not tested — only the schema defaults are verified. The interaction between `soul_archetype` and prompt construction is not tested here.