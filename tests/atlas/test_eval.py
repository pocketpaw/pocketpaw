# tests/atlas/test_eval.py — intent→capability ranking-regression eval
# harness for the atlas OS self-model (AT-2). Created: 2026-07-02
# (feat/atlas-eval). Loads real-user intents from eval_cases.json and,
# per case, asserts the expected entry id appears within ``rank_within``
# results of ``AtlasStore.search``. Cases marked ``xfail`` are known
# ranking misses kept on purpose (strict xfail — they fail loudly the
# moment the ranking improves, forcing a baseline update). A summary test
# pins the measured strict-hit score (expected id at rank 1) to a
# recorded baseline so ranking changes can never silently regress.
# Eval-only: this file must not require changes under src/pocketpaw.
# Updated: 2026-07-03 — re-baselined against the full compiled model
# (237 entries) + fixpoint stemmer: baseline 16/18 → 21/22 (cases
# re-pointed / promoted / added in eval_cases.json; see its note fields).
# Updated: 2026-07-03 (feat/workspace-admin-tools, WA-3) — the model grew to
# 254 entries (17 role-gated admin ``capability`` cards added). Those cards
# are hidden from the UNFILTERED store the eval uses, but their natural-phrase
# names would have polluted rankings via bare stopwords; the store now drops a
# tiny function-word stoplist (``store._STOPWORDS``). Net effect on the eval:
# strict-hit baseline UNCHANGED at 21/22 (the "review the agent's edit" case
# even tightened from rank 3 to rank 2, still within its rank_within budget).
# Updated: 2026-07-05 (fix/atlas-data-accuracy-and-relevance) — the model grew
# to 256 entries (two new authored surfaces: /settings/billing and /security).
# Nine cases added pinning the data-accuracy + relevance fixes (billing /
# deep-work / security surfaces, 'user' member vocabulary, the IDF name-weight
# damper, the five new instruction-filler stopwords, the orientation query).
# All nine measure rank 1, so the strict-hit baseline rises 21/22 → 30/31.
# Updated: 2026-07-05 (fix/atlas-relevance-round2, Finding A) — five governance
# paraphrase cases added. Four ("how do I approve what the agent does",
# "sign-off", "gate the agent", "gate what the agent can do") measure rank 1
# after the governance keywords on primitive:instinct + the store's kind-priority
# bias (a management surface can no longer outrank the governing primitive at
# equal overlap). The fifth ("approval gate") is pinned rank_within 2 — Instinct
# co-answers behind the owner-only approval-LEVEL capability, which the small
# kind bias deliberately does not overpower. Strict-hit baseline 30/31 → 34/36.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pocketpaw.atlas.store import AtlasStore

_CASES_PATH = Path(__file__).parent / "eval_cases.json"

# Measured baseline (2026-07-05, full compiled model of 256 entries, fixpoint
# stemmer + IDF name-weight damper + round-2 kind-priority bias): 34 of 36
# cases put the expected id at rank 1. The two non-rank-1: "review the agent's
# edit before it goes live" ranks branch #3 behind instinct and the edit-pocket
# skill (generic review/approve vocabulary overlaps instinct's core keywords —
# known weakness), and "approval gate" ranks instinct #2 behind the owner-only
# approval-LEVEL capability (a legitimate co-answer the small kind bias does not
# overpower). Both are within their rank_within budgets.
# If a ranking change LOWERS the strict-hit count below this, the summary
# test fails; if it raises it, bump the constant in the same PR.
STRICT_HIT_BASELINE = 34

# Search depth for the eval: at least as deep as the largest rank_within,
# generous enough that "not found at all" is a ranking fact, not a limit
# artifact (zero-overlap entries are dropped regardless of limit).
_SEARCH_LIMIT = 5


def _load_cases() -> list[dict]:
    data = json.loads(_CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 12, "eval suite must keep at least 12 cases"
    return cases


_CASES = _load_cases()


@pytest.fixture(scope="module")
def store() -> AtlasStore:
    # A fresh store (not the process singleton) so the eval is hermetic.
    return AtlasStore.load()


def _rank_of(store: AtlasStore, intent: str, expected_id: str) -> int | None:
    """1-based rank of expected_id in search results, or None if absent."""
    ids = [entry.id for entry in store.search(intent, limit=_SEARCH_LIMIT)]
    return ids.index(expected_id) + 1 if expected_id in ids else None


def _case_params() -> list:
    params = []
    for case in _CASES:
        marks = []
        if case.get("xfail"):
            marks.append(pytest.mark.xfail(reason=case["xfail"], strict=True))
        params.append(pytest.param(case, marks=marks, id=case["intent"][:48]))
    return params


@pytest.mark.parametrize("case", _case_params())
def test_intent_maps_to_expected_capability(store: AtlasStore, case: dict) -> None:
    rank = _rank_of(store, case["intent"], case["expected_id"])
    within = case["rank_within"]
    assert rank is not None and rank <= within, (
        f"intent {case['intent']!r}: expected {case['expected_id']} within "
        f"rank {within}, got rank {rank} "
        f"(top: {[e.id for e in store.search(case['intent'], limit=3)]})"
    )


def test_strict_hit_score_meets_baseline(store: AtlasStore) -> None:
    """Strict-hit score (expected id at rank 1) must never regress."""
    hits = [case for case in _CASES if _rank_of(store, case["intent"], case["expected_id"]) == 1]
    misses = [case["intent"] for case in _CASES if case not in hits]
    score = len(hits)
    assert score >= STRICT_HIT_BASELINE, (
        f"strict-hit score regressed: {score}/{len(_CASES)} < baseline "
        f"{STRICT_HIT_BASELINE}/{len(_CASES)}; misses: {misses}"
    )
    # Ranking improved? Record it: bump STRICT_HIT_BASELINE in this PR.
    assert score == STRICT_HIT_BASELINE, (
        f"strict-hit score improved to {score}/{len(_CASES)} — raise "
        f"STRICT_HIT_BASELINE to {score} so the gain is locked in"
    )


def test_all_expected_ids_exist_in_seed(store: AtlasStore) -> None:
    """Guard against typo'd expected ids making a case unwinnable."""
    known = {entry.id for entry in store.entries}
    unknown = {c["expected_id"] for c in _CASES} - known
    assert not unknown, f"eval cases reference unknown atlas ids: {unknown}"
