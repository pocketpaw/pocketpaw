# ee/pocketpaw_ee/terrarium/domain.py
#
# Terrarium persistence + frozen read-path value objects.
#
# Four workspace-keyed Beanie documents, shaped exactly like the mandates
# entity: UniverseDoc (the world + its physics genome + the monotonic ``seq``
# counter), CitizenDoc (a Soul with a balance and a position), EventDoc (the
# JOURNAL — the truth; citizens/ledger/artifacts are projections rebuildable
# from it) and ArtifactDoc (what a write/craft/build verb left behind).
#
# Per the 4-file entity rule ONLY ``terrarium/service.py`` imports these doc
# classes; router/dto/world/weather see the frozen views or plain dicts. The
# docs live here rather than in ``cloud/models/`` so the entity is
# self-contained; they are registered into ``init_beanie`` by the lazy
# ``_ensure_terrarium_docs()`` helper in ``cloud/models/__init__`` (the same
# calendar/mandates out-of-models pattern).
#
# Invariant carried here: ``UniverseDoc.seq`` is the monotonic per-universe
# event sequence the client pages with (``?since=``). It is assigned in-process
# under the service's per-universe asyncio lock.
#
# EventDoc carries a free-form ``data`` payload so a story-layer row (a moment)
# rides the same Journal as the acts it summarises rather than needing a second
# collection and a second read path.

"""Terrarium documents (universe, citizen, event, artifact) + read-path views."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument

UniverseStatus = Literal["running", "dormant", "paused", "archived"]
CitizenState = Literal["alive", "hibernating"]
Trend = Literal["up", "down", "flat"]
EventOrigin = Literal["citizen", "viewer", "system"]
ArtifactKind = Literal["structure", "tool", "book", "law", "map"]
ArtifactStage = Literal["done", "building"]

# Every kind the Journal projection can carry (the contract's ONE event shape).
EVENT_KINDS: tuple[str, ...] = (
    "think",
    "say",
    "write",
    "trade",
    "craft",
    "build",
    "explore",
    "vote",
    "spawn",
    "gate",
    "hibernate",
    "weather",
    "arrive",
    "raid",
    "gain",
    "moment",
    "batch",
)

# Contract invariant 2: every event costs or earns. ``cost: 0`` is legal only
# for these kinds. The service asserts this before it writes an EventDoc.
# ``batch`` is the clock speaking, not a citizen: it is written when a dormant
# world's half-price batch is abandoned and the world goes back to thinking live.
ZERO_COST_KINDS: frozenset[str] = frozenset(
    {"gate", "weather", "hibernate", "arrive", "moment", "batch"}
)


# ---------------------------------------------------------------------------
# Beanie documents — service.py is the SOLE importer.
# ---------------------------------------------------------------------------


class UniverseDoc(TimestampedDocument):
    """One universe. ``physics`` carries the validated PhysicsFile as a dict.

    ``seq`` is the monotonic event counter (contract invariant 1).
    ``weather_pledges`` accumulates ``{kind: {tokens, gods:[user_id]}}`` until a
    power's threshold fires. ``storm_ticks`` is how many more ticks the think
    cost stays doubled. ``public`` is the per-universe opt-in the anonymous read
    surface requires — it is NOT sufficient on its own (the env flag gates too).
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    seed: int = 0
    status: UniverseStatus = "running"
    day: int = 1
    tick: int = 0
    pool: int = 0
    rung: str = "camp"
    physics: dict[str, Any] = Field(default_factory=dict)
    public: bool = False
    creator: str = ""
    seq: int = 0
    weather_pledges: dict[str, dict[str, Any]] = Field(default_factory=dict)
    storm_ticks: int = 0
    # The clock (scheduler.py). ``last_tick_at`` is when the sweeper (or a manual
    # /tick) last advanced the world; ``last_viewed_at`` is when a viewer last
    # read the Journal — a world nobody watches for a world-day goes dormant.
    last_tick_at: datetime | None = None
    last_viewed_at: datetime | None = None
    # The open Message Batch a DORMANT world is waiting on (TERRARIUM_BATCH_DORMANT).
    # ``batch_tick`` is the tick its entries were built for — a batch that no
    # longer matches is stale — and ``batch_at`` is when it was filed, which is
    # the age the sweep abandons it on. All three are None/0 on the watched path.
    batch_id: str | None = None
    batch_tick: int = 0
    batch_at: datetime | None = None
    # What this world has cost to run: the running ``llm.CostMeter`` summary
    # (model, calls, tokens, cache_marked_calls, cost_usd, cost_per_call),
    # accrued once per tick.
    # It is NOT on the wire — the model name is internal, the same reason
    # ``public_universe_wire`` strips ``physics.models``. Only the derived
    # ``cost_per_watched_hour`` crosses.
    cost: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "terrarium_universes"


