# tests/cloud/test_ee_correction.py — Tests for the Correction Loop (Move 1 PR-A).
# Created: 2026-04-12 — Unit coverage for compute_patches + summarize_correction,
# store-level record_correction + query helpers, and the /approve endpoint behavior
# across unedited, edited, and edge-case bodies.

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.instinct.correction import (
    Correction,
    CorrectionPatch,
    compute_patches,
    summarize_correction,
)
from ee.instinct.models import (
    Action,
    ActionCategory,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
)
from ee.instinct.router import router
from ee.instinct.store import InstinctStore


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="claude", reason="unit test")


def _action(**overrides) -> Action:
    defaults: dict = {
        "pocket_id": "pocket-1",
        "title": "Send renewal outreach",
        "description": "Three accounts up for renewal this month",
        "recommendation": "Draft a friendly nudge email",
        "trigger": _trigger(),
        "category": ActionCategory.WORKFLOW,
        "priority": ActionPriority.MEDIUM,
        "parameters": {"tone": "formal", "discount_pct": 20},
    }
    defaults.update(overrides)
    return Action(**defaults)


# ---------------------------------------------------------------------------
# compute_patches — field-level diff logic
# ---------------------------------------------------------------------------


class TestComputePatches:
    def test_identical_actions_produce_no_patches(self) -> None:
        before = _action()
        after = before.model_copy()
        assert compute_patches(before, after) == []

    def test_scalar_field_change_is_captured(self) -> None:
        before = _action(title="Send renewal outreach")
        after = before.model_copy(update={"title": "Quick renewal nudge"})
        patches = compute_patches(before, after)
        assert len(patches) == 1
        assert patches[0].path == "title"
        assert patches[0].before == "Send renewal outreach"
        assert patches[0].after == "Quick renewal nudge"

    def test_enum_fields_normalize_to_string_values(self) -> None:
        before = _action(priority=ActionPriority.MEDIUM)
        after = before.model_copy(update={"priority": ActionPriority.HIGH})
        patches = compute_patches(before, after)
        assert len(patches) == 1
        assert patches[0].path == "priority"
        assert patches[0].before == "medium"
        assert patches[0].after == "high"

    def test_parameters_diff_uses_dotted_path(self) -> None:
        before = _action(parameters={"tone": "formal", "discount_pct": 20})
        after = before.model_copy(
            update={"parameters": {"tone": "casual", "discount_pct": 15}},
        )
        paths = {p.path for p in compute_patches(before, after)}
        assert paths == {"parameters.tone", "parameters.discount_pct"}

    def test_parameter_added_and_removed_both_captured(self) -> None:
        before = _action(parameters={"tone": "formal"})
        after = before.model_copy(update={"parameters": {"discount_pct": 15}})
        patches = compute_patches(before, after)
        paths = {p.path for p in patches}
        assert paths == {"parameters.tone", "parameters.discount_pct"}
        by_path = {p.path: p for p in patches}
        assert by_path["parameters.tone"].after is None
        assert by_path["parameters.discount_pct"].before is None

    def test_context_field_is_ignored(self) -> None:
        """Context carries reasoning metadata, not action content — skip it."""
        before = _action()
        after = before.model_copy(update={"context": before.context.model_copy()})
        # Even if context were different, compute_patches should ignore it.
        assert compute_patches(before, after) == []

    def test_multiple_unrelated_fields_return_multiple_patches(self) -> None:
        before = _action()
        after = before.model_copy(
            update={
                "title": "New title",
                "description": "New desc",
                "priority": ActionPriority.HIGH,
                "parameters": {"tone": "casual", "discount_pct": 20},
            },
        )
        patches = compute_patches(before, after)
        paths = {p.path for p in patches}
        assert paths == {"title", "description", "priority", "parameters.tone"}


# ---------------------------------------------------------------------------
# summarize_correction — deterministic recall-key formatting
# ---------------------------------------------------------------------------


