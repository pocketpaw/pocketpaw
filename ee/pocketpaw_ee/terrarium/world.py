# ee/pocketpaw_ee/terrarium/world.py
#
# The PURE world engine. State + physics + a citizen's decision go in; Events,
# Artifacts and a citizen patch come out. NO Beanie, NO soul, NO bus, NO env —
# ``service.py`` owns all of that. Keeping the rules pure is what makes the
# invariants (cost accounting, tech gating, hibernation, the write-policy)
# testable without a database.
#
# Two things here are load-bearing beyond "apply a verb":
#
#   * ``label_viewer_claim`` — THE WRITE-POLICY. Viewer text reaches a citizen
#     ONLY through this function, wrapped as an unverified claim from a named
#     outside voice. Ground truth (pool, ledger, artifacts, laws) rides the
#     digest separately so the citizen can check the claim. Nothing produced
#     here is ever written into a soul as fact — ``episodic_summary`` builds the
#     soul memory from citizen-origin events ONLY.
#   * ``cluster_moments`` — a MOMENT is two or more citizens acting in the same
#     place on the same day. Pure, deterministic, and the only thing that turns
#     a stream of acts into something a stranger can read as a story.
#   * ``apply_acts`` re-validates every act server-side against the citizen's
#     balance, the physics verb list, and the tech tree. The model is never
#     trusted: an act it cannot afford or has not unlocked is DROPPED, not run.
#   * ``design`` is the first GRANTED verb: only a citizen holding ``workshop``
#     may draw a building, the drawing is validated against ``design.py`` (the
#     frontend's schema), and an invalid one is dropped unpaid. A later
#     ``build`` may name a design it owns (``Act.design_id``) so the frontend
#     draws the citizen's own building; the service checks the ownership.

"""The pure terrarium world engine: verbs, tech gating, and the write-policy."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from pocketpaw_ee.terrarium.design import DesignError, validate_design
from pocketpaw_ee.terrarium.physics import PhysicsFile

# The tech node that grants ``design``.
DESIGN_TECH = "workshop"

# Verb -> Journal event kind. ``speak`` reads as ``say`` on the wire (the
# contract's kind list), so the mapping is explicit rather than implied.
VERB_TO_KIND: dict[str, str] = {
    "speak": "say",
    "write": "write",
    "trade": "trade",
    "craft": "craft",
    "build": "build",
    "explore": "explore",
    "spawn": "gate",
    "vote": "vote",
    "design": "design",
}

# Verbs that leave an Artifact behind, and the artifact kind each produces.
VERB_ARTIFACT_KIND: dict[str, str] = {
    "write": "book",
    "craft": "tool",
    "build": "structure",
    "design": "design",
}


# ---------------------------------------------------------------------------
# The strict decision schema the citizen LLM must return
# ---------------------------------------------------------------------------


class Act(BaseModel):
    """One verb the citizen wants to perform this tick."""

    verb: str
    text: str = ""
    name: str = ""
    node: str | None = None
    to: str | None = None
    amount: int = 0
    # ``design``: the design JSON for a ``design`` act (a JSON string in ``text``
    # is accepted too). ``Any`` on purpose: a malformed design is the
    # validator's verdict to give, not a reason to lose the whole decision.
    # ``design_id``: for ``build``, a design this citizen owns.
    design: Any = None
    design_id: str | None = None


class Decision(BaseModel):
    """A citizen's whole-tick output — strict JSON, nothing else."""

    thought: str = ""
    acts: list[Act] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Snapshots — plain data the service maps its Beanie docs onto
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CitizenSnapshot:
    id: str
    name: str
    role: str = ""
    balance: int = 0
    state: str = "alive"
    unlocked: tuple[str, ...] = ()
    x: float = 50.0
    y: float = 50.0
    charter: str | None = None
    generation: int = 1
    ocean: dict[str, float] = field(default_factory=dict)
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ViewerMessage:
    """A line a human paid a token to say. ALWAYS unverified."""

    voice: str
    text: str


@dataclass(frozen=True)
class SenseDigest:
    """Everything a citizen perceives this tick.

    ``ground_truth`` is checkable world state; ``viewer_claims`` are the
    already-labelled outside voices. The split is the write-policy: only
    ground truth may be reasoned about as fact.
    """

    day: int
    tick: int
    ground_truth: dict[str, Any]
    nearby_speech: tuple[str, ...] = ()
    new_artifacts: tuple[str, ...] = ()
    weather: tuple[str, ...] = ()
    viewer_claims: tuple[str, ...] = ()
    memories: tuple[str, ...] = ()


