# tests/ee/sites/test_html_uid_stamp.py — the Python html data-uid stamper must stamp
# exactly what paw-sites ``arm-html`` stamps, or refuse the page.
#
# fixtures/html_uid_parity/arm_html_parity.json holds input pages and the stamped
# source paw-sites ``arm-html`` produced for them (commit recorded in the file). The
# well-formed pages cover what the Sites agent writes (head/title, svg icons inside
# links, tables with an implied tbody, entities, CRLF, unquoted and uppercase
# attributes, pre/textarea, template/noscript/script/style, math and foreignObject,
# an already-stamped leaf). The ``malformed/`` pages need HTML5 tree repair; the port
# may refuse those, but if it stamps one it must still match paw-sites byte for byte.
"""Parity between html_uid_stamp and paw-sites arm-html."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pocketpaw_ee.sites.html_uid_stamp import classify_role, stamp_html_data_uids

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "html_uid_parity" / "arm_html_parity.json").read_text(
        "utf-8"
    )
)
PAGES: dict[str, str] = FIXTURE["pages"]
ARMED: dict[str, str] = FIXTURE["armed"]
WELL_FORMED = sorted(k for k in PAGES if not k.startswith("malformed/"))
MALFORMED = sorted(k for k in PAGES if k.startswith("malformed/"))


@pytest.mark.parametrize("rel", WELL_FORMED)
def test_well_formed_pages_stamp_exactly_like_paw_sites(rel):
    assert stamp_html_data_uids(PAGES[rel], rel) == ARMED[rel]


@pytest.mark.parametrize("rel", MALFORMED)
def test_pages_needing_html5_repair_match_or_are_refused(rel):
    out = stamp_html_data_uids(PAGES[rel], rel)
    assert out is None or out == ARMED[rel]


def test_the_known_repair_cases_are_refused_not_guessed():
    # Each of these makes parse5 build a different tree than the token stream says
    # (implied </p>, implied </li>, nested <a>, foster parenting, adoption agency,
    # ignored stray end tag, <image> -> <img>, unclosed elements at </body>).
    for rel in MALFORMED:
        if rel == "malformed/selfclose-div.html":
            continue  # <div/> stays open to EOF in both parsers: stamped, and equal
        assert stamp_html_data_uids(PAGES[rel], rel) is None, rel


def test_stamping_only_inserts_attributes():
    rel = "index.html"
    out = stamp_html_data_uids(PAGES[rel], rel)
    assert out is not None

    def strip(s: str) -> str:
        return re.sub(r' data-uid="[^"]*"', "", s)

    assert strip(out) == strip(PAGES[rel])
    assert len(out) > len(PAGES[rel])


def test_stamping_is_idempotent():
    rel = "kitchen.html"
    once = stamp_html_data_uids(PAGES[rel], rel)
    assert once is not None
    assert stamp_html_data_uids(once, rel) == once


def test_classify_role_mirrors_paw_sites():
    assert classify_role("h2", []) == "headline"
    assert classify_role("p", ["Lead"]) == "subhead"
    assert classify_role("a", ["btn"]) == "cta"
    assert classify_role("span", ["pill"]) == "badge"
    assert classify_role("h3", ["kicker"]) == "eyebrow"
    assert classify_role("li", []) is None


def test_a_bom_or_nul_page_is_refused():
    assert stamp_html_data_uids("﻿<h1>x</h1>", "index.html") is None
    assert stamp_html_data_uids("<h1>x\x00</h1>", "index.html") is None
