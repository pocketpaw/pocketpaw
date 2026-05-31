# tests/cloud/test_instinct_verification.py — smoke tests for the
# deterministic outcome verifier (issue #1162).
# Created: 2026-05-21 (feat/instinct-outcome-verification)
#
# Covers the foundation verifier: verify_outcome / check_criterion against
# simple criteria/result pairs. The verifier is deterministic — no LLM — so
# every assertion here is an exact, repeatable check. The four verdict
# states (solved / partial / not_solved / unknown), the OutcomeVerdict
# roll-up counts, and the structured-verdict round-trip through the
# instinct store all get exercised.

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.instinct.models import (
    ActionTrigger,
    OutcomeStatus,
    OutcomeVerdict,
)
from pocketpaw.instinct.store import InstinctStore
from pocketpaw.instinct.verification import check_criterion, verify_outcome

# ---------------------------------------------------------------------------
# check_criterion — single criterion against a result
# ---------------------------------------------------------------------------


class TestCheckCriterion:
    """A single success criterion checked against an action result."""

    def test_criterion_met_when_keywords_present(self):
        # The result text contains every content word of the criterion
        # (list, overdue, invoice) — the deterministic matcher passes it.
        result = "Produced a list of every overdue invoice in the system"
        cr = check_criterion(result, "A list of overdue invoices is produced")
        assert cr.met is True
        assert cr.criterion == "A list of overdue invoices is produced"

    def test_criterion_not_met_on_synonym_only_overlap(self):
        # Deterministic foundation: it keys off the criterion's actual
        # words. A result that uses a synonym ("pulled" for "produced")
        # does NOT satisfy the criterion — catching that is the deferred
        # LLM-as-judge follow-up, not this verifier.
        result = "Pulled a list of overdue invoices"
        cr = check_criterion(result, "A list of overdue invoices is produced")
        assert cr.met is False
        assert "produced" in cr.detail

    def test_criterion_not_met_when_keywords_absent(self):
        result = "The connector returned an authentication error"
        cr = check_criterion(result, "A list of overdue invoices is produced")
        assert cr.met is False
        # The detail names what was missing.
        assert "missing" in cr.detail.lower()

    def test_plural_and_singular_match(self):
        # "emails" in the result should satisfy "email" in the criterion.
        cr = check_criterion("Sent three reminder emails", "A reminder email is sent")
        assert cr.met is True

    def test_criterion_with_no_content_is_not_met(self):
        cr = check_criterion("any result text", "...")
        assert cr.met is False
        assert "no checkable content" in cr.detail.lower()

    def test_dict_result_is_searched(self):
        # A structured (dict) result is flattened and searched.
        result = {"status": "done", "note": "invoice reminder emails sent"}
        cr = check_criterion(result, "reminder emails sent")
        assert cr.met is True


# ---------------------------------------------------------------------------
# verify_outcome — roll-up verdict over multiple criteria
# ---------------------------------------------------------------------------


class TestVerifyOutcomeSolved:
    """All criteria met -> SOLVED."""

    def test_all_criteria_met_is_solved(self):
        result = "Generated the overdue invoice list and emailed every customer"
        verdict = verify_outcome(
            result,
            [
                "An overdue invoice list is generated",
                "Every customer is emailed",
            ],
        )
        assert verdict.status == OutcomeStatus.SOLVED
        assert verdict.met_count == 2
        assert verdict.total_count == 2

    def test_single_criterion_met_is_solved(self):
        verdict = verify_outcome(
            "The reminder email was sent",
            ["A reminder email is sent"],
        )
        assert verdict.status == OutcomeStatus.SOLVED


class TestVerifyOutcomeNotSolved:
    """No criteria met -> NOT_SOLVED."""

    def test_no_criteria_met_is_not_solved(self):
        verdict = verify_outcome(
            "The job crashed before doing anything useful",
            ["An overdue invoice list is generated"],
        )
        assert verdict.status == OutcomeStatus.NOT_SOLVED
        assert verdict.met_count == 0

    def test_empty_result_is_not_solved(self):
        verdict = verify_outcome("", ["A reminder email is sent"])
        assert verdict.status == OutcomeStatus.NOT_SOLVED


class TestVerifyOutcomePartial:
    """Some criteria met, some not -> PARTIAL."""

    def test_some_criteria_met_is_partial(self):
        result = "Sent the reminder emails"
        verdict = verify_outcome(
            result,
            [
                "Reminder emails are sent",
                "A do-not-contact customer is skipped",
            ],
        )
        assert verdict.status == OutcomeStatus.PARTIAL
        assert verdict.met_count == 1
        assert verdict.total_count == 2
        # The per-criterion breakdown shows which one passed.
        met = [c for c in verdict.criteria_results if c.met]
        assert len(met) == 1
        assert "Reminder emails" in met[0].criterion