@dataclass
class NewEvent:
    """A Journal row the engine wants written. ``cost`` is signed: negative =
    the actor spent credits, positive = the actor earned them.

    ``node`` is the place the act named (a tech-tree node), carried so the
    moment clusterer can group by place without re-reading the decision.
    """

    kind: str
    actor: str
    body: str
    cost: int = 0
    artifact_index: int | None = None
    origin: str = "citizen"
    viewer_origin: bool = False
    node: str | None = None


@dataclass
class NewArtifact:
    kind: str
    name: str
    author: str
    cost: int = 0
    body: str = ""
    mime: str | None = None
    x: float | None = None
    y: float | None = None
    unlocks: list[str] = field(default_factory=list)
    design_id: str | None = None


@dataclass
class TickOutcome:
    """Everything one citizen's tick changed. The service persists it."""

    events: list[NewEvent] = field(default_factory=list)
    artifacts: list[NewArtifact] = field(default_factory=list)
    balance_delta: int = 0
    pool_delta: int = 0
    unlocked: list[str] = field(default_factory=list)
    charter: str | None = None
    x: float | None = None
    y: float | None = None
    transfers: list[tuple[str, int]] = field(default_factory=list)
    spawn_requests: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# THE WRITE-POLICY
# ---------------------------------------------------------------------------

VIEWER_CLAIM_PREFIX = "UNVERIFIED CLAIM"


def label_viewer_claim(voice: str, text: str) -> str:
    """Wrap human-authored text as an unverified claim from a named outside voice.

    WHY THIS EXISTS, and why it is one function: viewer speech and omens reach
    citizens by design — that is the experiment. What must never happen is a
    viewer's assertion entering a soul as FACT, because that is exactly the
    hallucination cascade Project Sid documented: a false claim propagates
    through the social graph as if it were observed. So every human-authored
    line crosses into a citizen's context through here, and only here, marked as
    a claim by a named voice. The citizen can then fact-check it against the
    ground truth in the same digest (pool level, ledger, artifacts, laws).

    The complement of this rule lives in ``episodic_summary``: the soul memory
    written at the end of a tick is built from citizen-origin events ONLY, so a
    labelled claim can be reasoned about but can never be remembered as fact.
    """
    voice_name = (voice or "an unnamed voice").strip()
    body = " ".join(str(text or "").split())
    return f'[{VIEWER_CLAIM_PREFIX} from outside voice "{voice_name}", not verified: {body}]'


def episodic_summary(citizen_name: str, day: int, outcome: TickOutcome) -> str:
    """Build the soul memory for a finished tick.

    Write-policy: ONLY citizen-origin events contribute. Viewer-origin text and
    system lines are excluded by construction, so nothing a human asserted can
    land in a soul as fact.
    """
    own = [e for e in outcome.events if e.origin == "citizen" and not e.viewer_origin]
    if not own:
        return ""
    lines = "; ".join(f"{e.kind}: {e.body}"[:160] for e in own)
    return f"Day {day}: {citizen_name} {lines}"


def build_digest(
    *,
    day: int,
    tick: int,
    pool: int,
    citizen: CitizenSnapshot,
    ledger: list[dict[str, Any]],
    nearby_speech: list[str],
    new_artifacts: list[str],
    weather: list[str],
    viewer_messages: list[ViewerMessage],
    memories: list[str],
    constitution: list[str],
) -> SenseDigest:
    """Assemble what one citizen perceives. Viewer text is labelled on the way in."""
    return SenseDigest(
        day=day,
        tick=tick,
        ground_truth={
            "pool": pool,
            "your_balance": citizen.balance,
            "your_unlocked": list(citizen.unlocked),
            "ledger": ledger,
            "constitution": list(constitution),
        },
        nearby_speech=tuple(nearby_speech),
        new_artifacts=tuple(new_artifacts),
        weather=tuple(weather),
        viewer_claims=tuple(label_viewer_claim(m.voice, m.text) for m in viewer_messages),
        memories=tuple(memories),
    )


# ---------------------------------------------------------------------------
# Verb application
# ---------------------------------------------------------------------------


def think_cost(physics: PhysicsFile, *, storm: bool = False) -> int:
    """Per-tick think cost. A storm doubles it — the only world knob on cost."""
    return physics.costs.think * (2 if storm else 1)


def unlockable(physics: PhysicsFile, citizen: CitizenSnapshot) -> list[str]:
    """Tech nodes whose ``needs`` are already held and that are not yet unlocked."""
    held = set(citizen.unlocked)
    return [
        name
        for name, node in physics.tech_tree.items()
        if name not in held and set(node.needs) <= held
    ]


