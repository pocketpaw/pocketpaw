# tests/test_invoice_minor_units_migration.py — the Mongo script that moves
# sites.client_invoices[].amount_cents to ISO 4217 minor units. Loaded from its
# path (scripts/ has no importable package) and run against mongomock-motor.
# The invariant under test: an invoice is converted at most once, however many
# times the script runs. The create path that stamps new rows is tested in
# tests/ee/sites/test_client_record.py.

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from mongomock_motor import AsyncMongoMockClient

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrations"
    / "2026_10_01_invoice_minor_units.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("invoice_minor_units", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inv(amount: int, currency: str) -> dict:
    return {"id": f"i-{currency}-{amount}", "amount_cents": amount, "currency": currency}


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["paw"]
    await database["sites"].insert_many(
        [
            {"_id": "s1", "client_invoices": [_inv(1999, "USD"), _inv(150000, "JPY")]},
            {"_id": "s2", "client_invoices": [_inv(125, "KWD")]},
            {"_id": "s3", "client_invoices": [_inv(500, "EUR")]},
            {"_id": "s4", "client_invoices": []},
        ]
    )
    return database


async def _amounts(db) -> dict[str, list[int]]:
    out = {}
    async for site in db["sites"].find({}):
        out[site["_id"]] = [i["amount_cents"] for i in site["client_invoices"]]
    return out


_CONVERTED = {"s1": [1999, 1500], "s2": [1250], "s3": [500], "s4": []}


async def test_dry_run_counts_and_writes_nothing(db) -> None:
    mod = _load()
    before = await _amounts(db)
    result = await mod.run(db, apply=False)
    assert result["invoices_by_currency"] == {"JPY": 1, "KWD": 1}
    assert result["sites_affected"] == 3  # s3's EUR row is stamped, not changed
    assert await _amounts(db) == before
    assert await db["schema_migrations"].count_documents({}) == 0


async def test_apply_converts_and_stamps_once(db) -> None:
    mod = _load()
    result = await mod.run(db, apply=True)
    assert result["sites_updated"] == 3 and result["sites_skipped"] == []
    assert await _amounts(db) == _CONVERTED
    async for site in db["sites"].find({"client_invoices.0": {"$exists": True}}):
        assert {i["amount_unit"] for i in site["client_invoices"]} == {"iso4217"}
    again = await mod.run(db, apply=True)
    assert again["refused"] == "already_applied"
    assert await _amounts(db) == _CONVERTED


async def test_force_after_an_apply_converts_nothing_twice(db) -> None:
    mod = _load()
    await mod.run(db, apply=True)
    forced = await mod.run(db, apply=True, force=True)
    assert forced["sites_affected"] == 0 and forced["invoices_by_currency"] == {}
    assert await _amounts(db) == _CONVERTED


class _CrashAfterFirstWrite:
    """A db whose ``sites.update_one`` dies after one successful write: a run
    killed half-way, with no marker recorded."""

    def __init__(self, db) -> None:
        self._db = db

    def __getitem__(self, name: str):
        coll = self._db[name]
        if name != "sites":
            return coll
        db = self._db

        class _Sites:
            writes = 0

            def find(self, *a, **kw):
                return coll.find(*a, **kw)

            async def update_one(self, *a, **kw):
                if _Sites.writes:
                    raise RuntimeError("connection lost")
                _Sites.writes += 1
                return await db["sites"].update_one(*a, **kw)

        return _Sites()


async def test_a_rerun_after_a_partial_run_converts_nothing_twice(db) -> None:
    mod = _load()
    with pytest.raises(RuntimeError):
        await mod.run(_CrashAfterFirstWrite(db), apply=True)
    assert await db["schema_migrations"].count_documents({}) == 0
    partial = await _amounts(db)
    assert partial != _CONVERTED  # one site done, the rest not

    result = await mod.run(db, apply=True)
    assert result["sites_skipped"] == []
    assert await _amounts(db) == _CONVERTED
    assert (await mod.run(db, apply=True, force=True))["sites_affected"] == 0
    assert await _amounts(db) == _CONVERTED


async def test_stamped_and_unstamped_invoices_on_one_site() -> None:
    mod = _load()
    database = AsyncMongoMockClient()["paw"]
    new_yen = {**_inv(1500, "JPY"), "id": "new", "amount_unit": "iso4217"}  # recorded post-switch
    await database["sites"].insert_one(
        {"_id": "m", "client_invoices": [new_yen, _inv(150000, "JPY"), _inv(125, "KWD")]}
    )
    result = await mod.run(database, apply=True)
    assert result["invoices_by_currency"] == {"JPY": 1, "KWD": 1}
    site = await database["sites"].find_one({"_id": "m"})
    assert [i["amount_cents"] for i in site["client_invoices"]] == [1500, 1500, 1250]
    assert [i["amount_unit"] for i in site["client_invoices"]] == ["iso4217"] * 3