class CitizenDoc(TimestampedDocument):
    """One citizen — a Soul with a ledger row and a position.

    ``charter`` is None until the citizen writes its own on its first tick.
    ``soul_path`` points at the ``.soul`` archive; it is a SERVER filesystem
    path and must never reach the public surface.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    universe_id: Indexed(str)  # type: ignore[valid-type]
    name: str
    role: str = ""
    did: str = ""
    parent_did: str | None = None
    generation: int = 1
    soul_path: str | None = None
    ocean: dict[str, float] = Field(default_factory=dict)
    values: list[str] = Field(default_factory=list)
    charter: str | None = None
    balance: int = 0
    trend: Trend = "flat"
    state: CitizenState = "alive"
    x: float = 50.0
    y: float = 50.0
    unlocked: list[str] = Field(default_factory=list)
    born_day: int = 1
    earned_today: int = 0
    spent_today: int = 0

    class Settings:
        name = "terrarium_citizens"


class EventDoc(TimestampedDocument):
    """One Journal row. The Journal is truth; every other doc is a projection.

    ``viewer_origin`` marks text that came from a human. Write-policy: such text
    is NEVER written into a soul as fact (see ``world.label_viewer_claim``).

    ``data`` is the row's free-form structured payload — empty for every act,
    and the ``world.NewMoment`` fields (actors, act_seqs, place, x, y, kind) on
    a ``moment`` row. It exists so a moment rides the SAME Journal the acts do,
    keeping ``seq`` paging and the public events route as the only read path.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    universe_id: Indexed(str)  # type: ignore[valid-type]
    seq: int
    day: int = 1
    tick: int = 0
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: str
    actor: str
    body: str = ""
    cost: int = 0
    artifact_id: str | None = None
    origin: EventOrigin = "citizen"
    viewer_origin: bool = False
    data: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "terrarium_events"


class ArtifactDoc(TimestampedDocument):
    """What a write / craft / build verb left behind.

    ``file_id`` points into the /files surface for payload-bearing artifacts
    (books, laws, maps); structures carry None. ``unlocks`` names the tech-tree
    node this artifact completed, if any.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    universe_id: Indexed(str)  # type: ignore[valid-type]
    kind: ArtifactKind
    name: str
    author: str
    day: int = 1
    cost: int = 0
    file_id: str | None = None
    mime: str | None = None
    x: float | None = None
    y: float | None = None
    unlocks: list[str] = Field(default_factory=list)
    stage: ArtifactStage = "done"
    body: str = ""

    class Settings:
        name = "terrarium_artifacts"


# ---------------------------------------------------------------------------
# Frozen read-path views — what consumers outside the service see.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerRow:
    """One row of the universe ledger (the contract's LedgerRow)."""

    citizen: str
    balance: int
    trend: Trend
    earned_today: int
    spent_today: int
    state: CitizenState


@dataclass(frozen=True)
class WeatherPower:
    """One god power's pledge state (the ``GET /weather`` row)."""

    kind: str
    cost: int
    pledged: int
    gods: int
    ready: bool
    fields: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "EVENT_KINDS",
    "ZERO_COST_KINDS",
    "ArtifactDoc",
    "ArtifactKind",
    "ArtifactStage",
    "CitizenDoc",
    "CitizenState",
    "EventDoc",
    "EventOrigin",
    "LedgerRow",
    "Trend",
    "UniverseDoc",
    "UniverseStatus",
    "WeatherPower",
]
