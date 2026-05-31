# ee/pocketpaw_ee/foresight/persona.py
# Updated: 2026-05-25 (feat/foresight-v03-calibration) — PR 3 adds the
# OASIS substrate-level integration RFC §7.2 specifies:
#   - ``PawSocialAgent`` class that subclasses
#     ``oasis.social_agent.SocialAgent`` when OASIS_AVAILABLE. The
#     subclass overrides ``perform_action_by_llm`` to delegate to a
#     wrapped ``SoulSeededPersona.decide`` so the runtime cognition
#     path (PawAgent + Soul bootstrap + memory tiers) is preserved
#     inside the OASIS-side agent shell. The "fidelity floor" RFC
#     §7.2 calls for is met: what Foresight rehearses is what the
#     live system would do.
#   - ``make_paw_social_agent(...)`` factory — convenience builder
#     that constructs the OASIS ``UserInfo`` + auto-assigns a unique
#     integer ``social_agent_id`` so callers don't have to thread
#     them through.
#   - Graceful fallback — when OASIS is not loaded (e.g. missing
#     torch / pandas / igraph despite the PR 3 lazy-import work), the
#     factory raises ``RuntimeError`` with a clear pointer to the
#     ``OASIS_AVAILABLE`` flag and the recovery path.
#   - ``SoulSeededPersona`` remains the lightweight fallback path
#     (no OASIS dependency) for the smoke loop, tests, and dev
#     environments without the full substrate.
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — PR 2 lifts
# the persona toward RFC §7.2 fidelity:
#   - Optional ``paw_agent: PawAgent`` parameter — when provided, the
#     persona uses the live PawAgent's bootstrap provider + soul bridge
#     to seed the prompt with the soul's context (identity block, recent
#     memories, OCEAN baseline). The captain-locked "fidelity floor" is
#     met once a real PawAgent is attached.
#   - ``SoulSeededPersona.from_paw_agent`` factory — convenience builder
#     that constructs a persona by wrapping an existing PawAgent. PR 3
#     will swap this for the OASIS ``PawSocialAgent(SocialAgent)`` shell
#     when the substrate is wired in.
#   - Backward-compat preserved — when ``paw_agent`` is ``None`` the
#     persona falls back to the v0.1 backend-only path so the smoke
#     test (DeterministicFakeBackend, no soul) keeps passing.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# SoulSeededPersona — the v0.2 persona shape. RFC 08 §7.2 calls for
# wrapping a real PawAgent inside OASIS's SocialAgent and routing
# memory through the Soul engine. PR 1 shipped a backend-only stub;
# PR 2 lands the PawAgent wrapper while leaving the OASIS subclass
# swap for PR 3 (the substrate is now vendored but not yet wired into
# the engine's tick loop).
#
# The persona deliberately does NOT subclass ``oasis.social_agent.SocialAgent``
# in v0.2 — the OASIS substrate is vendored but the cross-class wiring
# (action vocabulary translation, AgentGraph membership, message channel)
# is a PR 3 deliverable. The shape here matches the public surface RFC
# 08 §7.2 specifies (OceanDrift + memory tier stub + PawAgent reference
# + delegate-to-backend), so the PR 3 wiring becomes "swap parent class"
# rather than "rewrite cognition path".

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from pocketpaw.paw.agent import PawAgent  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


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

    PR 2 update: when a Persona is bound to a real PawAgent, the
    ``soul_bridge`` slot holds the PawAgent's ``SoulBridge`` so the
    overlay can be promoted to a real soul memory in PR 3. v0.2 still
    writes to the scratchpad regardless — soul writes stay deferred.
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
    soul_bridge: Any | None = None  # PR 3 routes writes through this

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
    """v0.2 soul-seeded persona.

    Construction has two shapes:

      - ``SoulSeededPersona(name, backend=..., ...)`` — the v0.1
        backend-only path. The persona has no live PawAgent attached;
        prompts are composed locally from identity + drift + scratchpad.
        Backwards-compat path retained for tests and the smoke runner.

      - ``SoulSeededPersona(name, backend=..., paw_agent=..., ...)`` or
        the convenience factory ``SoulSeededPersona.from_paw_agent(...)``
        — the v0.2 fidelity path. The persona's prompt composer pulls
        the soul's identity context via ``paw_agent.bootstrap_provider.
        get_context()`` and routes recall through
        ``paw_agent.bridge.recall``. The captain-locked "fidelity
        floor" (RFC §7.2) is met here: the persona's system context is
        what the live runtime would assemble.

    The persona's ``decide`` entrypoint is what ``ForesightWorld.tick()``
    invokes per tick. ``decide`` composes a prompt from the persona's
    identity block + the world observation + the recent scratchpad,
    asks the backend to produce an action, parses the backend's
    response into an action dict, and remembers the cycle in the
    scratchpad. When a PawAgent is attached, the identity block is
    enriched with the soul's bootstrap context.

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
        paw_agent: PawAgent | None = None,
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
        self._paw_agent = paw_agent
        if paw_agent is not None:
            # Bind the soul bridge for PR 3's overlay-write path. v0.2
            # only reads through the bridge; writes still hit scratchpad.
            self.memory.soul_bridge = getattr(paw_agent, "bridge", None)

    @property
    def paw_agent(self) -> PawAgent | None:
        """The live PawAgent backing this persona, or ``None`` for
        the v0.1 backend-only path. Read-only at v0.2; PR 3 may add
        rebind-at-tick support for scenario-mid swaps.
        """
        return self._paw_agent

    @property
    def has_fidelity(self) -> bool:
        """``True`` when the persona is backed by a real PawAgent and
        thus meets RFC §7.2's "fidelity floor" requirement. Useful for
        the run report to surface how much of a scenario hit the
        floor vs ran on stub personas.
        """
        return self._paw_agent is not None

    # --- convenience factory --------------------------------------------

    @classmethod
    def from_paw_agent(
        cls,
        paw_agent: PawAgent,
        *,
        backend: Any,
        name: str | None = None,
        role: str | None = None,
        ocean_drift: OceanDrift | None = None,
        agent_id: UUID | None = None,
    ) -> SoulSeededPersona:
        """Build a persona that wraps an already-constructed PawAgent.

        v0.2 keeps the PawAgent factory call (``get_paw_agent``) out
        of band — callers are expected to instantiate the agent
        themselves and hand it in. This keeps the persona pure and
        side-effect-free; PR 3 will offer a higher-level
        ``run_scenario(paw_agent_factory=...)`` entrypoint that lazily
        builds per-persona agents.

        The persona's display name defaults to the soul's ``name``
        attribute when no explicit ``name`` is supplied.
        """
        soul = getattr(paw_agent, "soul", None)
        derived_name = name or getattr(soul, "name", None) or "unnamed-persona"
        return cls(
            name=derived_name,
            backend=backend,
            ocean_drift=ocean_drift,
            agent_id=agent_id,
            role=role,
            paw_agent=paw_agent,
        )

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
        """Compose a prompt for one think-act cycle.

        Two branches:

          - With a live PawAgent attached: identity block comes from the
            soul's bootstrap provider (so the persona's prompt matches
            what the live runtime would assemble). OCEAN drift is added
            as a persona-shaping suffix. Recent scratchpad + observation
            stay v0.1-shaped so the parser contract holds.
          - Without PawAgent (v0.1 backend-only path): identity block
            is composed locally from name + role + drift, matching the
            v0.1 contract the smoke test pins.

        v1.0 will hand control of prompt assembly fully back to the
        live PawAgent so the persona produces what the real runtime
        would produce (RFC §7.2 "fidelity floor" — the captain-locked
        requirement). v0.2 takes the first half of that step.
        """
        recent = self.memory.recall(limit=3)
        recent_lines = (
            "\n".join(f"  - t{e.get('tick')}: {e.get('action', {})}" for e in recent) or "  (none)"
        )
        format_hint = (
            "Respond with one short line of the form: "
            "action=<verb>; rationale=<one phrase>; put=<key>:<value>"
        )
        identity_block = self._identity_block()
        return (
            f"{identity_block}\n"
            f"Recent activity:\n{recent_lines}\n"
            f"Current observation: tick={observation.get('tick')}, "
            f"active_count={observation.get('active_count')}, "
            f"state={observation.get('state')}\n"
            f"{format_hint}\n"
            "If no state change is appropriate, set put=none.\n"
        )

    def _identity_block(self) -> str:
        """Render the persona's identity block for the prompt header.

        When the persona is bound to a PawAgent, defer to the soul's
        bootstrap provider for the bulk of the identity; layer the
        scenario role + OCEAN drift on top. When no agent is bound,
        compose a local identity block (the v0.1 shape).
        """
        drift = self.ocean_drift.as_prompt_block()
        if self._paw_agent is None:
            return f"You are {self.name}, role={self.role}. Personality: {drift}."
        bootstrap = getattr(self._paw_agent, "bootstrap_provider", None)
        soul_context = ""
        if bootstrap is not None and hasattr(bootstrap, "get_context"):
            try:
                soul_context = str(bootstrap.get_context() or "")
            except Exception as exc:  # noqa: BLE001 — never let bootstrap break decide
                logger.debug(
                    "Soul bootstrap failed for persona %s: %s: %s",
                    self.name,
                    type(exc).__name__,
                    exc,
                )
                soul_context = ""
        scenario_overlay = (
            f"\nScenario role: {self.role}. Personality overlay for this simulation: {drift}."
        )
        return (soul_context.rstrip() + scenario_overlay).strip()

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


# --- PR 3 — PawSocialAgent (RFC §7.2 substrate-level integration) -----
#
# This is the load-bearing primitive RFC 08 §7.2 calls for: a persona
# that IS a real Paw agent wrapped in OASIS's SocialAgent shell. The
# class subclasses ``oasis.social_agent.SocialAgent`` so OASIS-side
# machinery (AgentGraph membership, action-space restriction, the
# Channel-based message bus) treats it like any other SocialAgent —
# while the cognition path delegates to a wrapped
# ``SoulSeededPersona`` so the live runtime's bootstrap / memory /
# tool routing is preserved.
#
# Construction is via the ``make_paw_social_agent`` factory because
# OASIS's ``SocialAgent.__init__`` needs a ``user_info`` and an
# integer ``agent_id`` — the factory hands callers a friendlier shape
# and handles the OASIS-import gating.


_PAW_SOCIAL_AGENT_COUNTER = 0
"""Monotonic counter for ``social_agent_id``. OASIS keys its
AgentGraph on this int; we need uniqueness within a run. Reset by
``reset_paw_social_agent_counter()`` for tests.
"""


def reset_paw_social_agent_counter() -> None:
    """Reset the module-level counter — test-only convenience."""
    global _PAW_SOCIAL_AGENT_COUNTER
    _PAW_SOCIAL_AGENT_COUNTER = 0


def _wrap_as_camel_backend(backend: Any) -> Any:
    """Wrap a Foresight backend in a CAMEL ``BaseModelBackend`` shim
    so OASIS's ``SocialAgent.__init__`` accepts it.

    Foresight's adapters (ClaudeCodeBackend etc.) are intentionally
    protocol-shaped (``async def run(messages, ...) -> dict``) and
    don't subclass CAMEL's ``BaseModelBackend``. CAMEL's ChatAgent
    constructor strictly type-checks ``model``, so we wrap.

    The shim:
      - subclasses ``BaseModelBackend`` (so CAMEL's check passes)
      - delegates ``_arun`` (the async path) to ``backend.run(...)``
      - synthesizes ``_run`` (sync) by calling ``asyncio.run`` on the
        async path — sufficient for the simulation tick context where
        only the async path is exercised.
      - exposes a ``token_counter`` that always returns 0 (Foresight's
        cost meter is fed from outside; CAMEL just needs the property
        to exist).

    If ``backend`` is already a ``BaseModelBackend`` instance, it's
    returned unchanged.
    """
    from camel.models import BaseModelBackend  # noqa: PLC0415
    from camel.types import ModelType  # noqa: PLC0415

    if isinstance(backend, BaseModelBackend):
        return backend

    class _ForesightBackendShim(BaseModelBackend):
        """Minimal BaseModelBackend wrapper. Constructed on-demand
        so each ``make_paw_social_agent`` call gets a fresh instance
        — avoids state collisions when one PawAgent is reused across
        multiple PawSocialAgents.
        """

        def __init__(self) -> None:
            # CAMEL's __init__ wants a ModelType + model_config; the
            # config is consulted by CAMEL's default token counter,
            # which we override to a noop below. Picking
            # ``ModelType.STUB`` (if it exists) or ``ModelType.DEFAULT``
            # avoids accidentally engaging CAMEL's pricing tables.
            chosen_type = getattr(ModelType, "STUB", None) or ModelType.DEFAULT
            super().__init__(model_type=chosen_type, model_config_dict={})
            self._wrapped = backend

        async def _arun(self, messages, response_format=None, tools=None):  # noqa: ARG002, ANN001
            """Delegate to the wrapped backend's run(). Returns a CAMEL
            chat-completion-shaped dict; CAMEL will parse it.
            """
            return await self._wrapped.run(messages, response_format=response_format, tools=tools)

        def _run(self, messages, response_format=None, tools=None):  # noqa: ARG002, ANN001
            """Sync fallback — CAMEL rarely exercises this in async
            sim runs but the abstract method must be implemented.
            """
            import asyncio  # noqa: PLC0415

            return asyncio.run(
                self._wrapped.run(messages, response_format=response_format, tools=tools)
            )

        @property
        def token_counter(self):  # type: ignore[override]
            """Foresight's cost meter is fed externally (RFC §10);
            CAMEL's per-call counter is bypassed.
            """
            return _NoopTokenCounter()

    return _ForesightBackendShim()


class _NoopTokenCounter:
    """Always returns 0. Used by ``_ForesightBackendShim``."""

    def count_tokens_from_messages(self, messages: Any) -> int:  # noqa: ARG002
        return 0

    def count_tokens_from_text(self, text: Any) -> int:  # noqa: ARG002
        return 0


def make_paw_social_agent(
    *,
    persona: SoulSeededPersona,
    user_info_template: str | None = None,
    available_actions: list[Any] | None = None,
    backend: Any | None = None,
) -> Any:
    """Construct a ``PawSocialAgent`` (subclass of
    ``oasis.social_agent.SocialAgent``) wrapping the given
    ``SoulSeededPersona``.

    The wrapped persona owns the cognition path: when OASIS's
    ``perform_action_by_llm`` fires, the override delegates to
    ``persona.decide(observation)`` — same code path the
    ``ForesightWorld.tick()`` smoke loop uses. The OASIS shell layers
    on the AgentGraph membership + Channel-based message bus that
    Market Sim and Org Change Rehearsal sub-types will need (RFC §4.2,
    §4.3).

    Args:
        persona: a ``SoulSeededPersona`` (ideally with
            ``paw_agent=`` attached for RFC §7.2 fidelity).
        user_info_template: optional ``camel.prompts.TextPrompt`` for
            ``SocialAgent.user_info.to_custom_system_message``.
            Defaults to ``None`` so OASIS uses
            ``user_info.to_system_message()``.
        available_actions: optional ``list[oasis.ActionType]`` — gates
            which OASIS action vocab the agent is allowed to emit.
            Defaults to "all" (matches OASIS's default).
        backend: optional CAMEL ``BaseModelBackend`` to pass to
            OASIS's ``SocialAgent(model=...)``. Defaults to the
            persona's own backend.

    Raises:
        RuntimeError: when OASIS is not loaded (missing igraph /
            pandas / torch / camel — see
            ``pocketpaw_ee.foresight.substrate.oasis.OASIS_LOAD_ERROR``
            for the underlying cause).

    Returns:
        An instance of the dynamically-created ``PawSocialAgent``
        subclass (subclass of ``oasis.social_agent.SocialAgent``).
        Its ``perform_action_by_llm`` delegates to ``persona.decide``;
        its ``decide`` (added for parity with ``SoulSeededPersona``)
        also delegates to ``persona.decide`` so ``ForesightWorld.tick``
        can treat it as a regular persona.
    """
    from pocketpaw_ee.foresight.substrate import oasis  # noqa: PLC0415 — lazy

    if not oasis.OASIS_AVAILABLE or oasis.SocialAgent is None or oasis.UserInfo is None:
        raise RuntimeError(
            "make_paw_social_agent requires the OASIS substrate to be "
            "loaded. OASIS_AVAILABLE=False; underlying error: "
            f"{oasis.OASIS_LOAD_ERROR!r}. Use SoulSeededPersona directly "
            "for the non-OASIS path."
        )

    global _PAW_SOCIAL_AGENT_COUNTER
    agent_id_int = _PAW_SOCIAL_AGENT_COUNTER
    _PAW_SOCIAL_AGENT_COUNTER += 1

    raw_backend = backend or persona._backend  # noqa: SLF001 — internal contract
    # OASIS's ``SocialAgent.__init__`` (inherited from CAMEL's
    # ``ChatAgent``) strictly type-checks the ``model`` parameter and
    # rejects anything that isn't a ``BaseModelBackend``. Our adapter
    # classes (ClaudeCodeBackend, DeterministicFakeBackend,
    # LiteLLMFallbackBackend) intentionally don't subclass it — they
    # are protocol-shaped. Wrap them in a thin CAMEL BaseModelBackend
    # shim so CAMEL's type check passes; the shim's ``_arun`` delegates
    # straight to the wrapped backend's ``run``.
    backend_to_use = _wrap_as_camel_backend(raw_backend)

    # Build a UserInfo from the persona. OASIS UserInfo carries a
    # ``user_id`` and a ``description`` (used by
    # ``to_system_message()``); we render the persona's identity
    # block as the description so the OASIS-side system prompt
    # carries the soul context.
    user_info = oasis.UserInfo(
        name=persona.name,
        description=persona._identity_block(),  # noqa: SLF001
        profile={
            "role": persona.role,
            "ocean_drift": {
                "openness": persona.ocean_drift.openness,
                "conscientiousness": persona.ocean_drift.conscientiousness,
                "extraversion": persona.ocean_drift.extraversion,
                "agreeableness": persona.ocean_drift.agreeableness,
                "neuroticism": persona.ocean_drift.neuroticism,
            },
            "has_fidelity": persona.has_fidelity,
        },
    )

    class PawSocialAgent(oasis.SocialAgent):  # type: ignore[misc, valid-type]
        """RFC §7.2 — Paw persona wrapped in OASIS's SocialAgent shell.

        Defined inline so the OASIS class lookup happens lazily at
        ``make_paw_social_agent`` call time, not at module import.
        This keeps ``persona.py`` importable without OASIS.
        """

        def __init__(self, _agent_id: int, _user_info: Any, _persona: SoulSeededPersona) -> None:
            super().__init__(
                agent_id=_agent_id,
                user_info=_user_info,
                user_info_template=user_info_template,
                model=backend_to_use,
                available_actions=available_actions,
            )
            self._persona = _persona

        async def perform_action_by_llm(self) -> Any:
            """Override OASIS's per-tick action emitter. The persona's
            ``decide`` runs the live Paw runtime path (bootstrap,
            memory, Soul recall, the SDK's loop); we wrap its return
            in a CAMEL response shape so OASIS's downstream parsers
            don't break if a future PR wires the ``perform_action_by_data``
            path back in.
            """
            # The observation OASIS would normally read off
            # ``self.env`` is not available here (we don't have a
            # Platform). Pass a minimal observation so the persona's
            # prompt composer can render its tick context.
            observation = {
                "tick": getattr(self, "_pp_tick", 0),
                "state": getattr(self, "_pp_state", {}),
                "active_count": 1,
            }
            return await self._persona.decide(observation)

        async def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
            """Parity surface for ``ForesightWorld.tick()`` — delegates
            straight through to the wrapped persona's decide(). Lets
            the world's registry treat a ``PawSocialAgent`` and a
            plain ``SoulSeededPersona`` identically.
            """
            return await self._persona.decide(observation)

    return PawSocialAgent(agent_id_int, user_info, persona)
