# tests/cloud/other_hand/test_illustrate_tool.py — the agent-facing tool.
#
# Created 2026-08-28. The tool is agent-driven, so the tests that matter are
# about what it does NOT do: it must not put a whole drawing through the model's
# context, and it must not spend past the day's budget.

from __future__ import annotations

import pytest
from pocketpaw_ee.agent.mcp_servers import other_hand as tool_mod


@pytest.fixture
def captured(monkeypatch):
    """Capture the SSE frame instead of pushing it at a live stream."""
    frames: list[tuple[str, dict]] = []
    import pocketpaw_ee.cloud.chat.agent_service as agent_service

    monkeypatch.setattr(agent_service, "push_sse_event", lambda n, d: frames.append((n, d)))
    return frames


@pytest.fixture
def generator_ready(monkeypatch):
    from pocketpaw_ee.cloud.other_hand import illustrate as ill
    from pocketpaw_ee.cloud.studio import fal_edit

    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "test-key")

    async def _ops(subject, box, **_k):
        # Two shapes, enough points that returning them to the model would be
        # the mistake this design exists to avoid.
        return [
            {"t": "path", "pts": [[float(i), float(i)] for i in range(300)]},
            {"t": "path", "pts": [[0.0, 700.0], [700.0, 700.0]]},
        ]

    monkeypatch.setattr(ill, "illustrate_as_ops", _ops)


@pytest.fixture
def signed_up_caller(monkeypatch):
    """A real, non-guest account in the run context.

    Added 2026-09-01 with the guest gate. The tool now refuses a caller it
    cannot identify, because it spends platform money and an unknown caller is
    the case we do not want to pay for. That matches what the budget already
    did (no tenancy resolves to a refusal), so these tests have to say who is
    asking rather than the gate being loosened to let nobody through.
    """
    import pocketpaw_ee.cloud.chat.agent_service as agent_service
    from pocketpaw_ee.cloud.auth import guest_budget

    monkeypatch.setattr(agent_service, "current_user_id", lambda: "u-real")

    async def _not_a_guest(_user_id):
        return None

    monkeypatch.setattr(guest_budget, "load_guest", _not_a_guest)


@pytest.fixture
def budget_open(monkeypatch):
    from pocketpaw_ee.cloud.other_hand import illustration_budget as budget

    async def _spend(*_a, **_k):
        return True, 1, 20

    monkeypatch.setattr(budget, "try_spend", _spend)


class TestTheDrawingDoesNotGoThroughTheModel:
    @pytest.mark.asyncio
    async def test_the_ops_are_pushed_to_the_client_not_returned(
        self, captured, generator_ready, budget_open, signed_up_caller
    ):
        res = await tool_mod._illustrate_handler({"subject": "a honeybee"})
        text = res["content"][0]["text"]
        # The frame carries the geometry...
        assert captured and captured[0][0] == tool_mod.ILLUSTRATION_EVENT
        assert len(captured[0][1]["ops"]) == 2
        # ...and the model's reply does not. This is the whole architecture:
        # a real illustration is thousands of points, and putting that in the
        # context costs more than the picture.
        assert "pts" not in text
        assert len(text) < 400

    @pytest.mark.asyncio
    async def test_the_model_is_told_not_to_redraw_it(
        self, captured, generator_ready, budget_open, signed_up_caller
    ):
        res = await tool_mod._illustrate_handler({"subject": "a honeybee"})
        text = res["content"][0]["text"]
        # Without this the agent helpfully "adds" the picture it was told about,
        # and the page gets it twice.
        assert "do NOT repeat" in text or "do not repeat" in text.lower()


