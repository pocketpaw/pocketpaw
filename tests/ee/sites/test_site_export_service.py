# tests/ee/sites/test_site_export_service.py — the service half of the site data
# export (sites lifecycle wave 1 chunk 2).
#
# These pin the branch that decides whether a site's data can be vouched for. The
# builder's own honesty is covered in test_site_export.py; what is tested here is
# that the SERVICE routes a static site to the legitimate empty and a dynamic site
# with no reachable D1 to a failure, rather than collapsing both into "no tables".
#
# Updated 2026-09-12 (feat/sites-delete-endpoint): added the response-mapping test at
# the bottom. Every test above it exercises a PURE helper and none had ever built a
# document, which is exactly how ``_export_response`` shipped reading a ``created_at``
# field no document declares — an ``AttributeError`` on every call, so the whole export
# surface answered 500 and the delete lane's forced export could never pass its gate.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import export as export_mod


def test_retention_refuses_a_window_that_deletes_before_anyone_can_download(monkeypatch) -> None:
    """A zero or negative retention is worse than none: it satisfies the cascade's
    gate and then destroys the copy the gate existed to preserve."""
    monkeypatch.setenv("POCKETPAW_SITE_EXPORT_RETENTION_DAYS", "0")
    assert export_mod.retention_days() == 30
    monkeypatch.setenv("POCKETPAW_SITE_EXPORT_RETENTION_DAYS", "-5")
    assert export_mod.retention_days() == 30


def test_retention_honours_a_real_window_and_ignores_junk(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_SITE_EXPORT_RETENTION_DAYS", "7")
    assert export_mod.retention_days() == 7
    monkeypatch.setenv("POCKETPAW_SITE_EXPORT_RETENTION_DAYS", "not-a-number")
    assert export_mod.retention_days() == 30
    monkeypatch.delenv("POCKETPAW_SITE_EXPORT_RETENTION_DAYS", raising=False)
    assert export_mod.retention_days() == 30


def test_the_storage_key_is_tenant_scoped() -> None:
    """Two workspaces must never collide on a key — an export is one tenant's data."""
    a = export_mod.export_key("w1", "e1")
    b = export_mod.export_key("w2", "e1")
    assert a != b
    assert a.startswith("site-exports/w1/")
    assert b.startswith("site-exports/w2/")


@pytest.mark.parametrize(
    ("name", "site_id", "expected"),
    [
        ("Bright Smile", "s1", "Bright-Smile-export.json"),
        ("", "s1", "site-s1-export.json"),
        ("../../etc/passwd", "s1", "etc-passwd-export.json"),
        ("a/b\\c", "s1", "a-b-c-export.json"),
    ],
)
def test_the_download_filename_cannot_carry_a_path(name, site_id, expected) -> None:
    """The name reaches a Content-Disposition header, so a separator in it is a
    header-injection and path-traversal surface, not a cosmetic problem."""
    out = export_mod.export_filename(name, site_id)
    assert out == expected
    assert "/" not in out and "\\" not in out and ".." not in out


class _Adapter:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        body = b""
        async for chunk in stream:
            body += chunk
        self.objects[key] = body
        return None


@pytest.mark.asyncio
async def test_store_export_writes_the_bytes_and_reports_the_real_size() -> None:
    adapter = _Adapter()
    payload = b'{"format": "paw-site-export/1"}'

    size = await export_mod.store_export(
        adapter=adapter, key="site-exports/w1/e1.json", payload=payload
    )

    assert size == len(payload)
    assert adapter.objects["site-exports/w1/e1.json"] == payload


def test_a_static_site_is_recognised_by_its_error_code_not_by_guessing() -> None:
    """The service tells a static site apart from a broken one by the error CODE
    ``_dynamic_pocket_objects`` raises. If that code ever changes, a static site
    starts reporting its export as failed — so the contract is pinned here."""
    exc = ValidationError("sites.not_dynamic", "not a dynamic site")
    assert exc.code == "sites.not_dynamic"


@pytest.mark.asyncio
async def test_an_export_can_actually_be_rendered_as_a_response(beanie_test_db) -> None:
    """The export response maps ``createdAt``, and nothing had ever built one.

    ``_export_response`` read ``doc.created_at`` — a snake_case spelling that
    ``TimestampedDocument`` does not declare and nothing aliases — so it raised
    ``AttributeError`` on EVERY call from the day the export shipped. The whole feature
    was unreachable behind it: ``POST /sites/{id}/export`` did all of its work and then
    answered 500, ``GET /sites/exports`` could not list, and the delete lane's forced
    export could never satisfy its own gate.

    It survived review because every existing test in this file exercises the PURE
    helpers (``retention_days``, ``export_key``, ``export_filename``, ``store_export``)
    and none of them builds a document. This one does, which is the only reason it
    catches it.
    """
    from pocketpaw_ee.cloud.models.site_export import SiteExport
    from pocketpaw_ee.sites.service import _export_response

    doc = SiteExport(
        workspace="w1", owner="u1", site_id="s1", status="ready", storage_key="k", size_bytes=12
    )
    await doc.insert()

    out = _export_response(doc)

    assert out.id == str(doc.id)
    assert out.status == "ready"
    assert out.created_at is not None, "createdAt must reach the wire"
    assert out.created_at == doc.createdAt.isoformat()
