# tests/cloud/surface/test_handler_row_injectivity.py — the property, on the
# handlers themselves.
# Created: 2026-08-03 (feat/prompt-entity-suffix), from review.
#
# WHAT THIS CLOSES. The first pass at the entity-id work tested two things:
# that ``entity_line`` is injective on id, and — by AST scan — that handlers
# call it. The property anybody actually cares about is that a HANDLER'S OUTPUT
# is injective on id, and that was INFERRED from those two rather than asserted.
# The inference is not airtight: a handler can call the renderer and still hand
# it the wrong field, or the same field for both entities, and both existing
# checks pass.
#
# It is not a hypothetical gap. ``pocket_to_wire_dict`` emits ``_id``, not
# ``id``, so ``p.get("id")`` would have returned None for every pocket, every
# row would have rendered ``id=?``, the AST scan would have been satisfied, and
# the renderer's own tests would still have been green. This file is what
# catches that.
#
# So these tests drive the REAL handler against REAL seeded documents through
# the REAL service, with two entities that differ only in id, and assert the
# rendered preambles differ. No stub sits between the assertion and the bug —
# deliberately, because a stubbed ``list_pockets`` returning hand-written dicts
# would encode MY belief about the wire shape, which is the belief under test.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import AddWidgetRequest, CreatePocketRequest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import pockets_list as pockets_list_handler

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-injectivity"
USER = "user-injectivity"


async def _seed_pocket(name: str) -> str:
    """Create a pocket through the real service and return its id."""
    created = await pockets_service.create(
        WORKSPACE, USER, CreatePocketRequest(name=name, type="custom")
    )
    return str(created["_id"])


class TestPocketsListHandler:
    async def test_two_pockets_with_one_name_render_distinguishable_rows(self) -> None:
        """The production scenario, end to end.

        One workspace, two pockets called Sales. Before the id was rendered
        these produced byte-identical rows and "open the Sales pocket" was a
        coin flip that failed silently.

        THE MUTATION THAT BREAKS THIS: in ``pockets_list.py``, pass
        ``p.get("id")`` instead of ``p.get("_id")``. Run: the wire dict has no
        ``id`` key, both rows rendered ``id=?``, and the rows-differ assertion
        failed — while the AST contract test and every renderer test stayed
        green. That is the exact gap this file was added for.
        (Applied 2026-08-03.)
        """
        first = await _seed_pocket("Sales")
        second = await _seed_pocket("Sales")
        assert first != second

        preamble = (await pockets_list_handler.build_preamble(WORKSPACE, USER, SurfaceMeta())).text

        rows = [ln for ln in preamble.splitlines() if ln.startswith("- ")]
        assert len(rows) == 2, f"expected two pocket rows, got {rows}"
        assert rows[0] != rows[1], f"two pockets named Sales are indistinguishable: {rows}"

    async def test_the_rendered_id_actually_addresses_the_pocket(self) -> None:
        """A row that differs but cannot be acted on has missed the point.

        Injectivity alone is satisfied by rendering any per-entity nonsense. The
        id has to be the thing a tool resolves, so this feeds what the prompt
        SHOWED to the resolver the tools use and checks it lands on the right
        pocket.

        THE MUTATION THAT BREAKS THIS: render ``p.get("name")`` as the id. Run:
        the rows still differed (the names differ here), the resolve returned
        the wrong pocket, and the assertion failed. (Applied 2026-08-03.)
        """
        from pocketpaw_ee.cloud.pockets.id_resolve import resolve_id

        target = await _seed_pocket("Launch Tracker")
        await _seed_pocket("Roadmap")

        preamble = (await pockets_list_handler.build_preamble(WORKSPACE, USER, SurfaceMeta())).text
        row = next(ln for ln in preamble.splitlines() if "Launch Tracker" in ln)
        shown = row.split("id=")[1].split(",")[0].rstrip(")")

        candidates = await pockets_service.list_pockets(WORKSPACE, USER)
        assert resolve_id(shown, candidates) == target

    async def test_a_pocket_row_is_not_missing_its_id(self) -> None:
        """Guards the ``id=?`` failure directly, so the diagnosis is readable.

        The injectivity test above catches this too, but reports it as "the rows
        are identical", which sends a reader looking at the wrong thing.

        THE MUTATION THAT BREAKS THIS: pass ``None`` as the pocket id. Run: the
        row carried ``id=?`` and this failed. (Applied 2026-08-03.)
        """
        await _seed_pocket("Solo")

        preamble = (await pockets_list_handler.build_preamble(WORKSPACE, USER, SurfaceMeta())).text
        row = next(ln for ln in preamble.splitlines() if ln.startswith("- "))

        assert "id=?" not in row, f"the handler could not supply an id: {row}"


class TestWidgetRows:
    async def test_two_widgets_with_one_name_render_distinguishable_rows(self) -> None:
        """Widgets are the case with a REQUIRED id on the tool that edits them.

        ``update_widget`` declares ``"required": [..., "widget_id", ...]``, and
        two tiles called Revenue on one pocket are ordinary — a chart and its
        summary card routinely share a name.

        THE MUTATION THAT BREAKS THIS: in ``_helpers.format_widget_line``, pass
        ``getattr(widget, "widget_id", None)`` — a field that does not exist.
        Run: both rows rendered ``id=?`` and the assertion failed.
        (Applied 2026-08-03.)
        """
        from pocketpaw_ee.cloud.surface.handlers._helpers import format_widget_line

        pocket_id = await _seed_pocket("Metrics")
        for _ in range(2):
            await pockets_service.add_widget(
                pocket_id, USER, AddWidgetRequest(name="Revenue", type="native")
            )

        pocket = await pockets_service.get(pocket_id, USER)
        widgets = pocket["widgets"]
        assert len(widgets) == 2

        rows = [format_widget_line(_AttrView(w)) for w in widgets]
        assert rows[0] != rows[1], f"two widgets named Revenue are indistinguishable: {rows}"

    async def test_the_rendered_widget_id_resolves_to_that_widget(self) -> None:
        """Same "differs is not enough" argument as the pocket case.

        THE MUTATION THAT BREAKS THIS: render the widget's index instead of its
        id. Run: the rows differed, the resolve raised KeyError, and this
        failed. (Applied 2026-08-03.)
        """
        from pocketpaw_ee.cloud.pockets.id_resolve import resolve_id
        from pocketpaw_ee.cloud.surface.handlers._helpers import format_widget_line

        pocket_id = await _seed_pocket("Metrics")
        for name in ("Revenue", "Churn"):
            await pockets_service.add_widget(
                pocket_id, USER, AddWidgetRequest(name=name, type="native")
            )

        pocket = await pockets_service.get(pocket_id, USER)
        views = [_AttrView(w) for w in pocket["widgets"]]
        target = next(v for v in views if v.name == "Churn")

        row = format_widget_line(target)
        shown = row.split("id=")[1].split(",")[0].rstrip(")")

        assert resolve_id(shown, views) == target.id


class _AttrView:
    """Attribute access over a widget wire dict.

    ``format_widget_line`` is duck-typed on attributes so it works for Beanie
    subdocs and domain objects alike; ``get_pocket`` hands back wire dicts. This
    adapts one to the other without asserting anything about either.
    """

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, item: str):
        if item == "id":
            return self._data.get("id") or self._data.get("_id")
        return self._data.get(item)