class TestItRefusesRatherThanSpends:
    @pytest.mark.asyncio
    async def test_an_empty_subject_never_reaches_the_generator(self, captured):
        res = await tool_mod._illustrate_handler({"subject": " "})
        assert res["isError"] is True
        assert not captured

    @pytest.mark.asyncio
    async def test_no_key_is_a_clear_refusal_with_no_retry_advice(self, captured, monkeypatch):
        from pocketpaw_ee.cloud.studio import fal_edit

        monkeypatch.setattr(fal_edit, "fal_api_key", lambda: None)
        res = await tool_mod._illustrate_handler({"subject": "a bee"})
        assert res["isError"] is True
        # An agent told only "it failed" will try again, once per turn, forever.
        assert "do not try again" in res["content"][0]["text"]
        assert not captured

    @pytest.mark.asyncio
    async def test_an_exhausted_budget_refuses_before_generating(
        self, captured, generator_ready, monkeypatch, signed_up_caller
    ):
        from pocketpaw_ee.cloud.other_hand import illustration_budget as budget

        async def _spent(*_a, **_k):
            return False, 20, 20

        monkeypatch.setattr(budget, "try_spend", _spent)
        res = await tool_mod._illustrate_handler({"subject": "a bee"})
        assert res["isError"] is True
        assert "20/20" in res["content"][0]["text"]
        assert not captured, "budget was refused but a generation happened anyway"


class TestTheBudgetItself:
    @pytest.mark.asyncio
    async def test_a_zero_cap_disables_the_feature(self, monkeypatch):
        from pocketpaw_ee.cloud.other_hand import illustration_budget as budget

        monkeypatch.setenv("POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS", "0")
        allowed, _spent, cap = await budget.try_spend("ws-1")
        assert allowed is False
        assert cap == 0

    @pytest.mark.asyncio
    async def test_no_workspace_in_context_is_refused(self, monkeypatch):
        from pocketpaw_ee.cloud.other_hand import illustration_budget as budget

        monkeypatch.setenv("POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS", "5")
        # An uncharged generation is exactly the hole the budget closes.
        allowed, _spent, _cap = await budget.try_spend("")
        assert allowed is False

    @pytest.mark.asyncio
    async def test_an_unreachable_database_fails_CLOSED(self, monkeypatch):
        from pocketpaw_ee.cloud.models.other_hand_usage import IllustrationUsage
        from pocketpaw_ee.cloud.other_hand import illustration_budget as budget

        monkeypatch.setenv("POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS", "5")

        # Establish the degraded database EXPLICITLY. This used to rely on
        # "Beanie is not initialised in this suite", which was true only while
        # the other-hand tests ran alone: once the uploads suites (which do
        # initialise Beanie) share the session, the counter became readable,
        # the claim succeeded and this gate silently inverted. A test must
        # create the condition it is named after, not inherit it from whatever
        # else happens to be in the run.
        def _unreachable():
            raise RuntimeError("database unreachable")

        monkeypatch.setattr(IllustrationUsage, "get_pymongo_collection", staticmethod(_unreachable))
        # A degraded database must never become an open tab at the illustrator,
        # so the answer is no — the cost of being wrong this way is a turn that
        # explains in words, and the other way is money.
        allowed, _spent, _cap = await budget.try_spend("ws-1")
        assert allowed is False

    def test_the_collection_accessor_this_module_calls_actually_EXISTS(self):
        """The bug this test exists for shipped, and the suite stayed green.

        ``try_spend`` reaches Mongo through one accessor and wraps everything in
        a fail-closed except. If that accessor is misnamed, the AttributeError
        lands in the except and every claim is refused — the feature reads as
        "switched off", not "broken", and the degraded-database test below
        passes for the wrong reason. It did: the name was ``get_motor_collection``
        (beanie 1.x) against beanie 2.1.0.

        So assert the API directly, where a mismatch is loud.
        """
        from pocketpaw_ee.cloud.models.other_hand_usage import IllustrationUsage

        assert hasattr(IllustrationUsage, "get_pymongo_collection")
        import inspect

        src = inspect.getsource(budget_module_for_accessor_check())
        assert "get_pymongo_collection()" in src
        assert "get_motor_collection()" not in src

    def test_a_nonsense_cap_falls_back_to_the_default(self, monkeypatch):
        from pocketpaw_ee.cloud.other_hand import illustration_budget as budget

        monkeypatch.setenv("POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS", "twenty")
        assert budget.daily_cap() == 20


def budget_module_for_accessor_check():
    """The budget module, imported lazily so the accessor test reads its real
    source rather than a name this file happens to have in scope."""
    from pocketpaw_ee.cloud.other_hand import illustration_budget

    return illustration_budget
