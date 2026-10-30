"""Parity between the server SVG policy and Ripple's browser-side check.

The same fixture lives in ripple at
packages/svelte/src/lib/security/__fixtures__/illustration-parity.json, where
the landing's `checkIllustrationSvg` must give the same verdict. Change a
verdict here only together with that copy, or the two layers drift.
"""

import json
from pathlib import Path

import pytest
from pocketpaw_ee.paw_bar.illustration_svg import svg_violation

_CASES = json.loads((Path(__file__).parent / "fixtures" / "illustration-parity.json").read_text())


@pytest.mark.parametrize("name", sorted(_CASES))
def test_server_verdict_matches_fixture(name: str) -> None:
    case = _CASES[name]
    assert (svg_violation(case["svg"]) is None) is case["ok"]
