# ee/pocketpaw_ee/terrarium/soul_link.py
#
# The citizen ↔ Soul bridge. A CITIZEN IS A SOUL: birth mints a ``.soul``
# archive with the citizen's OCEAN and values, each tick recalls a few memories
# before the judgment call, and each tick appends ONE episodic memory after it.
#
# Narrow on purpose (three functions) so the transport can be swapped without
# touching world/service, and EVERYTHING is best-effort: a missing file, a
# corrupt soul or a protocol error logs and degrades to a no-op. A soul failure
# must never wedge a tick — the Journal is the truth, the soul is enrichment.
#
# Write-policy note: this module writes whatever text it is handed. The rule
# that viewer-origin text never becomes soul fact is enforced UPSTREAM, in
# ``world.episodic_summary`` (citizen-origin events only). Callers must not
# hand viewer text to ``remember_tick``.

"""Best-effort Soul bridge for terrarium citizens (birth / recall / remember)."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_RECALL_LIMIT = 5


async def birth_soul(
    soul_path: str | Path,
    *,
    name: str,
    role: str,
    ocean: dict[str, float],
    values: list[str],
    world_brief: str,
) -> str | None:
    """Mint a citizen's ``.soul`` file. Returns its DID, or None on any failure.

    Best-effort: a universe whose souls fail to mint still runs (citizens keep
    their OCEAN + values on the CitizenDoc), it just has no long-lived memory.
    The world brief lands as the citizen's first memory — its only knowledge of
    where it woke up. Its charter is NOT written here: the citizen writes that
    itself on its first tick (the zero ritual).
    """
    path = Path(soul_path).expanduser()
    try:
        from soul_protocol import Soul

        path.parent.mkdir(parents=True, exist_ok=True)
        soul = await Soul.birth(
            name=name,
            role=role,
            values=list(values or []),
            ocean={_OCEAN_FIELDS.get(k, k): float(v) for k, v in (ocean or {}).items()},
            **_EVOLUTION_KWARG,
        )
        _unfreeze_personality(soul)
        if world_brief.strip():
            await soul.remember(world_brief.strip(), importance=9)
        await soul.save_local(path)
        return str(soul.did or "")
    except Exception:  # noqa: BLE001 — a soul failure must never wedge creation
        logger.warning("terrarium soul: birth failed for %s", path, exc_info=True)
        return None


# Citizens must be able to have children who differ from them.
#
# ``EvolutionConfig.immutable_traits`` defaults to ``["personality",
# "core_values"]`` and the "personality" category gates all five OCEAN traits,
# so a default-born soul FORKS INTO AN EXACT CLONE however much drift is asked
# for — silently, with nothing in the logs. A universe seeded that way looks
# fine and evolves never.
#
# Newer soul-protocol takes the config at birth. Older published versions
# accept the keyword and merely WARN that they ignored it, which would leave
# the traits frozen without failing, so passing it is not enough on its own:
# ``_unfreeze_personality`` checks the soul we actually got and repairs it.
# Drop the repair (and this note) once the floor version supports the kwarg.
_EVOLUTION_KWARG = {"evolution": {"immutable_traits": ["core_values"]}}


def _unfreeze_personality(soul: object) -> None:
    """Ensure this soul's OCEAN traits can drift when it forks a child."""
    try:
        config = soul._evolution.config  # type: ignore[attr-defined]  # noqa: SLF001
        if "personality" in config.immutable_traits:
            config.immutable_traits = [t for t in config.immutable_traits if t != "personality"]
    except Exception:  # noqa: BLE001 — best-effort, like everything in this module
        logger.warning("terrarium soul: could not unfreeze personality traits", exc_info=True)


# OCEAN letters (the contract's citizen shape) -> soul-protocol trait names.
_OCEAN_FIELDS = {
    "O": "openness",
    "C": "conscientiousness",
    "E": "extraversion",
    "A": "agreeableness",
    "N": "neuroticism",
}


async def recall_for_tick(soul_path: str | None, query: str) -> list[str]:
    """Recall up to 5 memory lines relevant to ``query``. Empty on any failure."""
    if not soul_path:
        return []
    path = Path(soul_path).expanduser()
    if not path.exists():
        return []
    try:
        from soul_protocol import Soul

        soul = await Soul.awaken(path)
        entries = await soul.recall(query, limit=_RECALL_LIMIT)
        return [str(e.content) for e in entries]
    except Exception:  # noqa: BLE001 — soul failures must never wedge a tick
        logger.warning("terrarium soul: recall failed for %s", soul_path, exc_info=True)
        return []


async def remember_tick(soul_path: str | None, summary: str) -> bool:
    """Append ONE episodic memory for a finished tick. False (logged) on failure.

    Callers must pass a citizen-origin summary only (see the module note).
    """
    if not soul_path or not summary.strip():
        return False
    path = Path(soul_path).expanduser()
    if not path.exists():
        return False
    try:
        from soul_protocol import MemoryType, Soul

        soul = await Soul.awaken(path)
        await soul.remember(summary.strip(), type=MemoryType.EPISODIC, importance=6)
        await soul.save_local(path)
        return True
    except Exception:  # noqa: BLE001 — soul failures must never wedge a tick
        logger.warning("terrarium soul: remember failed for %s", soul_path, exc_info=True)
        return False


__all__ = ["birth_soul", "recall_for_tick", "remember_tick"]
