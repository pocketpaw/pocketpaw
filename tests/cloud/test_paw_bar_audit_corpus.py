# tests/cloud/test_paw_bar_audit_corpus.py: the shared planted-fault corpus, Python side.
#
# ripple vendors ``paw_bar/ripple-audit-corpus/`` (see ``ripple-audit-corpus.source``):
# each entry's ``expect.python`` is exactly what the strict walk must report on the
# ripple profile, as ``{code, path}`` items with ``path`` a JSON Pointer. Ripple runs
# the same specs against ``expect.core``; a mismatch on either side is drift.
#
# The walk's refusals are free text, so the code comes from ``_CODES`` below, by
# message prefix; a refusal no prefix names fails the test until it gets a code.

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pocketpaw_ee.paw_bar import card_spec
from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, strict_audit

CORPUS = Path(card_spec.__file__).with_name("ripple-audit-corpus")

_CODES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"unknown widget type "), "unknownWidget"),
    (re.compile(r"an event runs an action the bar does not honour"), "badAction"),
    (re.compile(r"an emit names a host event"), "badAction"),
    (re.compile(r"a quiz without \d+ to \d+ questions"), "missingProp"),
]


def _code(message: str) -> str:
    for pattern, code in _CODES:
        if pattern.match(message):
            return code
    raise AssertionError(f"no audit code for the refusal {message!r}; add one to _CODES")


def _audit(spec: dict) -> list[dict[str, str]]:
    return [{"code": _code(i["message"]), "path": i["path"]} for i in strict_audit(spec)]


ENTRIES = sorted(CORPUS.glob("*.json"))


def test_the_vendored_corpus_holds_planted_faults_and_a_clean_control():
    assert len(ENTRIES) >= 3
    entries = [json.loads(p.read_text(encoding="utf-8")) for p in ENTRIES]
    assert any(not e["expect"]["python"] and not e["expect"]["core"] for e in entries)
    assert all(e["expect"]["python"] for e in entries if e["expect"]["core"])


@pytest.mark.parametrize("path", ENTRIES, ids=lambda p: p.stem)
def test_the_strict_walk_reports_exactly_expect_python(path: Path):
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert _audit(entry["spec"]) == entry["expect"]["python"]


def test_strict_audit_points_at_the_offending_value():
    spec = {
        "ui": {
            "type": "card",
            "children": [{"type": "text", "props": {"content": "Hi", "style": {"a/b": 1}}}],
        },
        "state": {"rows": [{"t": "ok"}, {"t": "{x}"}]},
    }
    assert strict_audit(spec, RIPPLE_PROFILE) == []
    bad = {"ui": {"type": "card", "children": [{"type": "text", "props": {"content": "x"}}]}}
    bad["ui"]["children"][0]["props"]["on_click"] = {"action": "explode"}
    assert _audit(bad) == [{"code": "badAction", "path": "/ui/children/0/props/on_click"}]
    assert strict_audit({"ui": []}) == [{"path": "/ui", "message": "ui is not an object"}]
