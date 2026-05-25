# ee/pocketpaw_ee/foresight/persona.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# SoulSeededPersona — the v0.1 persona shape. RFC 08 §7.2 calls for
# wrapping a real PawAgent inside OASIS's SocialAgent and routing
# memory through the Soul engine. v0.1 ships a *minimal* persona that
# carries OCEAN traits + a memory-tier-stub config + a backend handle,
# and delegates the actual "think" step to a pluggable backend.
#
# The persona deliberately does NOT subclass ``oasis.social_agent.SocialAgent``
# in v0.1 — the OASIS src-copy isn't vendored yet (see
# substrate/oasis/README-FORK.md). The shape here matches the public
# surface RFC 08 §7.2 specifies (OceanDrift + memory tier stub +
# delegate-to-backend), so the v1.0 wiring becomes "swap parent class"
# rather than "rewrite cognition path".

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True)
class OceanDrift:
    """Per-persona OCEAN delta (RFC §7.2).

    Values are interpreted as standard-deviation multiples in
    (-3.0, +3.0). 0.0 = baseline soul. v0.1 only carries the five
    fields; the variation engine that *samples* drifts across a
    population lands in v1.0.
    """

    openness: float = 0.0
    conscientiousness: float = 0.0
    extraversion: float = 0.0
    agreeableness: float = 0.0
    neuroticism: float = 0.0

    def as_prompt_block(self) -> str:
        """Render the drift as a short persona-prompt block.

        v0.1 returns deterministic prose ("slightly more conscientious";
        "noticeably less agreeable"). v1.0 will source the wording from
        Soul Protocol's psychology layer so the rendering is consistent
        with how the live runtime narrates personality elsewhere.
        """
        parts: list[str] = []
        labels = {
            "openness": ("more open", "less open"),
            "conscientiousness": ("more conscientious", "less conscientious"),
            "extraversion": ("more extraverted", "less extraverted"),
            "agreeableness": ("more agreeable", "less agreeable"),
            "neuroticism": ("more neurotic", "less neurotic"),
        }
        for trait, (pos, neg) in labels.items():
            value = getattr(self, trait)
            if abs(value) < 0.25:
                continue  # within noise; skip
            magnitude = "noticeably" if abs(value) >= 1.0 else "slightly"
            parts.append(f"{magnitude} {pos if value > 0 else neg}")
        if not parts:
            return "baseline temperament"
        return "; ".join(parts)


