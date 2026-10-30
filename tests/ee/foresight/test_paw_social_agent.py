# tests/ee/foresight/test_paw_social_agent.py
# Created: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
#
# Pin the PawSocialAgent contract (RFC §7.2):
#   - make_paw_social_agent constructs an oasis.SocialAgent subclass
#     when OASIS_AVAILABLE.
#   - The subclass's ``perform_action_by_llm`` delegates to the
#     wrapped persona's decide().
#   - The subclass's ``decide`` mirrors ``perform_action_by_llm``.
#   - The OASIS UserInfo carries the persona's identity block +
#     OCEAN drift + has_fidelity.
#   - Each call to the factory gets a unique integer agent id.
#   - The factory raises RuntimeError when OASIS is not loaded
#     (clear pointer to OASIS_AVAILABLE).
#
# Tests skip themselves when OASIS_AVAILABLE is False so they can
# run in environments without the full substrate (CI matrix that
# doesn't install ee[foresight]).

from __future__ import annotations

import pytest
from pocketpaw_ee.foresight.llm.adapter import DeterministicFakeBackend
from pocketpaw_ee.foresight.persona import (
    OceanDrift,
    SoulSeededPersona,
    make_paw_social_agent,
    reset_paw_social_agent_counter,
)
from pocketpaw_ee.foresight.substrate import oasis

# Skip the entire module when OASIS isn't loaded.
pytestmark = pytest.mark.skipif(
    not oasis.OASIS_AVAILABLE,
    reason=f"OASIS substrate not loaded; OASIS_LOAD_ERROR={oasis.OASIS_LOAD_ERROR!r}",
)


@pytest.fixture(autouse=True)
def _reset_counter():
    """Reset the integer agent-id counter between tests."""
    reset_paw_social_agent_counter()


# --- Construction ---------------------------------------------------


def test_make_paw_social_agent_returns_oasis_subclass():
    persona = SoulSeededPersona(name="alice", backend=DeterministicFakeBackend())
    agent = make_paw_social_agent(persona=persona)
    assert isinstance(agent, oasis.SocialAgent)


def test_make_paw_social_agent_assigns_unique_ids():
    p1 = SoulSeededPersona(name="a", backend=DeterministicFakeBackend())
    p2 = SoulSeededPersona(name="b", backend=DeterministicFakeBackend())
    a1 = make_paw_social_agent(persona=p1)
    a2 = make_paw_social_agent(persona=p2)
    assert a1.social_agent_id != a2.social_agent_id


def test_make_paw_social_agent_user_info_carries_persona_identity():
    persona = SoulSeededPersona(
        name="alice",
        backend=DeterministicFakeBackend(),
        role="approver",
        ocean_drift=OceanDrift(conscientiousness=1.5),
    )
    agent = make_paw_social_agent(persona=persona)

    # Inspect the OASIS-side user_info to confirm the persona's
    # identity + OCEAN drift made it through.
    ui = agent.user_info
    assert ui.name == "alice"
    # The description is the persona's identity block — should contain
    # the role and the OCEAN drift narration.
    assert "approver" in ui.description
    assert "conscientious" in ui.description
    profile = ui.profile or {}
    assert profile["role"] == "approver"
    assert profile["ocean_drift"]["conscientiousness"] == 1.5
    assert profile["has_fidelity"] is False  # no paw_agent attached


# --- Delegation -----------------------------------------------------


async def test_decide_delegates_to_wrapped_persona():
    backend = DeterministicFakeBackend(
        responses=["action=approve; rationale=looks good; put=status:approved"]
    )
    persona = SoulSeededPersona(name="alice", backend=backend, role="approver")
    agent = make_paw_social_agent(persona=persona)

    result = await agent.decide({"tick": 0, "state": {}, "active_count": 1})
    assert result["action"] == "approve"
    assert result["put"] == {"status": "approved"}


async def test_perform_action_by_llm_delegates_to_wrapped_persona():
    backend = DeterministicFakeBackend(responses=["action=ack; rationale=done; put=k:v"])
    persona = SoulSeededPersona(name="alice", backend=backend)
    agent = make_paw_social_agent(persona=persona)

    result = await agent.perform_action_by_llm()
    assert result["action"] == "ack"
    assert result["put"] == {"k": "v"}


# --- Failure mode ----------------------------------------------------


def test_make_paw_social_agent_raises_when_oasis_unavailable(monkeypatch):
    """When OASIS_AVAILABLE is patched False, the factory raises
    RuntimeError pointing the caller at the SoulSeededPersona path.
    """
    monkeypatch.setattr("pocketpaw_ee.foresight.substrate.oasis.OASIS_AVAILABLE", False)
    persona = SoulSeededPersona(name="x", backend=DeterministicFakeBackend())
    with pytest.raises(RuntimeError, match="OASIS_AVAILABLE=False"):
        make_paw_social_agent(persona=persona)
