# tests/ee/sites/test_site_export.py — the site data export (sites lifecycle wave 1
# chunk 2). The behaviour under test is mostly about HONESTY: the delete cascade is
# gated on "an export exists", so an export that comes back empty because it could
# not read is indistinguishable from one that read an empty site, and satisfies the
# gate just as well before destroying the data anyway.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.sites.export import (
    EXPORT_FORMAT,
    ExportUnavailable,
    collect_leads,
    dump_tables,
    render_bundle,
)


class FakeCloudflare:
    """Serves fixed rows per table, and can be told to fail."""

    def __init__(self, rows: dict[str, list[dict]] | None = None, fail: bool = False) -> None:
        self.rows = rows or {}
        self.fail = fail
        self.calls: list[tuple[str, list]] = []

    async def query_d1(self, *, database_id: str, sql: str, params: list) -> list[dict]:
        if self.fail:
            raise RuntimeError("d1 unreachable")
        self.calls.append((sql, list(params)))
        table = sql.split('"')[1]
        limit, offset = params
        return self.rows.get(table, [])[offset : offset + limit]


@pytest.mark.asyncio
async def test_dump_tables_pages_past_the_first_round_trip() -> None:
    """A site with more rows than one page must not be silently truncated — a partial
    copy of someone's bookings is not a copy."""
    rows = [{"id": i} for i in range(1201)]
    cf = FakeCloudflare({"bookings": rows})

    out = await dump_tables(cloudflare=cf, database_id="db1", tables=["bookings"])

    assert out["bookings"] == rows
    assert len(cf.calls) == 3  # 500 + 500 + 201 (short page ends it)


@pytest.mark.asyncio
async def test_an_empty_table_is_one_round_trip_and_reads_as_empty() -> None:
    cf = FakeCloudflare({"bookings": []})
    assert await dump_tables(cloudflare=cf, database_id="db1", tables=["bookings"]) == {
        "bookings": []
    }


@pytest.mark.asyncio
async def test_an_unreadable_d1_raises_instead_of_returning_an_empty_export() -> None:
    """THE CENTRAL GUARANTEE. Returning ``{}`` here would let the cascade record a
    satisfied export precondition and then destroy the database it could not read."""
    cf = FakeCloudflare({"bookings": [{"id": 1}]}, fail=True)

    with pytest.raises(ExportUnavailable):
        await dump_tables(cloudflare=cf, database_id="db1", tables=["bookings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ['bookings"; DROP TABLE x--', "book ings", "", "1bookings", "a-b"])
async def test_an_unsafe_table_name_is_refused(bad: str) -> None:
    """The table is the one token interpolated into SQL, so this module owns the gate."""
    cf = FakeCloudflare({bad: []})
    with pytest.raises(ExportUnavailable):
        await dump_tables(cloudflare=cf, database_id="db1", tables=[bad])


class FakeLead:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class FakeLeadModel:
    """Minimal stand-in for the Beanie Lead document's query surface."""

    def __init__(self, docs: list[FakeLead]) -> None:
        self.docs = docs
        self.query: dict | None = None

    def find(self, query: dict):
        self.query = query
        self._matched = [
            d
            for d in self.docs
            if d.workspace == query["workspace"] and d.site_id == query["site_id"]
        ]
        return self

    def sort(self, _key: str):
        return self

    async def to_list(self):
        return self._matched


@pytest.mark.asyncio
async def test_collect_leads_is_scoped_to_this_site_only() -> None:
    mine = FakeLead(
        id="l1",
        workspace="w1",
        site_id="s1",
        form_type="contact",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        properties={"email": "a@b.c"},
        source=None,
    )
    theirs = FakeLead(
        id="l2",
        workspace="w1",
        site_id="s2",
        form_type="contact",
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
        properties={"email": "x@y.z"},
        source=None,
    )
    model = FakeLeadModel([mine, theirs])

    out = await collect_leads(workspace_id="w1", site_id="s1", lead_model=model)

    assert [lead["id"] for lead in out] == ["l1"]
    assert model.query == {"workspace": "w1", "site_id": "s1"}
    # Datetimes survive as ISO strings rather than crashing json.dumps later.
    assert out[0]["created_at"] == "2026-01-02T00:00:00+00:00"


def test_the_bundle_names_its_format_and_what_it_omits() -> None:
    """A reader months later must be able to tell what this file does NOT contain,
    from the file — not from the dialog that offered it."""
    import json

    raw = render_bundle(
        site={"id": "s1", "name": "Bright Smile"},
        tables={"bookings": [{"id": 1, "at": datetime(2026, 5, 1, tzinfo=UTC)}]},
        leads=[],
    )
    payload = json.loads(raw)

    assert payload["format"] == EXPORT_FORMAT
    assert payload["site"]["name"] == "Bright Smile"
    assert payload["tables"]["bookings"][0]["at"] == "2026-05-01T00:00:00+00:00"
    joined = " ".join(payload["notes"]).lower()
    assert "analytics" in joined
    assert "_paw_outbox" in joined


def test_a_static_site_exports_an_empty_table_map_without_complaint() -> None:
    """The legitimate empty: no D1 at all. Distinct from an unreadable one, which
    never reaches this function because dump_tables raises first."""
    import json

    payload = json.loads(render_bundle(site={"id": "s1"}, tables={}, leads=[]))
    assert payload["tables"] == {}
