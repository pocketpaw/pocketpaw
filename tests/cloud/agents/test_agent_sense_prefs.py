# tests/cloud/agents/test_agent_sense_prefs.py
# Created 2026-08-02 (Sense Phase 2, SP2-2 — agent-tier provider preference) —
# pins ``AgentConfig.sense_prefs`` at both boundaries it has to hold:
#   * SCHEMA — keys are validated as sense ids (unknown/malformed paw.* ids and
#     non-ids are rejected at write time); vendor-extension ids are accepted;
#     connector-name VALUES are deliberately unvalidated.
#   * PERSISTENCE — the field round-trips through the doc<->spec mappers AND
#     survives an unrelated config update. That second case is the load-bearing
#     one: ``service.update`` rewrites the whole config sub-doc from the domain
#     spec, so a doc-only field would be silently erased on the next edit.
"""``AgentConfig.sense_prefs`` validates its keys and survives updates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("pocketpaw_ee")

from beanie import PydanticObjectId  # noqa: E402
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind  # noqa: E402
from pocketpaw_ee.cloud.agents import service as agents_service  # noqa: E402
from pocketpaw_ee.cloud.agents.domain import AgentConfigSpec  # noqa: E402
from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest, UpdateAgentRequest  # noqa: E402
from pocketpaw_ee.cloud.models.agent import Agent as AgentDoc  # noqa: E402
from pocketpaw_ee.cloud.models.agent import AgentConfig  # noqa: E402
from pydantic import ValidationError  # noqa: E402

# ---------------------------------------------------------------------------
# Schema boundary — key validation (no Mongo)
# ---------------------------------------------------------------------------


def test_default_is_empty() -> None:
    assert AgentConfig().sense_prefs == {}


def test_valid_core_sense_ids_accepted() -> None:
    config = AgentConfig(sense_prefs={"paw.code.v1": "gitlab", "paw.email.v1": "gmail"})
    assert config.sense_prefs["paw.code.v1"] == "gitlab"


def test_vendor_extension_sense_id_accepted() -> None:
    # The non-paw namespace is open — only the closed paw.* set is policed.
    config = AgentConfig(sense_prefs={"acme.crm.v1": "salesforce"})
    assert config.sense_prefs["acme.crm.v1"] == "salesforce"


def test_unknown_core_sense_id_key_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(sense_prefs={"paw.telepathy.v1": "gitlab"})


def test_malformed_sense_id_key_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(sense_prefs={"not a sense id": "gitlab"})


def test_connector_name_values_are_not_validated() -> None:
    # A value naming a connector that doesn't exist is legal at the schema
    # boundary — the resolver skips it at resolve time instead.
    config = AgentConfig(sense_prefs={"paw.code.v1": "no-such-connector"})
    assert config.sense_prefs["paw.code.v1"] == "no-such-connector"


# ---------------------------------------------------------------------------
# Mapper round-trip (no Mongo)
# ---------------------------------------------------------------------------


def test_mapper_round_trip_preserves_sense_prefs() -> None:
    spec = AgentConfigSpec(sense_prefs=(("paw.code.v1", "gitlab"),))
    doc = agents_service._config_to_doc(spec)
    assert doc.sense_prefs == {"paw.code.v1": "gitlab"}
    assert agents_service._config_to_domain(doc).sense_prefs == (("paw.code.v1", "gitlab"),)


def test_mapper_default_is_empty() -> None:
    assert agents_service._config_to_domain(AgentConfig()).sense_prefs == ()
    assert agents_service._config_to_doc(AgentConfigSpec()).sense_prefs == {}


# ---------------------------------------------------------------------------
# Persistence — the prefs survive an unrelated config update
# ---------------------------------------------------------------------------


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.mark.usefixtures("mongo_db")
async def test_sense_prefs_survive_an_unrelated_config_update(recording_bus) -> None:
    """``update`` rewrites the config sub-doc wholesale when any field changes.
    A pref written directly on the doc (SP2-5 adds the CRUD surface) must still
    be there afterwards."""
    agent = await agents_service.create(
        _ctx(), "w1", CreateAgentRequest(name="Coder", slug="coder", soul_enabled=False)
    )

    doc = await AgentDoc.get(PydanticObjectId(agent.id))
    doc.config.sense_prefs = {"paw.code.v1": "gitlab"}
    await doc.save()

    # Touch something else entirely.
    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(config={"temperature": 0.4})
    )

    assert updated.config.sense_prefs == (("paw.code.v1", "gitlab"),)
    reloaded = await agents_service.get(agent.id)
    assert reloaded.config.sense_prefs == (("paw.code.v1", "gitlab"),)
