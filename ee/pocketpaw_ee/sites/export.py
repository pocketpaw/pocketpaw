# ee/pocketpaw_ee/sites/export.py — build a site's data export: the copy its owner
# keeps when the site is destroyed.
#
# Created 2026-09-08 (sites lifecycle wave 1 chunk 2, feat/sites-delete-export).
#
# THE ONE RULE THIS MODULE EXISTS TO ENFORCE: an export that could not read
# something must FAIL, never come back empty. A delete cascade gated on "an export
# exists" is only a safety net if the export is honest about its own completeness —
# an empty file returned because the D1 was unreachable satisfies the gate exactly
# as well as a real one and destroys the data anyway. So there are two different
# empties here and they are never conflated:
#
#   * a STATIC site has no D1 at all, so ``tables`` is legitimately ``{}``;
#   * a DYNAMIC site whose D1 cannot be read raises ``ExportUnavailable``.
#
# ``sites/service.py`` already carries the note that the operator data-view "stays a
# recent-records list, not an unbounded export". This is the unbounded one, and the
# difference is deliberate: the data-view is a panel that must stay cheap, while
# this runs once, in a job, and a truncated copy of someone's bookings is not a copy.
# Hence paging rather than the data-view's ``LIMIT``.
"""Build the JSON export of a site's data (D1 tables + captured leads)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

# Bumped when the payload shape changes incompatibly. A reader that does not
# recognise the version must refuse rather than guess — the same contract
# ``sites.design_brief`` versions itself under, for the same reason: a silently
# mis-parsed export is worse than one that will not open.
EXPORT_FORMAT = "paw-site-export/1"

# D1/SQLite cannot bind an identifier as a placeholder, so a table name is the one
# token that gets interpolated into SQL here. This module builds the statement, so
# this module owns the gate — deliberately ONE gate rather than re-checking what the
# caller already checked, because a second guard makes it impossible to write a test
# that proves either one is load-bearing.
_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Rows per D1 round trip. D1 caps response size, so a site with thousands of rows
# has to be walked rather than asked for at once.
_PAGE = 500


class ExportUnavailable(Exception):
    """The export could not be built completely, so no export should be trusted.

    Raised rather than returning a partial bundle. The delete cascade treats this as
    a hard stop: the customer's data stays where it is until an export succeeds.
    """


def _safe_table(name: str) -> str:
    if not _SAFE_TABLE.match(name or ""):
        raise ExportUnavailable(f"Refusing to export a table with an unsafe name: {name!r}")
    return name


async def dump_tables(
    *,
    cloudflare: Any,
    database_id: str,
    tables: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Read every row of every named table, paging until a short page ends it.

    ``tables`` comes from the pocket spec's declared ``objects`` — the same source
    the operator data-view validates against. The internal ``_paw_*`` tables
    (migrations, handoffs, outbox) are NOT included: they are not the customer's
    records, they are not in the declared schema, and reaching them would need a
    second identifier path that nothing validates. See the module note in the PR for
    the outbox caveat.

    Any read failure propagates as ``ExportUnavailable`` rather than a short table.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for raw in tables:
        table = _safe_table(raw)
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                page = await cloudflare.query_d1(
                    database_id=database_id,
                    sql=f'SELECT * FROM "{table}" LIMIT ? OFFSET ?',
                    params=[_PAGE, offset],
                )
            except Exception as exc:  # noqa: BLE001 - re-raised as the honest failure
                raise ExportUnavailable(
                    f"Could not read table {table!r} from this site's database."
                ) from exc
            rows.extend(page)
            if len(page) < _PAGE:
                break
            offset += _PAGE
        out[table] = rows
    return out


def _jsonable(value: Any) -> Any:
    """Make a Mongo/D1 value JSON-safe without silently dropping it."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def collect_leads(
    *, workspace_id: str, site_id: str, lead_model: Any
) -> list[dict[str, Any]]:
    """Every captured form submission for this site, oldest first.

    Scoped on ``(workspace, site_id)`` — the compound index the Lead model already
    declares — so this cannot pick up another site's submissions.
    """
    docs = (
        await lead_model.find({"workspace": workspace_id, "site_id": site_id})
        .sort("+createdAt")
        .to_list()
    )
    return [
        {
            "id": str(getattr(d, "id", "")),
            "form_type": getattr(d, "form_type", ""),
            "created_at": _jsonable(getattr(d, "created_at", None)),
            "properties": _jsonable(getattr(d, "properties", {}) or {}),
            "source": _jsonable(
                d.source.model_dump() if hasattr(getattr(d, "source", None), "model_dump") else {}
            ),
        }
        for d in docs
    ]


def render_bundle(
    *,
    site: dict[str, Any],
    tables: dict[str, list[dict[str, Any]]],
    leads: list[dict[str, Any]],
    notes: list[str] | None = None,
) -> bytes:
    """Serialise the export. UTF-8 JSON, pretty-printed because a human opens it."""
    payload = {
        "format": EXPORT_FORMAT,
        "exported_at": datetime.now(UTC).isoformat(),
        "site": site,
        "tables": {name: [_jsonable(r) for r in rows] for name, rows in tables.items()},
        "leads": leads,
        # Said in the file itself, not only in the UI that offered it: someone
        # opening this months later needs to know what it does NOT contain.
        "notes": notes
        or [
            "Visitor analytics are not included. They live in Cloudflare Analytics "
            "Engine with a three-month retention we do not control, and cannot be "
            "exported or deleted through this product.",
            "Internal tables (_paw_migrations, _paw_handoffs, _paw_outbox) are not "
            "included — only the tables declared in the site's own schema.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
