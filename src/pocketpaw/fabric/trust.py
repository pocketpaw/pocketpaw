# fabric/trust.py — Trust-rule data format for the source-truth resolver (FST-2).
# Created: 2026-07-10 (feat/fst-2-resolver) — the configuration side of the
#   trust-ladder resolver. A ``TrustRules`` object is PLAIN READABLE DATA an
#   agent can print and explain: a global writer-class ladder (strongest
#   first), optional per-(object_type, property) ladder overrides, and the
#   ``recency_epsilon_seconds`` closeness window the resolver uses for
#   un-rankable detection. ``freshness_ttl_classes`` is a declared-but-unused
#   placeholder for the freshness TTL classes arriving in FST-7 — no TTL
#   logic lives here yet. Nothing reads settings or the store: rules are
#   constructed by the caller and passed into ``resolver.resolve``, keeping
#   the whole resolution path pure.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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


class TrustRules(BaseModel):
    """The full rule set the resolver consumes. Plain data, no behavior
    beyond the ``ladder_for`` lookup.

    - ``ladder`` — global writer-class precedence, strongest first.
    - ``overrides`` — per-(object_type, property) ladder replacements; first
      exact match wins.
    - ``recency_epsilon_seconds`` — the un-rankable closeness window (see
      module docstring / ``DEFAULT_RECENCY_EPSILON_SECONDS``).
    - ``freshness_ttl_classes`` — FST-7 placeholder: will map a TTL class
      name to a max-age in seconds for freshness demotion. Carried in the
      format now so stored rules don't need a migration later; NO logic
      consumes it in FST-2.
    """

    ladder: list[WriterClass] = Field(default_factory=lambda: list(DEFAULT_LADDER))
    overrides: list[TrustOverride] = Field(default_factory=list)
    recency_epsilon_seconds: float = DEFAULT_RECENCY_EPSILON_SECONDS
    freshness_ttl_classes: dict[str, float] = Field(default_factory=dict)

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


def default_trust_rules() -> TrustRules:
    """The out-of-the-box rule set: global default ladder, no overrides,
    24h recency epsilon, no TTL classes."""
    return TrustRules()
