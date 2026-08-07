# tests/cloud/agents/test_config_wire_parity.py
# Created 2026-08-07 (C4-b, feat/coupling-c4b-agentconfig-parity) — guards the
# four-way AgentConfig mirror so a field added to one leg cannot silently fail to
# reach the others:
#   * FIELD SETS: dataclasses.fields(AgentConfigSpec) == _config_to_dict() keys
#     == Beanie AgentConfig.model_fields. This is the gate that would have caught
#     ``tool_mode`` — it lived on the doc, the spec, the service mappers and the
#     run-time enforcement for two weeks while never once reaching the wire.
#   * SENTINEL ROUND-TRIP: spec -> doc -> spec with a NON-DEFAULT value on every
#     field. The service mappers pass fields explicitly by keyword, so a dropped
#     field does not raise — it silently reverts to its default. Only distinct
#     sentinel values catch that; a default-valued round-trip passes either way.
#   * tool_mode specifically: emitted on the wire, and its legal set matches what
#     run_core._agent_tool_policy actually branches on.
#
# WHY HERE. It sits with the other agents tests but imports only ``domain`` +
# ``dto`` (+ ``models.agent`` and ``service``'s pure mappers) — no Mongo fixture,
# no event bus, no HTTP. So it runs in milliseconds and fails at the cheapest
# possible point: the next person to add a field to AgentConfigSpec sees THIS
# test go red before any integration test does, with a message naming the missing
# key. Putting it in a DTO-only file elsewhere would have cost the doc-leg
# comparison; putting it behind the ``mongo_db`` fixture would have made the
# cheapest gate the slowest one.
"""The AgentConfig mirror (spec / doc / wire dict) stays field-for-field aligned."""

from __future__ import annotations

import dataclasses

import pytest
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.domain import (
    TOOL_MODE_PATTERN,
    TOOL_MODES,
    AgentConfigSpec,
)
from pocketpaw_ee.cloud.agents.dto import (
    CreateAgentRequest,
    UpdateAgentRequest,
    _config_to_dict,
)
from pocketpaw_ee.cloud.chat.runs.run_core import _agent_tool_policy
from pocketpaw_ee.cloud.models.agent import AgentConfig as AgentConfigDoc


def _spec_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(AgentConfigSpec)}


# ---------------------------------------------------------------------------
# Field-set parity across the mirror legs
# ---------------------------------------------------------------------------


def test_wire_dict_emits_every_spec_field():
    """``_config_to_dict`` must emit one key per ``AgentConfigSpec`` field.

    A field only on the spec is invisible to every client; a key only in the
    dict is one the FE would read and the server would never write.
    """
    emitted = set(_config_to_dict(AgentConfigSpec()))
    spec = _spec_field_names()
    assert emitted == spec, (
        f"wire dict is missing {sorted(spec - emitted)} and emits unknown {sorted(emitted - spec)}"
    )


def test_beanie_doc_mirrors_every_spec_field():
    """The Beanie ``AgentConfig`` sub-model mirrors the domain spec field-for-field.

    ``domain.py``'s own docstring promises this ("mirrors this domain
    AgentConfigSpec field-for-field"); nothing enforced it until now.
    """
    doc_fields = set(AgentConfigDoc.model_fields)
    spec = _spec_field_names()
    assert doc_fields == spec, (
        f"Beanie AgentConfig is missing {sorted(spec - doc_fields)} "
        f"and has extra {sorted(doc_fields - spec)}"
    )


# ---------------------------------------------------------------------------
# Sentinel round-trip — catches a field the mappers silently default
# ---------------------------------------------------------------------------

#: One NON-DEFAULT value per ``AgentConfigSpec`` field. Deliberately exhaustive:
#: ``test_sentinel_spec_covers_every_field`` fails when a new field is added
#: without a sentinel, which is what forces this map to stay complete.
_SENTINELS: dict[str, object] = {
    "backend": "sentinel_backend",
    "model": "sentinel-model",
    "system_prompt": "sentinel prompt",
    "tools": ("mcp__code__read",),
    "tool_mode": "exclusive",
    "trust_level": 5,
    "temperature": 0.11,
    "max_tokens": 1234,
    "scopes": ("org:sentinel",),
    "skill_refs": ("sentinel-skill",),
    "plugins": ("sentinel-plugin",),
    "soul_enabled": False,
    "soul_persona": "sentinel persona",
    "soul_archetype": "The Sentinel",
    "soul_values": ("sentinel-value",),
    "soul_ocean": (("openness", 0.13),),
    "welcome_message": "sentinel welcome",
    "conversation_starters": ("sentinel starter",),
    "voice": {"provider": "sentinel"},
    "appearance": {"accent": "#5e5"},
}


