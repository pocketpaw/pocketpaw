"""Regression tests for the sidebar system status widgets (issue #816)."""

import re
from pathlib import Path

import pytest

SIDEBAR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "pocketpaw"
    / "frontend"
    / "templates"
    / "components"
    / "sidebar.html"
)


@pytest.mark.parametrize("field", ["cpu", "ram", "disk", "battery"])
def test_status_widget_falls_back_to_dash(field):
    """Non-numeric status values (e.g. the initial '...') must render as '—'."""
    html = SIDEBAR.read_text(encoding="utf-8")
    match = re.search(
        rf"x-text=\"typeof status\.{field} === 'number' \? status\.{field} \+ '%' : ([^\"]+)\"",
        html,
    )
    assert match, f"status.{field} widget not found in sidebar.html"
    assert match.group(1).strip() == "'—'"
