# tests/cloud/pockets/test_short_id_tool_wiring.py — the loop closed.
# Created: 2026-08-04 (feat/prompt-entity-suffix).
#
# WHAT WAS UNCOVERED. The shortened id has three moving parts and only two were
# tested:
#
#   1. the renderer shortens        -> tests/test_prompt_entity_line.py
#   2. the resolver expands         -> tests/cloud/pockets/test_id_resolve.py
#   3. THE TOOLS CALL THE RESOLVER  -> nothing
#
# Part 3 is the wiring in ``_agent_load_doc`` and ``agent_update_widget``, and
# the pre-existing ``agent_update_widget`` tests all pass WHOLE ids, so they
# would keep passing with the resolve call deleted outright. A perfect renderer
# and a perfect resolver that are never joined up ship an agent that reads
# ``id=…3f9a1c07`` off its own prompt and gets "widget not found".
#
# So these tests start from the bytes the agent is actually shown — the real
# preamble, rendered by the real handler — pull the id out of that string the
# way a model would, and hand THAT to the real mutation entry point. If any
# link in the chain disagrees with any other, this fails.
#
# The only thing between here and a live server is whether the model chooses to
# use what it was shown, which no test can answer.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted.

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import AddWidgetRequest, CreatePocketRequest
from pocketpaw_ee.cloud.surface.handlers._helpers import format_widget_line

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-shortid"
USER = "user-shortid"


