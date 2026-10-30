# tests/cloud/ai_visibility/test_eval_labelled.py — accuracy floor on the labelled set.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). The seed set scores 100%;
# the floor sits below that so real, harder answers can be added without a
# matching change in the same PR, while a regression in the matcher still fails.
from __future__ import annotations

from .eval_labelled import evaluate


def test_labelled_accuracy_floor() -> None:
    report = evaluate()
    assert report["mentioned_n"] >= 20
    assert report["mentioned_accuracy"] >= 0.9, report["misses"]
    assert report["source_type_accuracy"] >= 0.9, report["misses"]