class TestSummarizeCorrection:
    def test_zero_patches_returns_approved_without_edits(self) -> None:
        summary = summarize_correction(_action(), [])
        assert "approved without edits" in summary
        assert "Send renewal outreach" in summary

    def test_summary_names_each_patched_field_up_to_five(self) -> None:
        patches = [CorrectionPatch(path=f"parameters.f{i}", before=1, after=2) for i in range(5)]
        summary = summarize_correction(_action(), patches)
        for i in range(5):
            assert f"parameters.f{i}" in summary

    def test_more_than_five_patches_appends_overflow_counter(self) -> None:
        patches = [CorrectionPatch(path=f"parameters.f{i}", before=1, after=2) for i in range(8)]
        summary = summarize_correction(_action(), patches)
        assert "(+3 more)" in summary


# ---------------------------------------------------------------------------
# InstinctStore — corrections CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "correction_test.db")


@pytest.fixture
def correction_for(store: InstinctStore):
    """Factory: build a Correction wired to a concrete pocket/action pair."""

    def _make(
        *,
        pocket_id: str = "pocket-1",
        action_id: str = "act-123",
        actor: str = "user:priya",
        patches: list[CorrectionPatch] | None = None,
        title: str = "Send renewal outreach",
    ) -> Correction:
        return Correction(
            action_id=action_id,
            pocket_id=pocket_id,
            actor=actor,
            patches=patches or [CorrectionPatch(path="title", before="Old", after="New")],
            context_summary="edited the greeting tone",
            action_title=title,
        )

    return _make


class TestCorrectionStore:
    @pytest.mark.asyncio
    async def test_record_correction_persists_the_row(
        self, store: InstinctStore, correction_for
    ) -> None:
        correction = correction_for()
        await store.record_correction(correction)

        saved = await store.get_corrections_for_action("act-123")
        assert len(saved) == 1
        assert saved[0].id == correction.id
        assert saved[0].patches[0].path == "title"

    @pytest.mark.asyncio
    async def test_record_correction_writes_audit_entry(
        self, store: InstinctStore, correction_for
    ) -> None:
        correction = correction_for()
        await store.record_correction(correction)

        audit = await store.query_audit(pocket_id="pocket-1")
        events = [e.event for e in audit]
        assert "correction_captured" in events
        captured = next(e for e in audit if e.event == "correction_captured")
        assert captured.context["correction_id"] == correction.id
        assert captured.context["patch_count"] == 1
        assert captured.context["paths"] == ["title"]

    @pytest.mark.asyncio
    async def test_get_corrections_for_pocket_filters_by_pocket(
        self, store: InstinctStore, correction_for
    ) -> None:
        await store.record_correction(correction_for(pocket_id="pocket-1"))
        await store.record_correction(correction_for(pocket_id="pocket-2"))

        only = await store.get_corrections_for_pocket("pocket-1")
        assert len(only) == 1
        assert only[0].pocket_id == "pocket-1"

    @pytest.mark.asyncio
    async def test_get_corrections_orders_newest_first(
        self, store: InstinctStore, correction_for
    ) -> None:
        first = correction_for(action_id="act-a")
        second = correction_for(action_id="act-b")
        await store.record_correction(first)
        await store.record_correction(second)

        corrections = await store.get_corrections_for_pocket("pocket-1")
        assert len(corrections) == 2
        assert corrections[0].action_id == "act-b"
        assert corrections[1].action_id == "act-a"

    @pytest.mark.asyncio
    async def test_count_corrections_by_path(self, store: InstinctStore, correction_for) -> None:
        await store.record_correction(
            correction_for(
                action_id="act-1",
                patches=[CorrectionPatch(path="title", before="A", after="B")],
            ),
        )
        await store.record_correction(
            correction_for(
                action_id="act-2",
                patches=[CorrectionPatch(path="title", before="C", after="D")],
            ),
        )
        await store.record_correction(
            correction_for(
                action_id="act-3",
                patches=[
                    CorrectionPatch(path="parameters.tone", before="formal", after="casual"),
                ],
            ),
        )

        assert await store.count_corrections_by_path("pocket-1", "title") == 2
        assert await store.count_corrections_by_path("pocket-1", "parameters.tone") == 1
        assert await store.count_corrections_by_path("pocket-1", "description") == 0