class _AttrView:
    """Attribute access over a widget wire dict (``format_widget_line`` is duck-typed)."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, item: str):
        if item == "id":
            return self._data.get("id") or self._data.get("_id")
        return self._data.get(item)


def _as_the_agent_reads_it(row: str) -> str:
    """Pull the id out of a rendered row the way a model would.

    Deliberately naive — split on ``id=``, stop at the delimiter — because that
    is what a model does. Anything cleverer here would be testing my parser
    rather than the contract.
    """
    return row.split("id=")[1].split(",")[0].rstrip(")")


def _stream_identity(workspace: str, user: str) -> ExitStack:
    """Fake the per-stream ContextVars ``_agent_load_doc`` reads.

    Agent pocket mutations refuse to run outside a cloud SSE chat stream, so
    without these every call under test returns "no active workspace/user"
    and the tests would pass for the wrong reason.
    """
    stack = ExitStack()
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=workspace),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value=user),
        )
    )
    return stack


async def _seed(name: str, widgets: tuple[str, ...] = ()) -> str:
    created = await pockets_service.create(
        WORKSPACE, USER, CreatePocketRequest(name=name, type="custom")
    )
    pocket_id = str(created["_id"])
    for widget_name in widgets:
        await pockets_service.add_widget(
            pocket_id, USER, AddWidgetRequest(name=widget_name, type="native")
        )
    return pocket_id


class TestTheAgentCanActOnWhatItWasShown:
    async def test_the_widget_id_from_the_prompt_updates_that_widget(self) -> None:
        """The whole point, in one test.

        Renders the real row, reads the id off it the way a model would, and
        calls the real ``agent_update_widget`` with that string. Two widgets, so
        landing on the wrong one is a possible failure rather than a certainty.

        THE MUTATION THAT BREAKS THIS: delete the ``resolve_id`` call from
        ``agent_update_widget``. Run: "widget …bd4f2a1c not found in pocket" —
        the exact failure an agent would hit reading its own prompt.
        (Applied 2026-08-04.)
        """
        pocket_id = await _seed("Metrics", ("Revenue", "Churn"))

        pocket = await pockets_service.get(pocket_id, USER)
        views = [_AttrView(w) for w in pocket["widgets"]]
        target = next(v for v in views if v.name == "Churn")
        shown = _as_the_agent_reads_it(format_widget_line(target))

        assert shown != target.id, "the row should carry a SHORTENED id, not the whole one"

        with _stream_identity(WORKSPACE, USER):
            view, err = await pockets_service.agent_update_widget(
                pocket_id, shown, {"name": "Churn risk"}
            )

        assert err is None, err
        renamed = {w["name"] for w in view["widgets"]}
        assert "Churn risk" in renamed
        assert "Revenue" in renamed, "the resolve landed on the wrong widget"

    async def test_the_pocket_id_from_the_prompt_loads_that_pocket(self) -> None:
        """Same chain, one level up — ``_agent_load_doc`` resolves a short pocket id.

        The pocket resolve is the one that has to hit the database, and the one
        that is scoped by workspace, so it is the more fragile of the two.

        THE MUTATION THAT BREAKS THIS: delete the ``_is_whole_object_id`` branch
        from ``_agent_load_doc``. Run: PydanticObjectId rejected the 8-char id
        and it returned "could not load pocket". (Applied 2026-08-04.)
        """
        from pocketpaw.prompt.entity import short_id

        pocket_id = await _seed("Launch Tracker", ("Timeline",))
        await _seed("Roadmap")

        with _stream_identity(WORKSPACE, USER):
            doc, err = await pockets_service._agent_load_doc(short_id(pocket_id))

        assert err is None, err
        assert doc is not None
        assert str(doc.id) == pocket_id
        assert doc.name == "Launch Tracker"

    async def test_a_whole_id_still_works_through_the_same_path(self) -> None:
        """Every caller that existed before this change sends 24 chars.

        THE MUTATION THAT BREAKS THIS: delete the ``resolve_id`` call from
        ``agent_update_widget`` — no, that one is caught above. This test is the
        no-regression half and is deliberately NOT the guard for the fast path;
        see ``test_a_whole_id_does_not_scan_the_collection`` for that, and read
        its docstring before assuming ``_is_whole_object_id`` protects
        correctness. It does not.
        """
        pocket_id = await _seed("Direct", ("Tile",))

        pocket = await pockets_service.get(pocket_id, USER)
        widget_id = pocket["widgets"][0].get("id") or pocket["widgets"][0].get("_id")

        with _stream_identity(WORKSPACE, USER):
            view, err = await pockets_service.agent_update_widget(
                pocket_id, widget_id, {"name": "Renamed"}
            )

        assert err is None, err
        assert view["widgets"][0]["name"] == "Renamed"

    async def test_a_whole_id_does_not_scan_the_collection(self) -> None:
        """``_is_whole_object_id`` is a PERFORMANCE guard, not a correctness one.

        I originally documented it as correctness — "force everything down the
        resolve path and a whole id stops working" — and ``scripts/mutate.py``
        proved that false: ``resolve_id`` checks for an exact match before it
        applies the tail rules, so a whole id resolves either way. The mutation
        escaped, which is precisely what the harness is for.

        What the guard actually buys is one round trip. Without it, EVERY agent
        pocket mutation lists every pocket id in the workspace before doing
        anything, on a path that already knows it was handed a whole id. So the
        honest assertion is about the query, not the result.

        Spies on ``_resolve_pocket_id_tail`` rather than on the collection:
        beanie's own ``Document.get`` reaches for ``get_pymongo_collection``
        internally, so counting THAT proves nothing about this code path. The
        first version of this test did exactly that and failed for that reason.

        THE MUTATION THAT BREAKS THIS: make ``_is_whole_object_id`` return False
        always. Run: the tail resolver ran for a whole id and this failed.
        (Applied 2026-08-04.)
        """
        pocket_id = await _seed("Direct", ("Tile",))
        real = pockets_service._resolve_pocket_id_tail
        calls: list[str] = []

        async def _spy(given: str, workspace_id: str):
            calls.append(given)
            return await real(given, workspace_id)

        with (
            _stream_identity(WORKSPACE, USER),
            patch.object(pockets_service, "_resolve_pocket_id_tail", _spy),
        ):
            doc, err = await pockets_service._agent_load_doc(pocket_id)

        assert err is None, err
        assert doc is not None
        assert not calls, (
            "a whole id triggered a workspace-wide pocket id scan — the "
            f"_is_whole_object_id fast path is not being taken (resolved {calls})"
        )


class TestTheFailuresAreLoud:
    async def test_an_unknown_short_id_reports_not_found(self) -> None:
        """A miss must not silently mutate a neighbouring widget.

        THE MUTATION THAT BREAKS THIS: catch KeyError in ``agent_update_widget``
        and fall through to the first widget. Run: err was None and the wrong
        widget was renamed.
        """
        pocket_id = await _seed("Metrics", ("Revenue",))

        with _stream_identity(WORKSPACE, USER):
            view, err = await pockets_service.agent_update_widget(
                pocket_id, "ffffffff", {"name": "Nope"}
            )

        assert view is None
        assert err is not None and "not found" in err

    async def test_a_short_pocket_id_cannot_reach_another_workspace(self) -> None:
        """The tenancy boundary on the resolve, asserted rather than assumed.

        The resolve queries by tail, so without the workspace filter a tail from
        one tenant could land on another's pocket. That would be a far worse bug
        than the prompt bloat the shortening was introduced to fix.

        THE MUTATION THAT BREAKS THIS: drop ``{"workspace": workspace_id}`` from
        the query in ``_resolve_pocket_id_tail``. Run: the other workspace's
        pocket resolved, ``_agent_load_doc`` then rejected it on its own
        workspace check — so the id LEAKED as far as the resolver and only a
        second guard caught it. Both layers matter; this pins the first.
        (Applied 2026-08-04.)
        """
        from pocketpaw.prompt.entity import short_id

        other = await pockets_service.create(
            "ws-somebody-else", "user-else", CreatePocketRequest(name="Theirs", type="custom")
        )
        other_id = str(other["_id"])

        with _stream_identity(WORKSPACE, USER):
            resolved, err = await pockets_service._resolve_pocket_id_tail(
                short_id(other_id), WORKSPACE
            )

        assert err is not None, f"a tail resolved across tenants to {resolved}"
        assert "not found" in err