def _verb_cost(physics: PhysicsFile, act: Act) -> int:
    """What this act costs. A tech-node build costs the NODE's price."""
    if act.verb == "build" and act.node:
        node = physics.tech_tree.get(act.node)
        if node is not None:
            return node.cost
    if act.verb == "trade":
        return max(0, int(act.amount))
    if act.verb == "vote":
        # ``vote`` has no entry in the contract's costs map but invariant 2
        # forbids a zero-cost vote, so it rides the speak price.
        return physics.costs.speak
    return int(getattr(physics.costs, act.verb, 0) or 0)


def apply_acts(
    physics: PhysicsFile,
    citizen: CitizenSnapshot,
    decision: Decision,
    *,
    storm: bool = False,
) -> TickOutcome:
    """Apply a citizen's chosen acts, re-validating each one server-side.

    Order: the think charge always lands first (a tick costs whether or not it
    produced an act), then each act in turn while the running balance affords
    it. An act the citizen cannot afford, whose verb this universe forbids, or
    whose tech prerequisites it does not hold, is DROPPED and recorded in
    ``outcome.dropped`` — never silently executed.
    """
    outcome = TickOutcome()
    allowed = set(physics.verbs)
    held = set(citizen.unlocked)

    tcost = think_cost(physics, storm=storm)
    outcome.events.append(
        NewEvent(
            kind="think",
            actor=citizen.name,
            body=(decision.thought or "considered the day").strip()[:400],
            cost=-tcost,
        )
    )
    balance = citizen.balance - tcost
    outcome.balance_delta -= tcost
    outcome.pool_delta += tcost  # spent credits return to the world pool

    for act in decision.acts:
        verb = str(act.verb or "").strip().lower()
        if verb not in VERB_TO_KIND:
            outcome.dropped.append(f"{verb or '?'}: unknown verb")
            continue
        if verb not in allowed:
            outcome.dropped.append(f"{verb}: this universe's physics does not allow it")
            continue

        cost = _verb_cost(physics, act)
        if cost > balance:
            outcome.dropped.append(f"{verb}: costs {cost}, balance is {balance}")
            continue

        if verb == "build" and act.node:
            node = physics.tech_tree.get(act.node)
            if node is None:
                outcome.dropped.append(f"build {act.node}: no such tech node")
                continue
            if act.node in held:
                outcome.dropped.append(f"build {act.node}: already unlocked")
                continue
            missing = [n for n in node.needs if n not in held]
            if missing:
                outcome.dropped.append(f"build {act.node}: needs {missing} first")
                continue

        if verb == "trade":
            if not act.to or cost <= 0:
                outcome.dropped.append("trade: needs a recipient and a positive amount")
                continue
            outcome.transfers.append((str(act.to), cost))

        design = None
        if verb == "design":
            # Granted, not merely allowed: the physics may list the verb, but
            # only a citizen holding the workshop may use it.
            if DESIGN_TECH not in held:
                outcome.dropped.append(f"design: needs {DESIGN_TECH} first")
                continue
            raw: Any = act.design
            if raw is None and act.text.strip():
                try:
                    raw = json.loads(act.text)
                except ValueError:
                    raw = act.text
            checked = validate_design(raw)
            if isinstance(checked, DesignError):
                # Never persisted, never charged: a design that does not
                # validate is not a design.
                outcome.dropped.append(f"design: {checked}")
                continue
            design = checked

        artifact_index: int | None = None
        if verb in VERB_ARTIFACT_KIND:
            unlocks = [act.node] if (verb == "build" and act.node) else []
            outcome.artifacts.append(
                NewArtifact(
                    kind=VERB_ARTIFACT_KIND[verb],
                    name=(
                        design.name
                        if design is not None
                        else (act.name or act.text[:40] or verb).strip()[:120]
                    ),
                    author=citizen.name,
                    cost=cost,
                    body=design.canonical() if design is not None else act.text,
                    mime=(
                        "text/markdown"
                        if verb == "write"
                        else "application/json"
                        if verb == "design"
                        else None
                    ),
                    x=citizen.x if verb == "build" else None,
                    y=citizen.y if verb == "build" else None,
                    unlocks=unlocks,
                    # The service verifies the design exists and is this
                    # citizen's own; a foreign or missing one is cleared there.
                    design_id=act.design_id if verb == "build" else None,
                )
            )
            artifact_index = len(outcome.artifacts) - 1
            if unlocks:
                held.update(unlocks)
                outcome.unlocked.extend(unlocks)

        if verb == "explore":
            # Deterministic drift keyed off the citizen id — no RNG in the
            # engine keeps a replay from the Journal reproducible.
            h = sum(ord(c) for c in citizen.id) or 1
            outcome.x = round((citizen.x + (h % 17) - 8) % 100, 2)
            outcome.y = round((citizen.y + (h % 13) - 6) % 100, 2)

        if verb == "write" and citizen.charter is None and outcome.charter is None:
            # A citizen's FIRST write is its charter (the zero ritual).
            outcome.charter = act.text.strip()[:2000]

        if verb == "spawn":
            # Spawning a child is gated: the act files an Instinct Action and
            # leaves a zero-cost ``gate`` event. No child exists until approval,
            # and no credits move until the executor runs.
            outcome.spawn_requests.append(
                {"parent_id": citizen.id, "parent": citizen.name, "child_name": act.name or "child"}
            )
            outcome.events.append(
                NewEvent(
                    kind="gate",
                    actor=citizen.name,
                    body=(
                        f"asked to bring {act.name or 'a child'} into the world — awaiting approval"
                    ),
                    cost=0,
                )
            )
            continue

        body = (act.text or act.name or verb).strip()[:600]
        if design is not None:
            body = f"{citizen.name} designed {design.name}"
        if verb == "build" and act.node:
            body = f"built {act.node}" + (f" — {body}" if body and body != verb else "")
        outcome.events.append(
            NewEvent(
                kind=VERB_TO_KIND[verb],
                actor=citizen.name,
                body=body,
                cost=-cost,
                artifact_index=artifact_index,
                node=act.node or None,
            )
        )
        balance -= cost
        outcome.balance_delta -= cost
        if verb != "trade":
            # Spent credits return to the world pool. Traded credits are the
            # exception: they move citizen → citizen and never touch it.
            outcome.pool_delta += cost

    return outcome


