# tests/cloud/ship/test_engine_metrics.py — the box-metrics parser (SHIP-3).
#
# ``parse_box_metrics`` turns four lines of shell output into the three
# percentages ``GET /ship/boxes/{id}/metrics`` reports. It is pure, so it is
# tested directly: the happy read comes from the recorded transcript in the
# router suite; here we pin the arithmetic and the never-500 fallbacks.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.ship.engine import parse_box_metrics


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        # load 0.42 over 2 cores -> 21% CPU; the rest passes through.
        ("0.42\n2\n37.5\n23%\n", (21.0, 37.5, 23.0)),
        # An overloaded box caps at 100 rather than reporting 400%.
        ("8.0\n2\n99.9\n91%\n", (100.0, 99.9, 91.0)),
        # Garbage on the wire reads as zero, never as a 500 on a health poll.
        ("who knows\n\n\n\n", (0.0, 0.0, 0.0)),
        ("", (0.0, 0.0, 0.0)),
        # A zero core count must not divide by zero.
        ("1.0\n0\n10\n10%\n", (0.0, 10.0, 10.0)),
    ],
)
def test_parse_box_metrics(stdout, expected):
    assert parse_box_metrics(stdout) == expected
