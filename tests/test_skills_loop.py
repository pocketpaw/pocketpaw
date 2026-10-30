# test_skills_loop.py — Self-improving skills loop (PocketPaw side).
# Created: 2026-06-16 (feat/self-improving-skills) — TDD coverage for the forked
#   write-only reviewer that learns procedures into the WORKSPACE agent's soul:
#     - the reviewer whitelist resolves to soul-write ONLY (it physically cannot
#       call any other tool),
#     - rubric-banned items (env failures / "tool broken" / one-off narratives)
#       are NOT written,
#     - agent-created procedures are provenance-tagged (distinguishable from
#       human-authored ones),
#     - writes target the workspace agent's soul (no per-pocket soul),
#     - XP graduation materializes a SKILL.md.
#   LLM extraction + the background spawn are faked — no real model calls.

from __future__ import annotations

import asyncio

import pytest

from pocketpaw.skills_loop.graduation import maybe_graduate_procedure
from pocketpaw.skills_loop.reviewer import SkillsLoopReviewer
from pocketpaw.skills_loop.rubric import is_rubric_banned
from pocketpaw.skills_loop.whitelist import (
    SOUL_WRITE_TOOL_IDS,
    assert_write_only,
    build_reviewer_whitelist,
)
from pocketpaw.skills_loop.writer import AGENT_PROVENANCE_TAG, SoulProcedureWriter


