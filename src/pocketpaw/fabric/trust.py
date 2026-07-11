# fabric/trust.py — Trust-rule data format for the source-truth resolver (FST-2).
# Created: 2026-07-10 (feat/fst-2-resolver) — the configuration side of the
#   trust-ladder resolver. A ``TrustRules`` object is PLAIN READABLE DATA an
#   agent can print and explain: a global writer-class ladder (strongest
#   first), optional per-(object_type, property) ladder overrides, and the
#   ``recency_epsilon_seconds`` closeness window the resolver uses for
#   un-rankable detection. Nothing reads settings or the store: rules are
#   constructed by the caller and passed into ``resolver.resolve``, keeping
#   the whole resolution path pure.
# Updated: 2026-07-11 (FST-7 — freshness TTL classes + the pure tri-state) —
#   the FST-2 ``freshness_ttl_classes`` placeholder comes alive: three named
#   volatility classes (volatile=1d, default=30d, stable=365d — see
#   DEFAULT_FRESHNESS_TTL_CLASSES) map a class name to a max-age in seconds.
#   The INSTANCE dict stays empty by default (the FST-2 wire format is
#   unchanged — stored rules need no migration and ``default_trust_rules()``
#   still serializes byte-identically); it is an OVERRIDE layer consulted
#   before the module defaults by ``max_age_for``. Per-(object_type,
#   property) class assignment rides ``freshness_overrides`` (same
#   first-exact-match-wins style as the ladder overrides; unassigned
#   properties get class "default"). ``freshness(observed_at,
#   max_age_seconds, now)`` is the pure tri-state: fresh (age <= max_age),
#   aging (age <= 2*max_age), stale beyond. TIMEZONE CONVENTION (one rule for
#   the whole freshness path): every comparison happens in UTC; a NAIVE
#   datetime is interpreted AS UTC at the comparison boundary (the store's
#   own persisted stamps — SQLite datetime('now'), the touch-time backfill
#   from updated_at/created_at — are naive UTC, and the store's live clock is
#   datetime.now(timezone.utc), so naive-as-UTC reads all of those exactly;
#   a naive-LOCAL outlier, e.g. Statement's datetime.now() default, is
#   mis-read by at most the local-UTC offset — hours against day-scale TTLs).
#   Stored data is never rewritten; conversion happens only at comparison.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

# The freshness tri-state a resolved value can carry (FST-7).
Freshness = Literal["fresh", "aging", "stale"]

# The writer classes a Statement can carry (mirrors Statement.writer_class).
WriterClass = Literal["human", "connector", "mirror", "agent", "inferred"]

# The default global ladder, strongest first. "human-pin" is NOT a rung here:
# pinning is a short-circuit the resolver applies BEFORE the ladder (a pinned,
# non-deprecated statement wins outright), so the ladder only orders the five
# writer_class values.
DEFAULT_LADDER: list[WriterClass] = [
    "human",
    "connector",
    "mirror",
    "agent",
    "inferred",
]

# Default closeness window for un-rankable detection: two same-tier statements
# whose observed_at are within this many seconds of each other, carrying
# materially different values, cannot be confidently ranked by recency. 24h is
# deliberately generous for v1 — most connectors sync daily, so same-day
# observations of conflicting values are a real conflict, not a stale echo.
DEFAULT_RECENCY_EPSILON_SECONDS = 24 * 60 * 60.0

# FST-7 — the named volatility classes: TTL class name → max-age in seconds.
# A value older than its class's max-age starts decaying (fresh → aging →
# stale, see ``freshness``). Three classes cover the realistic spread:
#
# - "volatile" (86400s = 1 day)   — values that churn daily and whose staleness
#   bites fast: presence/status flags, queue depths, live availability.
#   Most connectors sync at least daily, so a volatile value that missed one
#   sync cycle is already suspect.
# - "default" (2592000s = 30 days) — the unassigned-property fallback: ordinary
#   business facts (titles, owners, stages, amounts) that drift on a
#   weeks-to-month cadence. A month without re-observation is the point where
#   "probably still true" stops being a safe default.
# - "stable" (31536000s = 365 days) — near-immutable identity facts: legal
#   names, founding dates, tax ids. Only a year of silence makes these worth
#   flagging at all.
#
# These are MODULE defaults: ``TrustRules.freshness_ttl_classes`` (the
# instance dict) stays empty by default — the FST-2 wire format is unchanged,
# stored rules need no migration — and acts as an override layer consulted
# first by ``TrustRules.max_age_for``.
DEFAULT_FRESHNESS_TTL_CLASSES: dict[str, float] = {
    "volatile": 24 * 60 * 60.0,
    "default": 30 * 24 * 60 * 60.0,
    "stable": 365 * 24 * 60 * 60.0,
}

# The class assigned when no freshness override matches (also the lookup
# fallback for an override naming an unknown class — reads never crash on a
# typo'd rule set).
DEFAULT_FRESHNESS_CLASS = "default"


class TrustOverride(BaseModel):
    """A per-(object_type, property) ladder override.

    When a resolver call matches ``object_type`` + ``property`` exactly, this
    ``ladder`` replaces the global one. Writer classes OMITTED from an
    override ladder are not excluded — they rank BELOW every listed class,
    sharing one bottom tier (reads never block, so a partial override must
    not silently drop statements).
    """

    object_type: str
    property: str
    ladder: list[WriterClass]


