# Sense vocabulary — the curated CORE capability vocabulary + validation + static index.
# Created: 2026-06-08 — OSS catalog half (RFC Sense tier, chunk 1).
# A Sense is a provider-agnostic capability above Connectors. Templates/agents
# address a Sense (e.g. paw.email.v1); a resolver (built later, not here) binds
# it to whatever connector a tenant enabled. This module owns:
#   - CORE_SENSES: the in-repo, versioned curated vocabulary (id/name/description).
#   - validate_sense_id / is_core_sense: the "no fragmentation of the core" rule.
#   - connectors_for_sense: a pure static index over parsed ConnectorDefs.
# Each core sense MUST be declared by at least one connector YAML (guard test).

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocketpaw.connectors.yaml_engine import ConnectorDef

# Bump when the core vocabulary changes. Core sense ids are themselves
# versioned (paw.<domain>.vN); this version tracks the catalog as a whole.
SENSE_VOCAB_VERSION = "1"

# Core sense ids follow paw.<domain>.vN. Extension (vendor) ids follow
# vendor.domain.vN and are accepted freely — the core is closed, the
# extension space is open.
_CORE_ID_PATTERN = re.compile(r"^paw\.[a-z0-9_]+\.v\d+$")
_EXTENSION_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.v\d+$")


class SenseValidationError(ValueError):
    """Raised when a sense id is malformed or an unknown paw.* core id."""


@dataclass(frozen=True)
class CoreSense:
    """A single curated core capability in the versioned vocabulary."""

    id: str
    display_name: str
    description: str


# The v1 core vocabulary. Every entry here is backed by at least one connector
# YAML in connectors/ (enforced by the guard test). Do NOT add a core sense
# without a connector that can fill it — that is the anti-fragmentation rule.
CORE_SENSES: tuple[CoreSense, ...] = (
    CoreSense(
        id="paw.email.v1",
        display_name="Email",
        description="Read, search, and send email across providers.",
    ),
    CoreSense(
        id="paw.calendar.v1",
        display_name="Calendar",
        description="Read and manage calendar events and availability.",
    ),
    CoreSense(
        id="paw.code.v1",
        display_name="Code",
        description="Access repositories, issues, pull requests, and code search.",
    ),
    CoreSense(
        id="paw.payments.v1",
        display_name="Payments",
        description="Read payment, subscription, and billing data.",
    ),
    CoreSense(
        id="paw.db.v1",
        display_name="Database",
        description="Query structured data in a relational or document database.",
    ),
    CoreSense(
        id="paw.docs.v1",
        display_name="Documents",
        description="Read, search, and manage documents and files.",
    ),
)

_CORE_IDS: frozenset[str] = frozenset(s.id for s in CORE_SENSES)


def is_core_sense(sense_id: str) -> bool:
    """True if ``sense_id`` is one of the curated core senses."""
    return sense_id in _CORE_IDS


def validate_sense_id(sense_id: str) -> str:
    """Validate a sense id, returning it unchanged if valid.

    Rules:
      - A ``paw.*`` id is accepted ONLY if it is in the curated core set.
        An unknown ``paw.*`` id raises — the core namespace is closed so
        templates can't silently fragment it.
      - A non-``paw.*`` id (vendor extension, format ``vendor.domain.vN``)
        is accepted freely — the extension space is open.
      - Anything that is neither a well-formed core id nor a well-formed
        extension id raises.
    """
    if not isinstance(sense_id, str) or not sense_id:
        raise SenseValidationError(f"sense id must be a non-empty string, got {sense_id!r}")

    if sense_id.startswith("paw."):
        if not _CORE_ID_PATTERN.match(sense_id):
            raise SenseValidationError(
                f"malformed core sense id {sense_id!r} (expected paw.<domain>.vN)"
            )
        if sense_id not in _CORE_IDS:
            raise SenseValidationError(
                f"unknown core sense id {sense_id!r}; the paw.* namespace is "
                f"closed. Known core senses: {sorted(_CORE_IDS)}. "
                f"Use a vendor.domain.vN id for custom capabilities."
            )
        return sense_id

    # Extension (vendor) id — open namespace, just enforce the shape.
    if not _EXTENSION_ID_PATTERN.match(sense_id):
        raise SenseValidationError(
            f"malformed extension sense id {sense_id!r} (expected vendor.domain.vN)"
        )
    return sense_id


def connectors_for_sense(sense_id: str, connector_defs: list[ConnectorDef]) -> list[str]:
    """Return the names of connectors that declare ``sense_id``.

    Pure static index — given already-parsed ConnectorDefs, returns the
    connector names whose ``senses`` list contains ``sense_id``. No tenant
    state and no I/O beyond reading the passed-in defs. Results are sorted
    for determinism.
    """
    return sorted(d.name for d in connector_defs if sense_id in getattr(d, "senses", []))
