# component_map.py — the real ``ComponentResolver``: which C4 component owns a
# file, read from loom's world model.
#
# Created: 2026-09-12 (integration/belt-factory) — closing the seam
# ``belt/service.py`` shipped with. The entity-events slice defined the
# ``ComponentResolver`` Protocol and shipped ONLY ``no_component``, which
# returns ``None`` unconditionally. Nothing ever injected anything else, so
# every ``belt_entity_changed`` event carried ``component: None``, the Factory
# Map keys its highlight off that field, and no node ever lit: six slices each
# passing their own tests and the feature doing nothing.
#
# What this reads: the loom world model at ``settings.loom_model_path`` (the
# EXISTING setting the loom MCP server already uses — no new knob). The
# file->component mapping lives in its ``edges``: ``type == "composes"``, with
# ``from`` a ``<scope>:file:<repo-relative path>`` id and ``to`` a
# ``<scope>:component:<name>`` id. ``from`` is the SAME id form
# ``service.mint_entity_id`` produces, so the join needs no translation.
#
# The resolver returns the BARE component id — ``pocketpaw:component:fabric``
# resolves to ``fabric`` — because that is what the Factory Map's node ids are.
# Returning the prefixed form would match no node and send every change to the
# unattributed bucket, which is exactly the silent failure this module exists to
# end, so it is pinned by a test of its own.
#
# Caching: the model is ~47k edges, and this is called once per file WRITE on
# the hot path of an agent turn — re-parsing per event would be absurd. The
# parse is memoized on ``(path, mtime_ns)`` via ``lru_cache``, so a world model
# rebuilt by the loom-sync Stop hook is picked up on its next lookup with no
# restart, and a stale entry can never be served. The cache is a memo keyed on
# the file's own identity, NOT a process-global that gates behaviour: nothing
# here reads or writes a flag, and clearing it changes performance only.
#
# Fail-soft, exactly like the rest of the belt live feed: unset path, missing
# file, unreadable file, malformed JSON, or a model whose shape we don't
# recognise all resolve to ``None``. This sits under
# ``emit_belt_entity_changed``, which already swallows a resolver's exceptions —
# but a live feed that can raise into an agent run is a bug even when something
# upstream catches it, so nothing here raises in the first place.
#
# Partial coverage is NORMAL and not a defect to fix here: the model maps ~54%
# of files (1,046 file->component edges), and components with no file edges
# (discovery, enterprise-ops) legitimately own nothing. A file with no owner
# resolves to ``None``, which is the case the UI's unattributed bucket exists
# for.

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, and NO runtime import:
    # ``service`` imports this module, so a runtime import back would cycle.
    from pocketpaw_ee.cloud.belt.service import ComponentResolver

logger = logging.getLogger(__name__)

# The edge that says "this file makes up that component" in a loom world model.
_COMPOSES = "composes"
# Both halves are namespaced ids; we match on the MARKER rather than on a
# hardcoded ``pocketpaw:`` scope, so a soul-protocol or kb-go world model works
# through the same code with no change.
_FILE_MARKER = ":file:"
_COMPONENT_MARKER = ":component:"


def _parse_edges(raw: str) -> dict[str, str]:
    """Build ``{file entity id: bare component id}`` from a world model's JSON.

    Total on malformed input: a model that isn't a dict, has no ``edges``, or
    whose edges aren't dicts yields an EMPTY map rather than raising. A partial
    model still contributes whatever edges it does have — a truncated world
    model should degrade the highlight, not disable it.
    """
    model = json.loads(raw)
    if not isinstance(model, dict):
        return {}
    edges = model.get("edges")
    if not isinstance(edges, list):
        return {}

    mapping: dict[str, str] = {}
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("type") != _COMPOSES:
            continue
        source, target = edge.get("from"), edge.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if _FILE_MARKER not in source or _COMPONENT_MARKER not in target:
            continue
        # The BARE id — the last colon segment. See the header: the prefixed
        # form matches no Factory Map node.
        mapping[source] = target.rsplit(":", 1)[-1]
    return mapping


@lru_cache(maxsize=4)
def _mapping(path: str, _mtime_ns: int) -> dict[str, str]:
    """The parsed mapping for ONE version of ONE model file.

    ``_mtime_ns`` is unused in the body and load-bearing in the signature: it is
    what makes a rebuilt model a cache MISS. ``maxsize=4`` bounds the memo when
    a long-lived process sees the model rebuilt repeatedly.
    """
    try:
        return _parse_edges(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unreadable or not JSON. Debug, not warning: an operator who has not
        # built a world model should not get a log line per file write.
        logger.debug("belt: could not read the loom world model at %s", path, exc_info=True)
        return {}


def _current_mapping() -> dict[str, str]:
    """The mapping for the CURRENT model on disk, or empty when there is none.

    Settings are read per call, never captured at import, so turning loom on or
    off takes effect without a restart and no stale flag can survive.
    """
    try:
        from pocketpaw.config import get_settings

        configured = getattr(get_settings(), "loom_model_path", None)
        if not configured:
            return {}
        # One stat per lookup — cheap, and the only way a rebuilt model is
        # noticed without a restart. The file READ happens once per version.
        return _mapping(str(configured), Path(configured).stat().st_mtime_ns)
    except (OSError, AttributeError, ImportError):
        logger.debug("belt: loom world model unavailable for component lookup", exc_info=True)
        return {}


def loom_component(entity_id: str) -> str | None:
    """The C4 component owning ``entity_id``, or ``None``.

    A ``ComponentResolver`` (structurally — the Protocol is one call). ``None``
    covers every non-answer with no way for a caller to tell them apart, and
    that is deliberate: "loom is not configured", "this file has no owner", and
    "the model is unreadable" all mean the same thing to the consumer, which is
    that this change goes in the unattributed bucket.
    """
    return _current_mapping().get(entity_id)


def loom_component_resolver() -> ComponentResolver:
    """``loom_component`` as an injectable ``ComponentResolver``.

    Never ``None``: a caller would only have to substitute ``no_component`` for
    it, and an always-``None`` resolver already IS that. One less branch at
    every call site, and one less way to forget the branch.
    """
    return loom_component


__all__ = ["loom_component", "loom_component_resolver"]