# --------------------------------------------------------------------------- #
# Fakes (no real LLM, no real background spawn, in-memory soul)                #
# --------------------------------------------------------------------------- #
class FakeSoul:
    """Records note()/remember() calls so tests can assert what got written."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def note(self, content, **kwargs):
        record = {"content": content, **kwargs}
        record["_id"] = f"m{len(self.writes)}"
        self.writes.append(record)
        return {"action": "CREATE", "id": record["_id"]}


class FakeOldSoul:
    """Older soul-protocol stand-in: NO note(), only remember().

    Models the interim window before pocketpaw's soul-protocol dep is bumped —
    the writer must fall back to remember() and still tag provenance.
    """

    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def remember(self, content, **kwargs):
        record = {"content": content, **kwargs}
        record["_id"] = f"m{len(self.writes)}"
        self.writes.append(record)
        return record["_id"]


class FakeExtractor:
    """Stand-in for the LLM that reads a transcript and proposes procedures."""

    def __init__(self, procedures: list[str]) -> None:
        self._procedures = procedures

    async def extract(self, transcript: str) -> list[str]:  # noqa: ARG002
        return list(self._procedures)


# --------------------------------------------------------------------------- #
# (1) Whitelist — soul-write ONLY                                             #
# --------------------------------------------------------------------------- #
class TestWhitelist:
    def test_whitelist_is_soul_write_only(self):
        wl = build_reviewer_whitelist()
        assert wl == SOUL_WRITE_TOOL_IDS
        # The core safety assertion: nothing but soul-write may be present.
        assert wl == frozenset({"soul_remember"})

    def test_assert_write_only_passes_for_soul_write(self):
        # Must not raise.
        assert_write_only(build_reviewer_whitelist())

    @pytest.mark.parametrize(
        "forbidden",
        [
            frozenset({"soul_remember", "Bash"}),
            frozenset({"soul_remember", "Write"}),
            frozenset({"soul_remember", "Read"}),
            frozenset({"soul_remember", "Edit"}),
            frozenset({"soul_remember", "mcp__pocketpaw_pocket__add_widget"}),
            frozenset({"Bash"}),
        ],
    )
    def test_assert_write_only_rejects_any_other_tool(self, forbidden):
        with pytest.raises(ValueError, match="write-only"):
            assert_write_only(forbidden)


# --------------------------------------------------------------------------- #
# (2) Rubric — banned items are not captured                                  #
# --------------------------------------------------------------------------- #
class TestRubric:
    @pytest.mark.parametrize(
        "banned",
        [
            "The network was down so the deploy failed — wait and retry later.",
            "Tool Bash is broken and always errors, do not use it.",
            "The Read tool is broken, avoid calling it.",
            "On 2026-06-14 we fixed the login bug for customer Acme by editing line 42.",
            "Permission denied when running git push, the environment is misconfigured.",
        ],
    )
    def test_banned_items_are_rejected(self, banned):
        is_banned, reason = is_rubric_banned(banned)
        assert is_banned is True
        assert reason

    @pytest.mark.parametrize(
        "good",
        [
            "To regenerate C4 diagrams, run make c4-pp from the docs directory.",
            "When a pytest-asyncio test hangs, check for an unawaited coroutine.",
            "Prefer uv run ruff format over manual formatting before committing.",
        ],
    )
    def test_legitimate_procedures_pass(self, good):
        is_banned, _ = is_rubric_banned(good)
        assert is_banned is False


# --------------------------------------------------------------------------- #
# (3)+(4) Writer — provenance tagging + workspace soul                        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestWriter:
    async def test_write_tags_agent_provenance(self):
        soul = FakeSoul()
        writer = SoulProcedureWriter(soul)
        result = await writer.write("To run e2e tests use uv run pytest tests e2e")
        assert result["written"] is True
        assert len(soul.writes) == 1
        rec = soul.writes[0]
        # Provenance is carried on the entities tag (works on every
        # soul-protocol version) — distinguishable from human-authored.
        assert AGENT_PROVENANCE_TAG in rec["entities"]
        # And as PROCEDURAL memory.
        assert str(rec["type"]).endswith("procedural")

    async def test_write_rejects_rubric_banned(self):
        soul = FakeSoul()
        writer = SoulProcedureWriter(soul)
        result = await writer.write("Tool Bash is broken, never use it.")
        assert result["written"] is False
        assert result["reason"]
        assert soul.writes == []

    async def test_write_falls_back_to_remember_on_old_soul(self, monkeypatch):
        # Interim window: soul-protocol has no note() and no MemoryProvenance.
        # The writer must fall back to remember(), drop the unsupported
        # provenance kwarg, and STILL land the agent provenance tag on entities.
        import pocketpaw.skills_loop.writer as writer_mod

        monkeypatch.setattr(writer_mod, "_AGENT_PROVENANCE", None)
        soul = FakeOldSoul()
        writer = SoulProcedureWriter(soul)
        result = await writer.write("To run e2e tests use uv run pytest tests e2e")
        assert result["written"] is True
        assert len(soul.writes) == 1
        rec = soul.writes[0]
        # Fallback path: provenance tag still present on the entities list.
        assert AGENT_PROVENANCE_TAG in rec["entities"]
        # The unsupported native kwarg must NOT be forwarded to remember().
        assert "provenance" not in rec
        assert str(rec["type"]).endswith("procedural")


# --------------------------------------------------------------------------- #
# (3)+(4) Reviewer end-to-end — workspace soul, provenance, rubric            #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestReviewer:
    async def test_reviewer_writes_only_legitimate_procedures(self):
        soul = FakeSoul()
        extractor = FakeExtractor(
            [
                "To regenerate C4 run make c4-pp from docs",  # good
                "Tool Bash is broken and always fails",  # banned
                "The network was down so the build failed",  # banned
            ]
        )
        reviewer = SkillsLoopReviewer(soul=soul, extractor=extractor)
        report = await reviewer.review_session("...transcript...")

        # Only the legitimate procedure was written.
        assert len(soul.writes) == 1
        assert "c4-pp" in soul.writes[0]["content"]
        assert report["written"] == 1
        assert report["rejected"] == 2

    async def test_reviewer_targets_workspace_soul(self):
        # The reviewer must write to the soul it is given (the workspace
        # agent's soul) — it never resolves a per-pocket soul itself.
        workspace_soul = FakeSoul()
        extractor = FakeExtractor(["To deploy run make ship from the repo root"])
        reviewer = SkillsLoopReviewer(soul=workspace_soul, extractor=extractor)
        await reviewer.review_session("...transcript...")
        assert len(workspace_soul.writes) == 1
        assert AGENT_PROVENANCE_TAG in workspace_soul.writes[0]["entities"]

    async def test_reviewer_uses_write_only_whitelist(self):
        # The reviewer exposes a soul-write-only whitelist — the CONTRACT the
        # SDK caller must forward as allow_sdk_tools. (This object does not spawn
        # the SDK run itself; the runtime hookup is a follow-up PR.)
        reviewer = SkillsLoopReviewer(soul=FakeSoul(), extractor=FakeExtractor([]))
        assert_write_only(reviewer.tool_whitelist)
        assert reviewer.tool_whitelist == frozenset({"soul_remember"})


# --------------------------------------------------------------------------- #
# (5) Graduation — XP threshold materializes a SKILL.md                       #
# --------------------------------------------------------------------------- #
class FakeSkillRegistry:
    """Minimal SkillRegistry stand-in for graduation tests."""

    def __init__(self, *, graduate: bool) -> None:
        self._graduate = graduate
        self.granted: list[tuple[str, int]] = []

    def grant_xp_for_procedure_use(self, skill_id: str, amount: int = 10) -> bool:
        self.granted.append((skill_id, amount))
        return self._graduate


class TestGraduation:
    def test_graduation_materializes_skill_md(self, tmp_path):
        registry = FakeSkillRegistry(graduate=True)
        path = maybe_graduate_procedure(
            registry,
            procedure_id="abc123",
            procedure_text="To regenerate C4 run make c4-pp from docs",
            skills_root=tmp_path,
        )
        assert path is not None
        assert path.exists()
        body = path.read_text()
        assert "make c4-pp" in body
        assert registry.granted == [("proc:abc123", 10)]

    def test_no_graduation_below_threshold(self, tmp_path):
        registry = FakeSkillRegistry(graduate=False)
        path = maybe_graduate_procedure(
            registry,
            procedure_id="abc123",
            procedure_text="To regenerate C4 run make c4-pp from docs",
            skills_root=tmp_path,
        )
        assert path is None
        # No SKILL.md written.
        assert list(tmp_path.glob("**/SKILL.md")) == []

    def test_pathological_ids_do_not_collide(self, tmp_path):
        # Two distinct all-punctuation ids must not collapse to the same
        # "learned-procedure" directory (and clobber each other's SKILL.md).
        p1 = maybe_graduate_procedure(
            FakeSkillRegistry(graduate=True),
            procedure_id="@@@",
            procedure_text="First procedure body alpha",
            skills_root=tmp_path,
        )
        p2 = maybe_graduate_procedure(
            FakeSkillRegistry(graduate=True),
            procedure_id="###",
            procedure_text="Second procedure body beta",
            skills_root=tmp_path,
        )
        assert p1 is not None and p2 is not None
        # The two SKILL.md paths must be distinct (no clobber).
        assert p1 != p2
        assert "alpha" in p1.read_text()
        assert "beta" in p2.read_text()


# --------------------------------------------------------------------------- #
# Trigger — background fire-and-return at session-finalize                     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestTrigger:
    async def test_finalize_launches_background_review(self):
        from pocketpaw.skills_loop.trigger import SkillsLoopTrigger

        soul = FakeSoul()
        trigger = SkillsLoopTrigger()
        launched = trigger.on_session_finalize(
            "sess-1",
            soul=soul,
            extractor=FakeExtractor(["To deploy run make ship from the repo root"]),
            transcript="...",
        )
        assert launched is True
        # Let the background task run to completion.
        await asyncio.sleep(0.05)
        assert len(soul.writes) == 1
        assert not trigger.is_running("sess-1")

    async def test_double_dispatch_is_guarded(self):
        from pocketpaw.skills_loop.trigger import SkillsLoopTrigger

        soul = FakeSoul()
        trigger = SkillsLoopTrigger()
        slow = SlowExtractor(["To deploy run make ship from the repo root"])
        first = trigger.on_session_finalize("sess-1", soul=soul, extractor=slow, transcript="...")
        second = trigger.on_session_finalize("sess-1", soul=soul, extractor=slow, transcript="...")
        assert first is True
        assert second is False  # already in flight
        slow.release()
        await asyncio.sleep(0.05)
        assert len(soul.writes) == 1


class SlowExtractor:
    """Extractor that blocks until released — exercises the in-flight guard."""

    def __init__(self, procedures: list[str]) -> None:
        self._procedures = procedures
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def extract(self, transcript: str) -> list[str]:  # noqa: ARG002
        await self._gate.wait()
        return list(self._procedures)
