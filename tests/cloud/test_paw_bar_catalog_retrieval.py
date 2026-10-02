# tests/cloud/test_paw_bar_catalog_retrieval.py — what a concierge turn sees of
# the catalog store, and how cards are hydrated from it.
#
#   * ``catalog_for_turn``: a catalog of at most 50 goes whole in owner order;
#     a bigger one gives the page's product, the search hits, and the first 10
#     when the search is weak, de-duplicated; no store or a failing one is [].
#   * ``with_page_product``: the item whose url is the visitor's page.
#   * ``FenceFilter.afeed`` hydrates a card through the store lookup, so any id in
#     the catalog works, not only the ones the prompt listed; ``card_ids`` says
#     which ids a fence names.
#   * The prompt lists sold-out items last and marked.

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.paw_bar import concierge_runtime as cr

from pocketpaw.paw_bar.models import PawBarActionSpec, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore


def _item(i: Any, **ov: Any) -> dict:
    return {"id": f"p{i}", "name": f"Plain thing {i}", "price_cents": 100, **ov}


async def _widget(store: PawBarStore, items: list[dict]) -> PawBarWidget:
    w = await store.create_widget(
        PawBarWidget(
            pocket_id="p",
            owner="o",
            spec=PawBarSpec(
                widget_id="w",
                pocket_id="p",
                actions=[PawBarActionSpec(verb="add_to_cart", policy="auto")],
            ),
        )
    )
    if items:
        await store.upsert_catalog_items(w.id, items)
    return w


@pytest.fixture
def store(tmp_path: Path) -> PawBarStore:
    return PawBarStore(tmp_path / "retrieval.db")


async def test_a_small_catalog_goes_whole_in_owner_order(store):
    w = await _widget(store, [_item(i) for i in range(cr.CATALOG_ALL_UP_TO)])
    items = await cr.catalog_for_turn(store, w, "anything at all")
    assert [i.id for i in items] == [f"p{i}" for i in range(cr.CATALOG_ALL_UP_TO)]


async def test_a_big_catalog_sends_the_page_product_and_the_search_hits(store):
    items = [_item(i) for i in range(120)]
    items[77] = _item(77, name="Stoneware mug")
    items[90] = _item(90, name="Travel mug", description="keeps coffee hot")
    items[100] = _item(100, name="Mug rack")
    items[5] = _item(5, name="Kettle", url="/shop/kettle")
    w = await _widget(store, items)

    page = cr.PageContext(url="https://brewco.com/shop/kettle", title="Kettle", key="shop/kettle")
    page = await cr.with_page_product(page, w, store)
    assert page.product is not None and page.product.id == "p5"

    got = await cr.catalog_for_turn(store, w, "do you sell mugs?", page)
    assert got[0].id == "p5"
    assert {i.id for i in got[1:]} == {"p77", "p90", "p100"}


async def test_a_weak_search_adds_the_first_ten_without_repeats(store):
    items = [_item(i) for i in range(60)]
    items[3] = _item(3, name="Copper kettle")
    w = await _widget(store, items)

    got = await cr.catalog_for_turn(store, w, "kettle")
    ids = [i.id for i in got]
    assert ids == ["p3"] + [f"p{i}" for i in range(10) if i != 3]

    empty = await cr.catalog_for_turn(store, w, "")
    assert [i.id for i in empty] == [f"p{i}" for i in range(10)]


async def test_no_store_or_a_failing_one_is_an_empty_catalog():
    w = SimpleNamespace(id="w1")
    assert await cr.catalog_for_turn(None, w, "q") == []

    class Broken:
        async def catalog_count(self, _wid: str) -> int:
            raise RuntimeError("db gone")

    assert await cr.catalog_for_turn(Broken(), w, "q") == []


async def test_a_card_hydrates_any_catalog_id_through_the_lookup(store):
    w = await _widget(store, [_item(i) for i in range(80)])
    fences = cr._fence_filter_for(w, store=store)
    spec = {"ui": {"type": "product-card", "props": {"ids": ["p79", "nope", "p0"]}}}
    out = "".join(await fences.afeed(f"Here:\n```pawbar-card\n{json.dumps(spec)}\n```\n"))
    body = json.loads(out.split("```pawbar-card\n", 1)[1].split("\n```", 1)[0])
    assert [i["id"] for i in body["ui"]["props"]["items"]] == ["p79", "p0"]
    assert body["ui"]["props"]["items"][0]["name"] == "Plain thing 79"

    legacy = json.dumps(
        {"kind": "product", "items": [{"id": "p1", "name": "fake", "price_cents": 1}]}
    )
    out = "".join(await fences.afeed(f"```pawbar-card\n{legacy}\n```"))
    assert '"price_cents":100' in out and "fake" not in out

    unknown = json.dumps({"ui": {"type": "product-card", "props": {"ids": ["ghost"]}}})
    assert "".join(await fences.afeed(f"```pawbar-card\n{unknown}\n```")) == ""


def test_card_ids():
    from pocketpaw_ee.paw_bar.card_spec import card_ids

    spec = {
        "ui": {
            "type": "stack",
            "children": [
                {"type": "product-card", "props": {"ids": ["a", "b", 3, " a "]}},
                {"type": "product-card", "props": {"ids": ["c"]}},
            ],
        }
    }
    assert card_ids(json.dumps(spec)) == ["a", "b", "c"]
    assert card_ids(json.dumps({"kind": "product", "items": [{"id": "x"}, {"id": ""}]})) == ["x"]
    assert card_ids("not json") == []


def test_the_prompt_lists_sold_out_items_last():
    from pocketpaw_ee.cloud.surface.handlers.concierge import _catalog_block

    block = _catalog_block(
        [
            {"id": "gone", "name": "Gone", "price_cents": 100, "in_stock": False},
            {"id": "maybe", "name": "Maybe", "price_cents": 100, "in_stock": None},
            {"id": "here", "name": "Here", "price_cents": 100, "in_stock": True},
        ]
    )
    lines = [line for line in block.splitlines() if line.strip().startswith("- id")]
    assert [line.split('"')[1] for line in lines] == ["maybe", "here", "gone"]
    assert lines[-1].endswith("(sold out)")


async def test_the_v2_prompt_carries_the_turns_catalog(store):
    w = await _widget(store, [_item(1, name="Espresso")])
    items = await cr.catalog_for_turn(store, w, "coffee")
    prompt = cr.build_prompt([], w, [], "hi", catalog=items)
    assert "<catalog>" in prompt and 'id "p1": Espresso' in prompt