class FreshnessOverride(BaseModel):
    """A per-(object_type, property) freshness-class assignment (FST-7).

    Same shape and matching semantics as :class:`TrustOverride`: the first
    override matching BOTH fields exactly wins; a property with no match
    gets class ``"default"``. ``ttl_class`` names a key of the effective TTL
    map (instance ``freshness_ttl_classes`` layered over
    ``DEFAULT_FRESHNESS_TTL_CLASSES``); an unknown name falls back to the
    default class rather than failing — reads never block on a bad rule.
    """

    object_type: str
    property: str
    ttl_class: str


class TrustRules(BaseModel):
    """The full rule set the resolver consumes. Plain data, no behavior
    beyond the ``ladder_for`` lookup.

    - ``ladder`` — global writer-class precedence, strongest first.
    - ``overrides`` — per-(object_type, property) ladder replacements; first
      exact match wins.
    - ``recency_epsilon_seconds`` — the un-rankable closeness window (see
      module docstring / ``DEFAULT_RECENCY_EPSILON_SECONDS``).
    - ``freshness_ttl_classes`` — FST-7: per-rule-set OVERRIDES of the named
      TTL classes (class name → max-age seconds), layered over
      ``DEFAULT_FRESHNESS_TTL_CLASSES`` by :meth:`max_age_for`. Empty by
      default — the FST-2 wire format is unchanged; the named defaults
      (volatile=1d, default=30d, stable=365d) apply without any migration.
    - ``freshness_overrides`` — per-(object_type, property) TTL-class
      assignments (first exact match wins, like ``overrides``); an
      unassigned property gets class ``"default"``.
    """

    ladder: list[WriterClass] = Field(default_factory=lambda: list(DEFAULT_LADDER))
    overrides: list[TrustOverride] = Field(default_factory=list)
    recency_epsilon_seconds: float = DEFAULT_RECENCY_EPSILON_SECONDS
    freshness_ttl_classes: dict[str, float] = Field(default_factory=dict)
    freshness_overrides: list[FreshnessOverride] = Field(default_factory=list)

    def ladder_for(self, object_type: str | None, property: str) -> list[WriterClass]:
        """Return the effective ladder for one (object_type, property).

        The first override matching BOTH fields exactly wins; otherwise the
        global ladder applies. ``object_type=None`` (caller didn't know the
        type) never matches an override — overrides are keyed on concrete
        type names.
        """
        if object_type is not None:
            for override in self.overrides:
                if override.object_type == object_type and override.property == property:
                    return override.ladder
        return self.ladder

    def ttl_class_for(self, object_type: str | None, property: str) -> str:
        """The effective freshness TTL class for one (object_type, property).

        Mirrors :meth:`ladder_for`: the first ``freshness_overrides`` entry
        matching BOTH fields exactly wins; ``object_type=None`` never matches
        an override (overrides are keyed on concrete type names); no match →
        ``DEFAULT_FRESHNESS_CLASS``.
        """
        if object_type is not None:
            for override in self.freshness_overrides:
                if override.object_type == object_type and override.property == property:
                    return override.ttl_class
        return DEFAULT_FRESHNESS_CLASS

    def max_age_for(self, object_type: str | None, property: str) -> float:
        """The effective max-age in seconds for one (object_type, property).

        Resolves :meth:`ttl_class_for`'s class name against the instance
        ``freshness_ttl_classes`` first, then the module
        ``DEFAULT_FRESHNESS_TTL_CLASSES``. An unknown class name falls back
        to the ``"default"`` class through the same two layers — a typo'd
        rule set degrades to the 30-day default instead of raising.
        """
        ttl_class = self.ttl_class_for(object_type, property)
        for name in (ttl_class, DEFAULT_FRESHNESS_CLASS):
            if name in self.freshness_ttl_classes:
                return self.freshness_ttl_classes[name]
            if name in DEFAULT_FRESHNESS_TTL_CLASSES:
                return DEFAULT_FRESHNESS_TTL_CLASSES[name]
        return DEFAULT_FRESHNESS_TTL_CLASSES[DEFAULT_FRESHNESS_CLASS]


def _as_utc(value: datetime) -> datetime:
    """THE timezone convention for the freshness path (FST-7).

    Every freshness comparison happens in UTC; a NAIVE datetime is
    interpreted AS UTC here, at the comparison boundary. Why naive-as-UTC:
    the store's persisted stamps (SQLite ``datetime('now')``, the touch-time
    backfill from ``updated_at``/``created_at``) are naive UTC and the
    store's live clock is ``datetime.now(timezone.utc)`` — this rule reads
    all of those exactly. The known naive-LOCAL outlier (``Statement``'s
    ``datetime.now()`` default_factory) is mis-read by at most the
    local-UTC offset, i.e. hours against TTLs measured in days. Stored data
    is never rewritten.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def freshness(observed_at: datetime, max_age_seconds: float, now: datetime) -> Freshness:
    """Classify one observation's age into the FST-7 tri-state.

    Pure: no clock reads — the caller supplies ``now``. Both datetimes are
    UTC-normalized via :func:`_as_utc` (naive = UTC by convention), so naive
    and aware inputs can be mixed without raising.

    - ``fresh`` — age <= max_age (a future observed_at is fresh: negative age)
    - ``aging`` — max_age < age <= 2*max_age (past its TTL but within one
      more TTL window — worth flagging, not yet worth demoting)
    - ``stale`` — age > 2*max_age
    """
    age = (_as_utc(now) - _as_utc(observed_at)).total_seconds()
    if age <= max_age_seconds:
        return "fresh"
    if age <= 2 * max_age_seconds:
        return "aging"
    return "stale"


def default_trust_rules() -> TrustRules:
    """The out-of-the-box rule set: global default ladder, no overrides,
    24h recency epsilon, empty TTL-class override map (the named module
    defaults — volatile=1d, default=30d, stable=365d — apply)."""
    return TrustRules()
