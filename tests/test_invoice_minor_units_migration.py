# tests/test_invoice_minor_units_migration.py — the one-off Mongo script that moves
# sites.client_invoices[].amount_cents to ISO 4217 minor units. Loaded from its
# path (scripts/ has no importable package) and run against mongomock-motor.

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


async def test_dry_run_counts_and_writes_nothing(db) -> None:
    mod = _load()
    before = await _amounts(db)
    result = await mod.run(db, apply=False)
    assert result["invoices_by_currency"] == {"JPY": 1, "KWD": 1}
    assert result["sites_affected"] == 2
    assert await _amounts(db) == before
    assert await db["schema_migrations"].count_documents({}) == 0


async def test_apply_converts_once(db) -> None:
    mod = _load()
    result = await mod.run(db, apply=True)
    assert result["sites_updated"] == 2 and result["sites_skipped"] == []
    assert await _amounts(db) == {
        "s1": [1999, 1500],
        "s2": [1250],
        "s3": [500],
        "s4": [],
    }
    again = await mod.run(db, apply=True)
    assert again["refused"] == "already_applied"
    assert (await _amounts(db))["s1"] == [1999, 1500]
