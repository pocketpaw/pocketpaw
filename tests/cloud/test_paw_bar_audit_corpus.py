# tests/cloud/test_paw_bar_audit_corpus.py: the shared planted-fault corpus, Python side.
#
# ripple vendors ``paw_bar/ripple-audit-corpus/`` (see ``ripple-audit-corpus.source``):
# each entry's ``expect.python`` is exactly what the strict walk must report on the
# ripple profile, as ``{code, path}`` items with ``path`` a JSON Pointer. Ripple runs
# the same specs against ``expect.core``; a mismatch on either side is drift.
#
# The walk's refusals are free text, so the code comes from ``_CODES`` below, by
# message prefix; a refusal no prefix names fails the test until it gets a code.
# Some faults are core-only (an empty ``expect.python``): the walk does not resolve
# expressions, read dates or measure the share link. A ``meta.sample`` card passes
# the walk and validate_and_hydrate like any other.

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pocketpaw_ee.paw_bar import card_spec
from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, strict_audit, validate_and_hydrate

CORPUS = Path(card_spec.__file__).with_name("ripple-audit-corpus")

_CODES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ui is not an object"), "noUi"),
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
    assert sum(bool(e["expect"]["python"]) for e in entries) >= 3


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


def test_a_meta_sample_card_passes_the_walk_and_is_sent():
    spec = json.loads((CORPUS / "sample-future-date.json").read_text(encoding="utf-8"))["spec"]
    assert spec["meta"]["sample"] is True
    assert strict_audit(spec) == []
    assert validate_and_hydrate(spec, [], profile=RIPPLE_PROFILE) == {
        "ui": spec["ui"],
        "state": spec["state"],
    }
