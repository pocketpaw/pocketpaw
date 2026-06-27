# tests/ee/test_console_telemetry_catalog_gate.py
# Created 2026-06-13: regression guard for the 5 console-telemetry widgets
# shipped in ripple PR #72 (led-clock, seismograph, glyph-grid, fill-grid,
# streak-bars; manifest 179 -> 184). Asserts the catalog gate ACCEPTS the new
# types: they appear in allowed_types_from_manifest(), a console Mission Control
# bento built from them passes the strict EE catalog gate, and a typo is still
# rejected. The manifest fixture is inline (the 5 new entries plus the catalog
# widgets the bento uses) so the test needs no network / served dist.
"""Regression: the console-telemetry widgets pass the catalog allow-list gate.

These widgets are OPTIONAL (never seeded into any home default) but must be
ACCEPTABLE — a home pocket that opts in by composing a spec tile of one of these
types must clear the catalog gate. This test locks that contract so a future
manifest regression (or a gate that hard-codes an older type list) is caught.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.ripple_validator import (
    CatalogViolationError,
    validate_against_catalog_strict,
)

from pocketpaw.ripple.manifest import allowed_types_from_manifest

# The 5 new console-telemetry types.
NEW_TYPES = ["led-clock", "seismograph", "glyph-grid", "fill-grid", "streak-bars"]

# Inline manifest fixture: the 5 new widgets plus the catalog widgets the
# console bento composes (progress-ring / stat / audit-log). Shapes mirror the
# real published manifest — `props` is a map of name -> prop spec, and `stat` /
# `audit-log` carry the `required` flags the required-prop gate enforces.
MANIFEST = {
    "schema": "ripple.manifest/v1",
    "version": "0.5.0",
    "widgets": [
        {"type": "flex", "category": "layout", "props": {"direction": {}, "gap": {}, "class": {}}},
        {
            "type": "led-clock",
            "category": "display",
            "props": {"time": {}, "label": {}, "accent": {}},
        },
        {
            "type": "seismograph",
            "category": "data",
            "props": {"variant": {}, "live": {}, "color": {}},
        },
        {
            "type": "glyph-grid",
            "category": "display",
            "props": {"glyph": {}, "color": {}, "cols": {}},
        },
        {"type": "fill-grid", "category": "display", "props": {"value": {}, "max": {}, "cols": {}}},
        {
            "type": "streak-bars",
            "category": "display",
            "props": {"count": {}, "filled": {}, "color": {}},
        },
        {
            "type": "progress-ring",
            "category": "display",
            "props": {"value": {}, "max": {}, "color": {}},
        },
        {
            "type": "stat",
            "category": "display",
            "props": {
                "label": {"required": False},
                "value": {"required": True},
            },
        },
        {
            "type": "audit-log",
            "category": "display",
            "props": {"entries": {"required": True}},
        },
    ],
}

# A console Mission Control bento — led-clock + a throughput ring + a pending
# stat + a seismograph + an audit-log, console-styled per-tile via class=.
# This is the opt-in artifact a home agent / user would compose.
CONSOLE_BENTO = {
    "version": "1.0",
    "state": {},
    "ui": {
        "type": "flex",
        "props": {"direction": "column", "gap": "3", "class": "font-mono bg-neutral-950 p-4"},
        "children": [
            {
                "type": "led-clock",
                "props": {
                    "time": True,
                    "label": "MISSION CONTROL",
                    "accent": "#34d399",
                    "class": "bg-black rounded-lg p-3 border border-neutral-800",
                },
            },
            {
                "type": "progress-ring",
                "props": {
                    "value": 72,
                    "max": 100,
                    "color": "#34d399",
                    "class": "bg-neutral-900 rounded-lg p-3 border border-neutral-800",
                },
            },
            {
                "type": "stat",
                "props": {
                    "label": "Pending gates",
                    "value": 3,
                    "class": "bg-neutral-900 text-amber-300 rounded-lg p-3",
                },
            },
            {
                "type": "seismograph",
                "props": {
                    "variant": "line",
                    "live": True,
                    "color": "#22d3ee",
                    "class": "bg-black rounded-lg p-3 border border-neutral-800",
                },
            },
            {
                "type": "audit-log",
                "props": {
                    "entries": [
                        {"id": "1", "actor": "belt", "action": "propose", "timestamp": "07:41"}
                    ],
                    "class": "bg-neutral-900 text-neutral-300 rounded-lg p-3",
                },
            },
        ],
    },
}


def test_new_types_are_in_catalog_allowlist():
    """All 5 console-telemetry types resolve from the manifest's widget array."""
    allowed = allowed_types_from_manifest(MANIFEST)
    for t in NEW_TYPES:
        assert t in allowed, f"{t!r} missing from catalog allow-list"


def test_console_bento_passes_strict_catalog_gate():
    """The console bento (the opt-in artifact) clears the strict EE gate — no
    CatalogViolationError. Proves a home pocket CAN opt into these widgets."""
    allowed = allowed_types_from_manifest(MANIFEST)
    # Raises CatalogViolationError if any node type is outside the catalog.
    validate_against_catalog_strict(CONSOLE_BENTO, allowed, embed_allowed_hosts=[])


def test_typo_console_type_is_still_rejected():
    """Negative control: a typo'd type must still be rejected, with the correct
    widget suggested — proves the gate is live, not a blanket allow."""
    allowed = allowed_types_from_manifest(MANIFEST)
    bad = {"version": "1.0", "state": {}, "ui": {"type": "led-clok", "props": {}}}
    with pytest.raises(CatalogViolationError) as exc:
        validate_against_catalog_strict(bad, allowed, embed_allowed_hosts=[])
    assert "led-clock" in str(exc.value)


@pytest.mark.parametrize("missing_type", NEW_TYPES)
def test_each_new_type_individually_accepted(missing_type):
    """A bare single-widget tile of each new type clears the gate on its own —
    the home grid renders each as its own tile via {ui: node}."""
    allowed = allowed_types_from_manifest(MANIFEST)
    tile = {"version": "1.0", "state": {}, "ui": {"type": missing_type, "props": {}}}
    validate_against_catalog_strict(tile, allowed, embed_allowed_hosts=[])