def test_sentinel_spec_covers_every_field():
    """Every spec field has a sentinel, and no sentinel equals its default."""
    spec = _spec_field_names()
    assert set(_SENTINELS) == spec, (
        f"add a NON-DEFAULT sentinel for {sorted(spec - set(_SENTINELS))} "
        f"(stale sentinels: {sorted(set(_SENTINELS) - spec)})"
    )
    default = AgentConfigSpec()
    same = [k for k, v in _SENTINELS.items() if getattr(default, k) == v]
    assert not same, f"sentinels equal to the default prove nothing: {same}"


def test_mappers_round_trip_every_field():
    """spec -> doc -> spec preserves every field.

    The mappers copy field-by-field, so a forgotten field does NOT raise — the
    dataclass just fills in its default. Sentinel values are the only way to
    tell "copied" from "silently defaulted".
    """
    spec = AgentConfigSpec(**_SENTINELS)  # type: ignore[arg-type]
    back = agents_service._config_to_domain(agents_service._config_to_doc(spec))
    dropped = {
        name: (getattr(spec, name), getattr(back, name))
        for name in _spec_field_names()
        if getattr(spec, name) != getattr(back, name)
    }
    assert not dropped, f"mappers dropped/mangled fields: {dropped}"


def test_wire_dict_carries_sentinel_values():
    """The wire dict carries the VALUES, not just the keys."""
    wire = _config_to_dict(AgentConfigSpec(**_SENTINELS))  # type: ignore[arg-type]
    assert wire["tool_mode"] == "exclusive"
    assert wire["skill_refs"] == ["sentinel-skill"]
    assert wire["tools"] == ["mcp__code__read"]


# ---------------------------------------------------------------------------
# tool_mode: the wire value and the enforcement site agree on the legal set
# ---------------------------------------------------------------------------


def test_default_agent_is_advertised_as_additive():
    assert _config_to_dict(AgentConfigSpec())["tool_mode"] == "additive"


def test_exclusive_is_the_value_run_core_enforces():
    """The value the wire emits is the value the run-time gate branches on.

    If these ever drift, an agent would advertise a locked-down tool surface it
    does not actually get (or the reverse).
    """
    wire = _config_to_dict(AgentConfigSpec(tool_mode="exclusive", tools=("mcp__code__read",)))
    exclusive, tools = _agent_tool_policy(_Instance(wire))
    assert exclusive is True
    assert tools == frozenset({"mcp__code__read"})

    open_wire = _config_to_dict(AgentConfigSpec(tools=("mcp__code__read",)))
    assert _agent_tool_policy(_Instance(open_wire)) == (False, frozenset())


class _Instance:
    """Minimal stand-in for a pooled agent instance (``.config`` is a raw dict)."""

    def __init__(self, config: dict) -> None:
        self.config = config


def test_legal_tool_modes_are_exactly_additive_and_exclusive():
    assert set(TOOL_MODES) == {"additive", "exclusive"}
    assert TOOL_MODE_PATTERN == "^(additive|exclusive)$"


# ---------------------------------------------------------------------------
# Request DTOs: the field exists AND rejects an illegal value on both routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [CreateAgentRequest, UpdateAgentRequest])
@pytest.mark.parametrize("mode", TOOL_MODES)
def test_requests_accept_every_legal_tool_mode(model, mode):
    body = model(name="A", slug="a", tool_mode=mode)
    assert body.tool_mode == mode


@pytest.mark.parametrize("model", [CreateAgentRequest, UpdateAgentRequest])
def test_requests_reject_an_illegal_tool_mode(model):
    # "exlcusive" is the realistic failure: run_core treats anything that is not
    # exactly "exclusive" as additive, so a typo fails OPEN if it is not caught.
    with pytest.raises(Exception):  # pydantic ValidationError
        model(name="A", slug="a", tool_mode="exlcusive")


@pytest.mark.parametrize("model", [CreateAgentRequest, UpdateAgentRequest])
def test_requests_default_tool_mode_to_none(model):
    """None == "use the default" / "leave unchanged" — never a silent write."""
    assert model(name="A", slug="a").tool_mode is None


def test_config_dict_route_rejects_an_illegal_tool_mode():
    """The raw ``config`` dict cannot bypass the flat field's pattern.

    Before C4-b this dict WAS the only way to set ``tool_mode``, so callers use
    it. A gate the documented workaround walks around is not a gate.
    """
    with pytest.raises(Exception):  # pydantic ValidationError
        UpdateAgentRequest(config={"tool_mode": "anything-goes"})


def test_config_dict_route_still_accepts_legal_values():
    body = UpdateAgentRequest(config={"tool_mode": "exclusive", "tools": ["mcp__code__read"]})
    assert body.config == {"tool_mode": "exclusive", "tools": ["mcp__code__read"]}
    # An unrelated config dict is untouched by the validator.
    assert UpdateAgentRequest(config={"temperature": 0.4}).config == {"temperature": 0.4}
