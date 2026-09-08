# ee/pocketpaw_ee/terrarium/persona.py
#
# The Foresight adapter for one citizen. ``ForesightWorld.add_agent`` duck-types
# on ``async def decide(observation) -> dict`` and nothing else, so a citizen
# joins the fan-out by wrapping the row the tick already assembled: its Beanie
# doc, its snapshot, its sense digest, the physics, and the tick's LLM
# transport. One instance per citizen per tick — it holds no state between them.
#
# The ``observation`` argument is IGNORED on purpose. Foresight's v0.1
# observation is a stub over its own toy state (``{tick, state, active_count}``)
# while the terrarium's world state already rides the digest that
# ``world.build_digest`` assembled from the Journal. Reading the stub would only
# add a second, poorer view of the same tick.
#
# ``drift`` is how far this citizen's OCEAN sits from its parent's, in drift
# widths. The service computes it — it needs the parent's document, and only
# service.py may read those — and the persona speaks it into the prompt so a
# lineage difference reaches the model instead of only the database. None for a
# founder, and for a descendant whose parent is gone.
#
# ``doc`` is typed ``Any`` deliberately: ``service.py`` is the only module
# allowed to import the Beanie document classes (the 4-file entity rule), and
# this adapter needs nothing from the doc but ``soul_path``.

"""One citizen, dressed as a Foresight persona."""

from __future__ import annotations

from typing import Any

from pocketpaw_ee.foresight.persona import OceanDrift
from pocketpaw_ee.terrarium import llm as citizen_llm
from pocketpaw_ee.terrarium.physics import PhysicsFile
from pocketpaw_ee.terrarium.world import CitizenSnapshot, SenseDigest


class CitizenPersona:
    """One citizen's judgment call, in the shape ``ForesightWorld`` registers."""

    def __init__(
        self,
        doc: Any,
        snap: CitizenSnapshot,
        digest: SenseDigest,
        physics: PhysicsFile,
        llm: citizen_llm.CitizenLlm,
        drift: OceanDrift | None = None,
    ) -> None:
        self.doc = doc
        self.snap = snap
        self.digest = digest
        self.physics = physics
        self.llm = llm
        self.drift = drift

    @property
    def has_fidelity(self) -> bool:
        """True when a real ``.soul`` archive stands behind this citizen."""
        return bool(getattr(self.doc, "soul_path", None))

    async def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        decision = await citizen_llm.decide_tick(
            self.physics,
            self.snap,
            self.digest,
            llm=self.llm,
            drift_line=self.drift.as_prompt_block() if self.drift else "",
        )
        return decision.model_dump()


__all__ = ["CitizenPersona"]