# ---------------------------------------------------------------------------
# MOMENTS — when two citizens act in the same place on the same day
# ---------------------------------------------------------------------------

# Which act kinds can make a moment, and which kind wins when a cluster mixes
# them. A raid beats a build beats a trade beats speech: the loudest thing that
# happened is what the day is remembered for. ``think`` is absent on purpose —
# every citizen thinks every tick, so counting thoughts would make every tick a
# moment and the feed would say nothing.
MOMENT_KIND_RANK: dict[str, int] = {
    "say": 1,
    "explore": 2,
    "vote": 3,
    "write": 4,
    "craft": 5,
    "trade": 6,
    "build": 7,
    "raid": 8,
}

# How close two citizens have to stand to be "in the same place" when neither
# act named a node. The map is 100x100, so this is roughly a village.
MOMENT_RADIUS = 8.0

_COUNT_WORD = {4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight", 9: "Nine"}

# One plain sentence per kind. No em dashes: these are read aloud in a feed.
_MOMENT_TEMPLATE: dict[str, str] = {
    "say": "{who} talked {where}",
    "explore": "{who} went out {where}",
    "vote": "{who} voted {where}",
    "write": "{who} wrote {where}",
    "craft": "{who} made things {where}",
    "trade": "{who} traded {where}",
}


@dataclass(frozen=True)
class PlacedAct:
    """One written act, with the place it happened. The clusterer's input."""

    seq: int
    actor: str
    kind: str
    x: float
    y: float
    day: int = 1
    tick: int = 0
    node: str | None = None


@dataclass(frozen=True)
class NewMoment:
    """Two or more citizens acting in one place on one day."""

    day: int
    tick_from: int
    tick_to: int
    place: str | None
    x: float
    y: float
    actors: tuple[str, ...]
    act_seqs: tuple[int, ...]
    kind: str
    headline: str


def _who(actors: tuple[str, ...]) -> str:
    """Name the actors, or count them once there are too many to read."""
    if len(actors) == 1:
        return actors[0]
    if len(actors) == 2:
        return f"{actors[0]} and {actors[1]}"
    if len(actors) == 3:
        return f"{actors[0]}, {actors[1]} and {actors[2]}"
    return f"{_COUNT_WORD.get(len(actors), str(len(actors)))} citizens"


def _quadrant(x: float, y: float) -> str:
    """Which corner of the map this was, in words a viewer can point at."""
    return f"{'north' if y < 50 else 'south'} {'west' if x < 50 else 'east'}"


def _headline(kind: str, actors: tuple[str, ...], place: str | None, x: float, y: float) -> str:
    who = _who(actors)
    where = f"at the {place}" if place else f"in the {_quadrant(x, y)}"
    if kind == "raid":
        return f"{who} fought over the {place}" if place else f"{who} fought {where}"
    if kind == "build":
        return f"{who} raised the {place}" if place else f"{who} built something together {where}"
    return _MOMENT_TEMPLATE.get(kind, "{who} gathered {where}").format(who=who, where=where)


def cluster_moments(day_acts: Sequence[PlacedAct], *, phase: str | None = None) -> list[NewMoment]:
    """Group a day's acts into moments. PURE and DETERMINISTIC.

    A moment is two or more DISTINCT citizens acting at the same place on the
    same day. "Place" is the act's node when it named one, otherwise the
    citizens' positions inside ``MOMENT_RADIUS``. Acts are sorted by seq before
    grouping, so the caller's ordering can never leak into the output: the same
    acts in produce byte-identical moments out.

    ``phase`` is the story director's pacing phase. There is no director yet, so
    it is accepted and ignored rather than left out of the signature.
    """
    acts = sorted((a for a in day_acts if a.kind in MOMENT_KIND_RANK), key=lambda a: (a.day, a.seq))
    groups: list[list[PlacedAct]] = []
    for act in acts:
        for group in groups:
            head = group[0]
            if head.day != act.day:
                continue
            if head.node is not None or act.node is not None:
                if head.node == act.node:
                    group.append(act)
                    break
                continue
            if (act.x - head.x) ** 2 + (act.y - head.y) ** 2 <= MOMENT_RADIUS**2:
                group.append(act)
                break
        else:
            groups.append([act])

    moments: list[NewMoment] = []
    for group in groups:
        actors = tuple(dict.fromkeys(a.actor for a in group))
        if len(actors) < 2:
            continue
        kind = max(group, key=lambda a: MOMENT_KIND_RANK[a.kind]).kind
        place = group[0].node
        x = round(sum(a.x for a in group) / len(group), 2)
        y = round(sum(a.y for a in group) / len(group), 2)
        moments.append(
            NewMoment(
                day=group[0].day,
                tick_from=min(a.tick for a in group),
                tick_to=max(a.tick for a in group),
                place=place,
                x=x,
                y=y,
                actors=actors,
                act_seqs=tuple(a.seq for a in group),
                kind=kind,
                headline=_headline(kind, actors, place, x, y),
            )
        )
    return moments


def hibernates(balance: int) -> bool:
    """Contract invariant 3 — a citizen at balance <= 0 hibernates. Soul kept."""
    return balance <= 0


Rung = Literal["camp", "town", "nation", "planet", "multiverse"]


# The ladder is a POPULATION ladder, and these are the exact thresholds the
# observatory prints beside the label (camp "1–10 souls", town "10²", nation
# "10⁴", planet "10⁶"). They live here so the HUD can never contradict the
# ladder drawn next to it — five souls labelled "town" above a line reading
# "5 souls here" makes the whole ladder look arbitrary, and the ladder is how a
# viewer understands that Earth is stuck on rung four.
_RUNG_POP = {"town": 100, "nation": 10_000, "planet": 1_000_000}
# Tech is a FLOOR, not a substitute: a hundred souls with no infrastructure are
# a crowd, not a town.
_RUNG_TECH = {"town": 2, "nation": 4, "planet": 6}


def rung_for(pop: int, unlocked: int) -> str:
    """The ladder rung a universe has reached. Cheap projection, not state.

    ``multiverse`` is never returned: it means a soul has crossed into another
    universe, which is migration rather than scale, and nothing in v0 does it.
    """
    for rung in ("planet", "nation", "town"):
        if pop >= _RUNG_POP[rung] and unlocked >= _RUNG_TECH[rung]:
            return rung
    return "camp"


__all__ = [
    "DESIGN_TECH",
    "MOMENT_KIND_RANK",
    "MOMENT_RADIUS",
    "VERB_ARTIFACT_KIND",
    "VERB_TO_KIND",
    "VIEWER_CLAIM_PREFIX",
    "Act",
    "CitizenSnapshot",
    "Decision",
    "NewArtifact",
    "NewEvent",
    "NewMoment",
    "PlacedAct",
    "SenseDigest",
    "TickOutcome",
    "ViewerMessage",
    "apply_acts",
    "build_digest",
    "cluster_moments",
    "episodic_summary",
    "hibernates",
    "label_viewer_claim",
    "rung_for",
    "think_cost",
    "unlockable",
]
