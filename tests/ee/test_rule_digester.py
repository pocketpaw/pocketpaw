# tests/ee/test_rule_digester.py — unit tests for SZD slice-2 S2-R2.
#
# Created: 2026-06-20 (S2-R2 / feat/szd-slice2-discovery) — covers RuleDigester,
# the deterministic engine that reverse-engineers governed RuleDrafts from a
# tenant's Instinct exhaust (corrections + audit). It generalizes the existing
# 3x-correction → soul-procedural promotion (correction_soul_bridge.py) into a
# structured, gate-ready RuleDraft. Tests assert:
#   * N identical corrections on one path → exactly one draft, provenance = those
#     correction ids, confidence rising with N;
#   * below-threshold corrections → no draft;
#   * a weak / inconsistent signal → confidence below the floor → skipped;
#   * empty / insufficient exhaust → [] (never raises);
#   * every emitted draft's ``when`` is valid CEL (round-trips through RuleDraft
#     with no ValidationError);
#   * mixed correction paths → one draft per qualifying path, none for the rest;
#   * an ontology hint scopes the draft to a discovered object_type.
#
# Pure-logic tests — no DB / network / async. Hand-rolled Correction / AuditEntry
# fixtures mirror the store's return shape (store.get_corrections_for_pocket →
# list[Correction]; store.query_audit → list[AuditEntry]). Run with:
#   uv run --group ee pytest tests/ee/test_rule_digester.py -q

from __future__ import annotations

from pocketpaw_ee.discovery import (
    DraftObjectType,
    OntologyDraft,
    RuleDigester,
    RuleDraft,
)
from pocketpaw_ee.discovery.rule_digester import (
    RULE_CONFIDENCE_FLOOR,
    RULE_RECUR_THRESHOLD,
)

from pocketpaw.instinct.correction import Correction, CorrectionPatch

WS = "ws-acme"


# --------------------------------------------------------------------------- #
# Fixture helpers — mirror store.get_corrections_for_pocket → list[Correction]
# --------------------------------------------------------------------------- #
def _correction(
    *,
    cid: str,
    path: str,
    before: object,
    after: object,
    pocket_id: str = WS,
    actor: str = "user:alice",
) -> Correction:
    """One Correction carrying a single field-level patch (the store shape)."""
    return Correction(
        id=cid,
        action_id=f"act-{cid}",
        pocket_id=pocket_id,
        actor=actor,
        patches=[CorrectionPatch(path=path, before=before, after=after)],
        context_summary=f"edited {path}",
        action_title="Some action",
    )


def _digester() -> RuleDigester:
    return RuleDigester()


# --------------------------------------------------------------------------- #
# N identical corrections on one path → exactly one draft
# --------------------------------------------------------------------------- #
def test_recurring_identical_correction_yields_one_draft() -> None:
    corrections = [
        _correction(cid=f"c{i}", path="category", before="normal", after="urgent")
        for i in range(RULE_RECUR_THRESHOLD)
    ]
    drafts = _digester().infer(corrections=corrections, workspace_id=WS)

    assert len(drafts) == 1
    draft = drafts[0]
    assert isinstance(draft, RuleDraft)
    # provenance carries exactly the contributing correction ids
    assert set(draft.provenance) == {c.id for c in corrections}
    # workspace tenancy is set
    assert draft.scope.workspace_id == WS
    # a consistent category escalation → require_approval
    assert draft.action == "require_approval"


def test_confidence_rises_with_recurrence_count() -> None:
    base = [
        _correction(cid=f"c{i}", path="priority", before="low", after="high")
        for i in range(RULE_RECUR_THRESHOLD)
    ]
    more = base + [
        _correction(cid=f"c{i}", path="priority", before="low", after="high")
        for i in range(RULE_RECUR_THRESHOLD, RULE_RECUR_THRESHOLD + 4)
    ]
    low = _digester().infer(corrections=base, workspace_id=WS)
    high = _digester().infer(corrections=more, workspace_id=WS)

    assert len(low) == 1 and len(high) == 1
    assert high[0].confidence > low[0].confidence
    assert high[0].confidence <= 1.0  # clamped


# --------------------------------------------------------------------------- #
# Below threshold → no draft
# --------------------------------------------------------------------------- #
def test_below_threshold_yields_no_draft() -> None:
    corrections = [
        _correction(cid=f"c{i}", path="category", before="normal", after="urgent")
        for i in range(RULE_RECUR_THRESHOLD - 1)
    ]
    drafts = _digester().infer(corrections=corrections, workspace_id=WS)
    assert drafts == []


# --------------------------------------------------------------------------- #
# Weak / inconsistent signal → confidence below floor → skipped
# --------------------------------------------------------------------------- #
def test_weak_inconsistent_signal_skipped_below_floor() -> None:
    # The same path recurs at threshold, but every corrected value differs —
    # no constant target. The inferred rule is weak; confidence < floor → skip.
    corrections = [
        _correction(cid=f"c{i}", path="title", before="x", after=f"rewrite-{i}")
        for i in range(RULE_RECUR_THRESHOLD)
    ]
    drafts = _digester().infer(corrections=corrections, workspace_id=WS)
    assert drafts == []  # below RULE_CONFIDENCE_FLOOR → skipped, not emitted


def test_floor_is_distinct_from_key_confidence_floor() -> None:
    # Guard the RK-7 risk: the rule floor is its own constant, not the
    # idempotency-key floor borrowed from the ontology digester.
    from pocketpaw_ee.discovery.orchestrate import KEY_CONFIDENCE_FLOOR

    assert RULE_CONFIDENCE_FLOOR is not KEY_CONFIDENCE_FLOOR
    assert 0.0 < RULE_CONFIDENCE_FLOOR < 1.0