@dataclass
class MemoryTierStub:
    """v0.1 memory-tier stub. RFC §7.2 + RFC 08 architecture §5 specify
    a 5-tier memory hierarchy (core / episodic / semantic / procedural
    / graph) routed through Soul Protocol with a per-run overlay.

    v0.1 carries the *configuration* (tier names + max-entries caps) so
    the persona can be constructed with the same shape v1.0 will use,
    but the actual memory routing to Soul Protocol is deferred. The
    persona reads from ``self.scratchpad`` for now — a single-list
    in-memory store equivalent to OASIS's tick-scoped scratchpad.
    """

    tiers: dict[str, int] = field(
        default_factory=lambda: {
            "core": 0,  # 0 = unbounded; v1.0 enforces a cap
            "episodic": 200,
            "semantic": 500,
            "procedural": 100,
            "graph": 200,
        }
    )
    scratchpad: list[dict[str, Any]] = field(default_factory=list)

    def remember(self, entry: dict[str, Any]) -> None:
        """v0.1: append to the scratchpad. v1.0 will route the write
        to the configured tier via Soul Protocol's per-run overlay
        (so the real soul is never mutated unless captain-approved).
        """
        self.scratchpad.append(entry)

    def recall(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """v0.1: return the most-recent N scratchpad entries.
        v1.0 will run the recall through the Soul engine's tier-aware
        search (semantic + episodic + procedural, scored by importance).
        """
        return list(self.scratchpad[-limit:])


class SoulSeededPersona:
    """v0.1 soul-seeded persona.

    Construction: ``SoulSeededPersona(name, ocean_drift, backend, ...)``.

    The persona's ``decide`` entrypoint is what ``ForesightWorld.tick()``
    invokes per tick. ``decide`` composes a prompt from the persona's
    identity block + the world observation + the recent scratchpad,
    asks the backend to produce an action, parses the backend's
    response into an action dict, and remembers the cycle in the
    scratchpad.

    The backend interface is intentionally minimal:
      ``await backend.complete(prompt: str) -> str``
    so any object that exposes that method works as a backend — the
    Claude Code adapter, a deterministic fake (used in tests), or a
    LiteLLM proxy (the fallback path RFC §6.4 calls out).
    """

    def __init__(
        self,
        *,
        name: str,
        backend: Any,
        ocean_drift: OceanDrift | None = None,
        memory: MemoryTierStub | None = None,
        agent_id: UUID | None = None,
        role: str | None = None,
    ) -> None:
        if not hasattr(backend, "complete"):
            raise TypeError(
                "backend must expose `async def complete(prompt: str) -> str`; "
                f"got {type(backend).__name__}"
            )
        self.name = name
        self.role = role or "participant"
        self.agent_id = agent_id or uuid4()
        self.ocean_drift = ocean_drift or OceanDrift()
        self.memory = memory or MemoryTierStub()
        self._backend = backend

    # --- the entrypoint ForesightWorld.tick() calls -------------------

    async def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Run one think-act cycle.

        Returns an action dict shaped:
          ``{"action": str, "rationale": str, "put": dict | None}``

        v0.1's action vocabulary is open — the world's ``_apply_action``
        only acts on ``put``. v1.0 enforces a per-scenario
        ``action_space`` restriction per RFC §7.5 + §4.1.
        """
        prompt = self._compose_prompt(observation)
        try:
            raw = await self._backend.complete(prompt)
        except Exception as exc:  # noqa: BLE001 — surface as action error, never raise out of decide
            return {
                "action": "noop",
                "rationale": f"backend error: {type(exc).__name__}: {exc}",
                "put": None,
            }
        action = self._parse_response(raw)
        self.memory.remember({"tick": observation.get("tick"), "action": action})
        return action

    def _compose_prompt(self, observation: dict[str, Any]) -> str:
        """v0.1 prompt: identity + drift + recent scratchpad + observation.

        v1.0 will hand control of prompt assembly back to the
        live PawAgent so the persona produces what the real runtime
        would produce (RFC §7.2 "fidelity floor" — the captain-locked
        requirement).
        """
        drift = self.ocean_drift.as_prompt_block()
        recent = self.memory.recall(limit=3)
        recent_lines = (
            "\n".join(f"  - t{e.get('tick')}: {e.get('action', {})}" for e in recent) or "  (none)"
        )
        format_hint = (
            "Respond with one short line of the form: "
            "action=<verb>; rationale=<one phrase>; put=<key>:<value>"
        )
        return (
            f"You are {self.name}, role={self.role}. Personality: {drift}.\n"
            f"Recent activity:\n{recent_lines}\n"
            f"Current observation: tick={observation.get('tick')}, "
            f"active_count={observation.get('active_count')}, "
            f"state={observation.get('state')}\n"
            f"{format_hint}\n"
            "If no state change is appropriate, set put=none.\n"
        )

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        """Tolerant parser: pulls action / rationale / put out of a
        ``key=value; key=value; ...`` line. Missing fields default
        sanely so a chatty LLM response degrades to ``noop`` instead
        of raising. v1.0 will run responses through CAMEL's
        chat-completion-style schema (RFC §6.4 ``_to_camel_response``).
        """
        parts = [p.strip() for p in raw.replace("\n", ";").split(";") if p.strip()]
        kv: dict[str, str] = {}
        for p in parts:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            kv[k.strip().lower()] = v.strip()
        action = kv.get("action") or "noop"
        rationale = kv.get("rationale") or ""
        put_raw = kv.get("put", "none")
        put: dict[str, Any] | None
        if not put_raw or put_raw.lower() == "none":
            put = None
        elif ":" in put_raw:
            key, val = put_raw.split(":", 1)
            put = {key.strip(): val.strip()}
        else:
            put = {put_raw: True}
        return {"action": action, "rationale": rationale, "put": put}