# ---------------------------------------------------------------------------
# /approve endpoint — integration
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_store(tmp_path: Path):
    app = FastAPI()
    app.include_router(router)
    store = InstinctStore(tmp_path / "router_correction.db")
    with patch("ee.instinct.router._store", return_value=store):
        yield app, store


@pytest.fixture
def client(app_with_store):
    app, _ = app_with_store
    return TestClient(app)


class TestApproveEndpoint:
    @pytest.mark.asyncio
    async def test_approve_unchanged_returns_no_correction(
        self, app_with_store, client: TestClient
    ) -> None:
        _, store = app_with_store
        action = await store.propose(
            pocket_id="pocket-1",
            title="Send renewal outreach",
            description="",
            recommendation="Draft nudge",
            trigger=_trigger(),
        )

        res = client.post(f"/instinct/actions/{action.id}/approve")
        assert res.status_code == 200
        body = res.json()
        assert body["action"]["status"] == "approved"
        assert body["correction"] is None

    @pytest.mark.asyncio
    async def test_approve_with_edits_captures_correction(
        self, app_with_store, client: TestClient
    ) -> None:
        _, store = app_with_store
        action = await store.propose(
            pocket_id="pocket-1",
            title="Send renewal outreach",
            description="Three accounts up for renewal",
            recommendation="Formal email",
            trigger=_trigger(),
            priority=ActionPriority.MEDIUM,
            parameters={"tone": "formal", "discount_pct": 20},
        )

        res = client.post(
            f"/instinct/actions/{action.id}/approve",
            json={
                "approver": "user:priya",
                "title": "Quick renewal nudge",
                "priority": "high",
                "parameters": {"tone": "casual", "discount_pct": 15},
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["action"]["status"] == "approved"
        assert body["correction"] is not None
        paths = {p["path"] for p in body["correction"]["patches"]}
        assert paths == {"title", "priority", "parameters.tone", "parameters.discount_pct"}

        saved = await store.get_action(action.id)
        assert saved.title == "Quick renewal nudge"
        assert saved.priority == ActionPriority.HIGH
        assert saved.parameters["tone"] == "casual"
        assert saved.status == ActionStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_with_equal_body_treats_as_unchanged(
        self, app_with_store, client: TestClient
    ) -> None:
        """Approve body can carry identical fields — no correction should be stored."""
        _, store = app_with_store
        action = await store.propose(
            pocket_id="pocket-1",
            title="Send renewal outreach",
            description="desc",
            recommendation="rec",
            trigger=_trigger(),
        )

        res = client.post(
            f"/instinct/actions/{action.id}/approve",
            json={"approver": "user:priya", "title": action.title},
        )
        assert res.status_code == 200
        assert res.json()["correction"] is None

        corrections = await store.get_corrections_for_action(action.id)
        assert corrections == []

    def test_approve_unknown_action_returns_404(self, client: TestClient) -> None:
        res = client.post("/instinct/actions/does-not-exist/approve")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# /corrections endpoint
# ---------------------------------------------------------------------------


class TestCorrectionsEndpoint:
    @pytest.mark.asyncio
    async def test_list_by_pocket_returns_corrections(
        self, app_with_store, client: TestClient
    ) -> None:
        _, store = app_with_store
        action = await store.propose(
            pocket_id="pocket-1",
            title="Old",
            description="",
            recommendation="",
            trigger=_trigger(),
        )
        client.post(
            f"/instinct/actions/{action.id}/approve",
            json={"approver": "user:priya", "title": "New"},
        )

        res = client.get("/instinct/corrections?pocket_id=pocket-1")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["corrections"][0]["patches"][0]["path"] == "title"

    def test_list_without_filters_returns_400(self, client: TestClient) -> None:
        res = client.get("/instinct/corrections")
        assert res.status_code == 400
