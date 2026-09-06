# ee/pocketpaw_ee/terrarium/physics.py
#
# The PHYSICS FILE — a universe's genome. YAML in, a validated ``PhysicsFile``
# out. Everything the world costs, allows, and can unlock lives here; nothing
# else in terrarium invents a number.
#
# Validation is LOUD by design: a bad physics file is a universe that would run
# wrong forever, so ``load_physics`` raises ``PhysicsError`` with a message that
# names the offending key. The hard rules:
#   * every cost (and every tech-node cost) is a positive integer
#   * ``verbs`` is a subset of ``KNOWN_VERBS``
#   * every tech-tree ``needs`` entry names an existing node, and the graph
#     is acyclic (a cycle can never unlock, so it is a broken world)
#   * ``founders >= 1`` — a universe with nobody in it has no first tick
#
# Wire shape matches the frozen v0 contract exactly (YAML in, JSON out).

"""The physics file: a universe's genome, and its validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

# The fixed verb set. A physics file may narrow it, never widen it.
KNOWN_VERBS: tuple[str, ...] = (
    "speak",
    "write",
    "trade",
    "craft",
    "build",
    "explore",
    "spawn",
    "vote",
)

Rung = Literal["camp", "town", "nation", "planet", "multiverse"]


class PhysicsError(ValueError):
    """A physics file that would produce a broken universe."""


class Endowment(BaseModel):
    daily: int = 120
    decay_weekly: float = 0.02


class Costs(BaseModel):
    """Per-verb credit costs. ``think`` is charged once per citizen per tick."""

    think: int = 2
    speak: int = 1
    write: int = 4
    craft: int = 8
    build: int = 20
    explore: int = 6
    spawn: int = 150


class ChatRules(BaseModel):
    open: bool = True
    token_per_message: int = 1
    superchat: bool = True


class TimeRules(BaseModel):
    world_day_seconds: int = 3600
    ticks_per_day: int = 12
    dormant_ticks_per_day: int = 1


class TechNode(BaseModel):
    cost: int
    needs: list[str] = Field(default_factory=list)
    grants: list[str] = Field(default_factory=list)


class ModelTiers(BaseModel):
    founders: str = "premium"
    descendants: str = "mid"
    crowd: str = "tail"


class PhysicsFile(BaseModel):
    """The universe genome. Field-for-field the contract's PhysicsFile."""

    universe: str
    seed: int = 0
    endowment: Endowment = Field(default_factory=Endowment)
    costs: Costs = Field(default_factory=Costs)
    verbs: list[str] = Field(default_factory=lambda: list(KNOWN_VERBS))
    raids: bool = False
    chat: ChatRules = Field(default_factory=ChatRules)
    time: TimeRules = Field(default_factory=TimeRules)
    constitution: list[str] = Field(default_factory=list)
    tech_tree: dict[str, TechNode] = Field(default_factory=dict)
    models: ModelTiers = Field(default_factory=ModelTiers)
    founders: int = 5
    world_brief: str = ""


def _check_costs(physics: PhysicsFile) -> None:
    for verb, value in physics.costs.model_dump().items():
        if value <= 0:
            raise PhysicsError(f"costs.{verb} must be a positive integer, got {value!r}")
    if physics.endowment.daily <= 0:
        raise PhysicsError(f"endowment.daily must be positive, got {physics.endowment.daily!r}")


def _check_verbs(physics: PhysicsFile) -> None:
    unknown = [v for v in physics.verbs if v not in KNOWN_VERBS]
    if unknown:
        raise PhysicsError(
            f"verbs contains unknown verb(s) {unknown!r}; known verbs are {list(KNOWN_VERBS)}"
        )
    if not physics.verbs:
        raise PhysicsError("verbs must not be empty — a citizen with no verbs cannot act")


def _check_tech_tree(physics: PhysicsFile) -> None:
    """Costs positive, ``needs`` resolvable, and the graph acyclic.

    Iterative DFS with an on-stack set so the error names the actual cycle
    instead of blowing the Python recursion limit on a deep tree.
    """
    tree = physics.tech_tree
    for name, node in tree.items():
        if node.cost <= 0:
            raise PhysicsError(f"tech_tree.{name}.cost must be positive, got {node.cost!r}")
        for need in node.needs:
            if need not in tree:
                raise PhysicsError(
                    f"tech_tree.{name}.needs references unknown node {need!r}; "
                    f"known nodes are {sorted(tree)}"
                )

    visited: set[str] = set()
    for root in tree:
        if root in visited:
            continue
        # (node, iterator over its needs); ``stack_names`` is the on-stack set.
        stack: list[tuple[str, list[str]]] = [(root, list(tree[root].needs))]
        stack_names = {root}
        while stack:
            name, pending = stack[-1]
            if not pending:
                stack.pop()
                stack_names.discard(name)
                visited.add(name)
                continue
            nxt = pending.pop()
            if nxt in stack_names:
                cycle = [n for n, _ in stack] + [nxt]
                raise PhysicsError(
                    f"tech_tree has a cycle that can never unlock: {' -> '.join(cycle)}"
                )
            if nxt not in visited:
                stack.append((nxt, list(tree[nxt].needs)))
                stack_names.add(nxt)


def validate_physics(physics: PhysicsFile) -> PhysicsFile:
    """Run every hard rule. Raises ``PhysicsError`` on the first violation."""
    if physics.founders < 1:
        raise PhysicsError(f"founders must be >= 1, got {physics.founders!r}")
    if not physics.universe.strip():
        raise PhysicsError("universe must be a non-empty name")
    if physics.time.ticks_per_day < 1:
        raise PhysicsError(f"time.ticks_per_day must be >= 1, got {physics.time.ticks_per_day!r}")
    _check_costs(physics)
    _check_verbs(physics)
    _check_tech_tree(physics)
    return physics


def parse_physics(raw: dict[str, Any]) -> PhysicsFile:
    """Validate a physics dict (the wire shape) into a ``PhysicsFile``."""
    try:
        physics = PhysicsFile.model_validate(raw)
    except ValidationError as exc:
        raise PhysicsError(f"physics file is malformed: {exc}") from exc
    return validate_physics(physics)


def load_physics(path: str | Path) -> PhysicsFile:
    """Load + validate a physics YAML file."""
    p = Path(path).expanduser()
    if not p.exists():
        raise PhysicsError(f"physics file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PhysicsError(f"physics file {p} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PhysicsError(f"physics file {p} must be a YAML mapping, got {type(raw).__name__}")
    return parse_physics(raw)


def seed_physics_path(name: str = "dust") -> Path:
    """Path to a bundled seed physics file (``seeds/<name>.yaml``)."""
    return Path(__file__).parent / "seeds" / f"{name}.yaml"


__all__ = [
    "KNOWN_VERBS",
    "ChatRules",
    "Costs",
    "Endowment",
    "ModelTiers",
    "PhysicsError",
    "PhysicsFile",
    "Rung",
    "TechNode",
    "TimeRules",
    "load_physics",
    "parse_physics",
    "seed_physics_path",
    "validate_physics",
]