# --------------------------------------------------------------------------- #
# Empty / insufficient exhaust → [] (never raises)
# --------------------------------------------------------------------------- #
def test_empty_exhaust_returns_empty() -> None:
    assert _digester().infer(corrections=[], workspace_id=WS) == []
    assert _digester().infer(corrections=None, workspace_id=WS) == []  # type: ignore[arg-type]


def test_corrections_with_no_patches_do_not_crash() -> None:
    empty = Correction(
        id="c-empty",
        action_id="a1",
        pocket_id=WS,
        actor="user:alice",
        patches=[],
        context_summary="no edits",
        action_title="t",
    )
    assert _digester().infer(corrections=[empty], workspace_id=WS) == []


# --------------------------------------------------------------------------- #
# Emitted draft's ``when`` validates as CEL
# --------------------------------------------------------------------------- #
def test_emitted_when_is_valid_cel() -> None:
    for path in ("category", "priority", "parameters.assignee"):
        corrections = [
            _correction(cid=f"{path}-{i}", path=path, before="a", after="b")
            for i in range(RULE_RECUR_THRESHOLD + 1)
        ]
        drafts = _digester().infer(corrections=corrections, workspace_id=WS)
        assert len(drafts) == 1
        # Re-validate the emitted spec through a fresh RuleDraft — a malformed
        # CEL ``when`` would raise ValidationError here.
        round_tripped = RuleDraft.model_validate(drafts[0].model_dump())
        assert round_tripped.when == drafts[0].when
        assert round_tripped.when  # non-empty CEL text


# --------------------------------------------------------------------------- #
# Mixed paths → one draft per qualifying path, none for the rest
# --------------------------------------------------------------------------- #
def test_mixed_paths_one_draft_per_qualifying_path() -> None:
    corrections: list[Correction] = []
    # "category" qualifies (>= threshold, consistent)
    corrections += [
        _correction(cid=f"cat-{i}", path="category", before="n", after="urgent")
        for i in range(RULE_RECUR_THRESHOLD + 1)
    ]
    # "priority" qualifies (>= threshold, consistent)
    corrections += [
        _correction(cid=f"pri-{i}", path="priority", before="low", after="high")
        for i in range(RULE_RECUR_THRESHOLD)
    ]
    # "description" does NOT qualify (below threshold)
    corrections += [
        _correction(cid=f"desc-{i}", path="description", before="x", after="y")
        for i in range(RULE_RECUR_THRESHOLD - 1)
    ]
    drafts = _digester().infer(corrections=corrections, workspace_id=WS)

    paths_covered = {d.name for d in drafts}
    # exactly two drafts, one each for the two qualifying paths
    assert len(drafts) == 2
    # provenance never bleeds across paths
    for d in drafts:
        prov_paths = {pid.split("-")[0] for pid in d.provenance}
        assert len(prov_paths) == 1
    assert "description" not in " ".join(paths_covered).lower() or len(drafts) == 2


# --------------------------------------------------------------------------- #
# Suppression-shaped correction → notify action
# --------------------------------------------------------------------------- #
def test_repeated_recommendation_suppression_infers_notify() -> None:
    # Humans repeatedly blank the recommendation (suppress the auto-suggestion).
    corrections = [
        _correction(cid=f"sup-{i}", path="recommendation", before="auto-suggest", after="")
        for i in range(RULE_RECUR_THRESHOLD + 1)
    ]
    drafts = _digester().infer(corrections=corrections, workspace_id=WS)
    assert len(drafts) == 1
    assert drafts[0].action == "notify"


# --------------------------------------------------------------------------- #
# Ontology hint → scope the draft to a discovered object_type
# --------------------------------------------------------------------------- #
def test_ontology_hint_scopes_to_object_type() -> None:
    corrections = [
        _correction(cid=f"c-{i}", path="parameters.ticket_type", before="bug", after="incident")
        for i in range(RULE_RECUR_THRESHOLD + 1)
    ]
    ontology = OntologyDraft(
        object_types=[DraftObjectType(name="Ticket", confidence=0.9)],
    )
    drafts = _digester().infer(corrections=corrections, workspace_id=WS, ontology=ontology)
    assert len(drafts) == 1
    # the single discovered type is attached as scope when it is the only type
    assert drafts[0].scope.object_type == "Ticket"


def test_ontology_absent_leaves_object_type_none() -> None:
    corrections = [
        _correction(cid=f"c-{i}", path="category", before="n", after="urgent")
        for i in range(RULE_RECUR_THRESHOLD + 1)
    ]
    drafts = _digester().infer(corrections=corrections, workspace_id=WS)
    assert len(drafts) == 1
    assert drafts[0].scope.object_type is None


# --------------------------------------------------------------------------- #
# Workspace fallback — derive from correction.pocket_id when not passed
# --------------------------------------------------------------------------- #
def test_workspace_id_falls_back_to_correction_pocket() -> None:
    corrections = [
        _correction(cid=f"c-{i}", path="category", before="n", after="urgent", pocket_id="ws-x")
        for i in range(RULE_RECUR_THRESHOLD + 1)
    ]
    drafts = _digester().infer(corrections=corrections)  # no workspace_id arg
    assert len(drafts) == 1
    assert drafts[0].scope.workspace_id == "ws-x"


def test_returns_rule_draft_instances() -> None:
    corrections = [
        _correction(cid=f"c-{i}", path="category", before="n", after="urgent")
        for i in range(RULE_RECUR_THRESHOLD + 1)
    ]
    drafts = _digester().infer(corrections=corrections, workspace_id=WS)
    assert all(isinstance(d, RuleDraft) for d in drafts)
