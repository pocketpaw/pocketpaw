# tests/cloud/agents/test_agent_senses.py
# Created 2026-08-02 (Sense Phase 2, SP2-3 — the sense mount list) — sibling of
# test_agent_sense_prefs.py, pinning ``AgentConfig.senses`` at the same two
# boundaries the prefs field has to hold:
#   * SCHEMA — entries are validated as sense ids, so a bogus id fails on write
#     rather than silently costing the agent a capability. Vendor-extension ids
#     are accepted (only the closed paw.* set is policed).
#   * PERSISTENCE — the field round-trips through the doc<->spec mappers AND
#     survives an unrelated config update. That second case is load-bearing and
#     the stakes are higher here than for prefs: erasing the mount list doesn't
#     lose a provider choice, it silently WIDENS the agent's reach back to the
#     workspace's whole sense surface.
"""``AgentConfig.senses`` validates its entries and survives updates."""

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
# Schema boundary — entry validation (no Mongo)
# ---------------------------------------------------------------------------


def test_default_is_empty() -> None:
    # Empty is the legacy "inherit every sense the workspace can fill".
    assert AgentConfig().senses == []


def test_valid_core_sense_ids_accepted() -> None:
    config = AgentConfig(senses=["paw.email.v1", "paw.code.v1"])
    assert config.senses == ["paw.email.v1", "paw.code.v1"]


def test_vendor_extension_sense_id_accepted() -> None:
    assert AgentConfig(senses=["acme.crm.v1"]).senses == ["acme.crm.v1"]


def test_unknown_core_sense_id_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(senses=["paw.telepathy.v1"])


def test_malformed_sense_id_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(senses=["not a sense id"])


def test_one_bad_entry_rejects_the_whole_list() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(senses=["paw.email.v1", "paw.telepathy.v1"])


# ---------------------------------------------------------------------------
# Mapper round-trip (no Mongo)
# ---------------------------------------------------------------------------


def test_mapper_round_trip_preserves_senses() -> None:
    spec = AgentConfigSpec(senses=("paw.email.v1",))
    doc = agents_service._config_to_doc(spec)
    assert doc.senses == ["paw.email.v1"]
    assert agents_service._config_to_domain(doc).senses == ("paw.email.v1",)


def test_mapper_default_is_empty() -> None:
    assert agents_service._config_to_domain(AgentConfig()).senses == ()
    assert agents_service._config_to_doc(AgentConfigSpec()).senses == []


# ---------------------------------------------------------------------------
# Persistence — the mount list survives an unrelated config update
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
async def test_senses_survive_an_unrelated_config_update(recording_bus) -> None:
    """``update`` rewrites the config sub-doc wholesale when any field changes.
    A mount list written directly on the doc (SP2-5 adds the CRUD surface) must
    still be there afterwards — otherwise editing the temperature silently hands
    the agent back the whole workspace sense surface."""
    agent = await agents_service.create(
        _ctx(), "w1", CreateAgentRequest(name="Mailer", slug="mailer", soul_enabled=False)
    )

    doc = await AgentDoc.get(PydanticObjectId(agent.id))
    doc.config.senses = ["paw.email.v1"]
    await doc.save()

    # Touch something else entirely.
    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(config={"temperature": 0.4})
    )

    assert updated.config.senses == ("paw.email.v1",)
    reloaded = await agents_service.get(agent.id)
    assert reloaded.config.senses == ("paw.email.v1",)


@pytest.mark.usefixtures("mongo_db")
async def test_senses_and_prefs_survive_together(recording_bus) -> None:
    """Both sense fields ride the same erasure-prone path — pin them together so
    a future field-list edit can't drop one and leave the other passing."""
    agent = await agents_service.create(
        _ctx(), "w1", CreateAgentRequest(name="Coder", slug="coder2", soul_enabled=False)
    )

    doc = await AgentDoc.get(PydanticObjectId(agent.id))
    doc.config.senses = ["paw.code.v1"]
    doc.config.sense_prefs = {"paw.code.v1": "gitlab"}
    await doc.save()

    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(config={"model": "claude-x"})
    )

    assert updated.config.senses == ("paw.code.v1",)
    assert updated.config.sense_prefs == (("paw.code.v1", "gitlab"),)


@pytest.mark.usefixtures("mongo_db")
async def test_update_can_replace_the_mount_list(recording_bus) -> None:
    """The carry-forward default must not make the field read-only."""
    agent = await agents_service.create(
        _ctx(), "w1", CreateAgentRequest(name="Swap", slug="swap", soul_enabled=False)
    )
    doc = await AgentDoc.get(PydanticObjectId(agent.id))
    doc.config.senses = ["paw.email.v1"]
    await doc.save()

    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(config={"senses": ["paw.code.v1"]})
    )

    assert updated.config.senses == ("paw.code.v1",)


@pytest.mark.usefixtures("mongo_db")
async def test_update_rejects_a_bogus_mount_list(recording_bus) -> None:
    """Validation lives at the Beanie boundary, which the update path crosses on
    its way back to the doc — so a bad id can't be written through ``update``
    either."""
    agent = await agents_service.create(
        _ctx(), "w1", CreateAgentRequest(name="Bad", slug="bad", soul_enabled=False)
    )

    with pytest.raises(ValidationError):
        await agents_service.update(
            _ctx(), agent.id, UpdateAgentRequest(config={"senses": ["paw.telepathy.v1"]})
        )