class TestVerifyOutcomeUnknown:
    """No criteria captured -> UNKNOWN — nothing to check."""

    def test_empty_criteria_is_unknown(self):
        verdict = verify_outcome("the task finished", [])
        assert verdict.status == OutcomeStatus.UNKNOWN
        assert verdict.criteria_results == []

    def test_blank_criteria_are_ignored(self):
        # Whitespace-only criteria are dropped; nothing left -> UNKNOWN.
        verdict = verify_outcome("the task finished", ["", "   "])
        assert verdict.status == OutcomeStatus.UNKNOWN

    def test_summary_explains_unknown(self):
        verdict = verify_outcome("done", [])
        assert "cannot be verified" in verdict.summary.lower()


# ---------------------------------------------------------------------------
# OutcomeVerdict — model behavior
# ---------------------------------------------------------------------------


class TestOutcomeVerdict:
    """The structured verdict model."""

    def test_met_and_total_counts(self):
        verdict = verify_outcome(
            "Generated the report",
            ["A report is generated", "An email is sent", "A file is saved"],
        )
        # Only the first criterion's keywords appear in the result.
        assert verdict.total_count == 3
        assert verdict.met_count == 1
        assert verdict.status == OutcomeStatus.PARTIAL

    def test_verdict_json_round_trip(self):
        verdict = verify_outcome("the email was sent", ["An email is sent"])
        restored = OutcomeVerdict.model_validate_json(verdict.model_dump_json())
        assert restored.status == verdict.status
        assert restored.met_count == verdict.met_count
        assert restored.criteria_results[0].criterion == "An email is sent"


# ---------------------------------------------------------------------------
# InstinctStore — structured verdict persists through mark_executed
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    """Isolated SQLite store on a temp file."""
    return InstinctStore(tmp_path / "verification_test.db")


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="claude", reason="verification test")


class TestStoreOutcomeVerdict:
    """mark_executed accepts and round-trips a structured OutcomeVerdict."""

    @pytest.mark.asyncio
    async def test_mark_executed_stores_structured_verdict(self, store: InstinctStore):
        action = await store.propose(
            pocket_id="pocket-1",
            title="Send overdue reminders",
            description="",
            recommendation="",
            trigger=_trigger(),
        )
        await store.approve(action.id)

        verdict = verify_outcome(
            "Sent the reminder email to every overdue customer",
            ["A reminder email is sent to every overdue customer"],
        )
        executed = await store.mark_executed(action.id, verdict)

        assert executed is not None
        # The outcome came back as a structured verdict, not a bare string.
        assert isinstance(executed.outcome, OutcomeVerdict)
        assert executed.outcome.status == OutcomeStatus.SOLVED

    @pytest.mark.asyncio
    async def test_structured_verdict_survives_reload(self, store: InstinctStore):
        action = await store.propose(
            pocket_id="pocket-1",
            title="Partial task",
            description="",
            recommendation="",
            trigger=_trigger(),
        )
        await store.approve(action.id)

        verdict = verify_outcome(
            "Generated the report",
            ["A report is generated", "An email is sent"],
        )
        await store.mark_executed(action.id, verdict)

        # Reload from the DB — the verdict must rebuild intact.
        reloaded = await store.get_action(action.id)
        assert reloaded is not None
        assert isinstance(reloaded.outcome, OutcomeVerdict)
        assert reloaded.outcome.status == OutcomeStatus.PARTIAL
        assert reloaded.outcome.met_count == 1
        assert reloaded.outcome.total_count == 2

    @pytest.mark.asyncio
    async def test_legacy_string_outcome_still_works(self, store: InstinctStore):
        """Backward compatibility — a plain free-text outcome string still
        persists and reloads as a string (the pre-#1162 behavior)."""
        action = await store.propose(
            pocket_id="pocket-1",
            title="Legacy outcome task",
            description="",
            recommendation="",
            trigger=_trigger(),
        )
        await store.approve(action.id)
        await store.mark_executed(action.id, "Did the thing successfully")

        reloaded = await store.get_action(action.id)
        assert reloaded is not None
        assert reloaded.outcome == "Did the thing successfully"
        assert isinstance(reloaded.outcome, str)

    @pytest.mark.asyncio
    async def test_none_outcome_still_works(self, store: InstinctStore):
        """mark_executed with no outcome leaves the field None."""
        action = await store.propose(
            pocket_id="pocket-1",
            title="No outcome task",
            description="",
            recommendation="",
            trigger=_trigger(),
        )
        await store.approve(action.id)
        executed = await store.mark_executed(action.id)

        assert executed is not None
        assert executed.outcome is None
