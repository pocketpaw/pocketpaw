"""The Bundle contract — what a render produces, independent of how it was built.

Created for SG-1 (sites proving harness).

WHAT: ``Bundle`` is ``{files: Mapping[str, bytes], manifest: BundleManifest}`` —
the complete, self-describing output of rendering one spec. Later slices (deploy,
hydration, the fallback ladder) consume exactly this, so the shape is frozen here
and kept free of any renderer implementation detail.

WHY files are bytes keyed by relative POSIX path: a bundle has to survive being
written to disk, zipped, uploaded to an edge, or diffed against a legacy build
without changing shape. Anything richer (open file handles, a directory on disk)
would tie a bundle to the machine that made it.

WHY the manifest carries ``lane`` and ``fallback_rung``: the program's fallback
ladder needs to say WHICH rung served a given render, and the multi-lane matrix
(ripple / svelte / html / react) needs to say which engine produced it. Only the
``ripple`` lane and the ``prebuilt-ssr`` rung exist in SG-1; both fields are here
now so no consumer has to change shape when the later rungs land.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The only lane SG-1 renders. Named rather than inlined so a later slice adding
# 'svelte' / 'html' / 'react' has one place to look.
LANE_RIPPLE = "ripple"

# The primary rung of the fallback ladder: render through the ONCE-BUILT SSR
# bundle. Later rungs (source build in a sandbox, legacy per-site build, cached
# previous publish, ...) get their own constants when they are built.
RUNG_PREBUILT_SSR = "prebuilt-ssr"


@dataclass(frozen=True)
class BundleManifest:
    """Everything a consumer needs to know about a rendered bundle.

    ``entry_html`` is a key into ``Bundle.files``, not a filesystem path — a
    bundle that has never touched disk is still fully described.
    """

    entry_html: str
    asset_paths: tuple[str, ...]
    needs_server_worker: bool
    lane: str
    renderer_version: str
    fallback_rung: str = RUNG_PREBUILT_SSR
    # Free-form provenance (ripple version, bundle shape, timings). Deliberately
    # NOT load-bearing: consumers must not branch on it, so the harness can add
    # diagnostics without breaking the contract.
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view, for the evidence report."""
        return {
            "entry_html": self.entry_html,
            "asset_paths": list(self.asset_paths),
            "needs_server_worker": self.needs_server_worker,
            "lane": self.lane,
            "renderer_version": self.renderer_version,
            "fallback_rung": self.fallback_rung,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class Bundle:
    """A rendered site: its files, and the manifest describing them."""

    files: Mapping[str, bytes]
    manifest: BundleManifest

    @property
    def entry_bytes(self) -> bytes:
        """The entry HTML's bytes.

        Raises ``KeyError`` if the manifest points at a file the bundle does not
        contain. That is a renderer bug, and it must surface loudly rather than
        letting ``verify`` limp on against a missing entry.
        """
        return self.files[self.manifest.entry_html]

    def entry_text(self) -> str:
        return self.entry_bytes.decode("utf-8")
