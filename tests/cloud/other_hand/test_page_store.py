# tests/cloud/other_hand/test_page_store.py — the Otherhand page store.
#
# Created 2026-09-15 (feat/otherhand-page-store). Pins the contract the frontend
# will build against: GET/PUT ``/other-hand/pages/{page_id}`` and the list, plus
# the four properties that are easy to lose in a refactor and expensive to lose
# in production —
#
#   * tenancy: two workspaces sharing a client-minted page id never see each
#     other's ink (the read-path filter, cloud rule 7);
#   * round-trip: strokes and the ``book`` ref come back byte-identical,
#     including nested keys the server does not model (``text``/``img``/
#     ``icon``/``kind``/``color``);
#   * compare-and-set: a stale ``base_rev`` is refused with 409 and the refusal
#     carries the server's current page — this is the whole multi-tab story;
#   * the size cap refuses the WHOLE write and leaves the stored page untouched.
#
# Service-level tests hit ``other_hand.service`` directly against the mongomock
# fixture; the router tests go through an ASGI client so the wire shape (status
# codes, the ``page`` key on a 409) is what is pinned, not a Python return value.
# Every test that writes requests ``recording_bus`` so ``emit`` records rather
# than asserts; the event tests read ``bus.events``.

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    NotFound,
    OtherhandPageConflict,
    PayloadTooLarge,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.other_hand import service
from pocketpaw_ee.cloud.other_hand.dto import UpsertPageRequest
from pocketpaw_ee.cloud.other_hand.router import router as other_hand_router

WS_A = "ws-a"
WS_B = "ws-b"
USER = "u-1"
PAGE = "3f9a1c07-0000-4000-8000-000000000001"

# A stroke list exercising every optional key the frontend's ``Stroke`` carries
# today, plus one (``color``) that landed after the server was written. The
# server must round-trip all of them without knowing what they are.
STROKES: list[dict[str, Any]] = [
    {
        "id": "s1",
        "owner": "user",
        "points": [{"x": 100, "y": 80, "p": 0.5}, {"x": 101, "y": 82, "p": 0.6}],
        "width": 2.2,
        "color": "#d0342c",
    },
    {
        "id": "s2",
        "owner": "agent",
        "points": [{"x": 120, "y": 200, "p": 0.5}],
        "width": 2.2,
        "text": {"s": "hello", "size": 28},
    },
    {
        "id": "s3",
        "owner": "agent",
        "points": [{"x": 300, "y": 300, "p": 0.5}],
        "width": 2.2,
        "img": {"src": "data:image/png;base64,iVBORw0KGgo=", "w": 200, "h": 120},
    },
    {
        "id": "s4",
        "owner": "agent",
        "points": [{"x": 500, "y": 500, "p": 0.5}],
        "width": 2.2,
        "icon": {"name": "star", "size": 40},
        "kind": "point",
    },
]
BOOK = {"fileId": "file-abc", "name": "paper.pdf", "pageNumber": 3, "mime": "application/pdf"}


def _req(**kw: Any) -> UpsertPageRequest:
    return UpsertPageRequest(**{"strokes": STROKES, "book": BOOK, **kw})


# ── service ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_round_trips_strokes_and_book_verbatim(mongo_db, recording_bus):
    saved = await service.upsert_page(WS_A, USER, PAGE, _req(session_id="sess-1"))
    assert saved == {"page_id": PAGE, "rev": 1, "updated_at": saved["updated_at"]}

    page = await service.get_page(WS_A, USER, PAGE)
    assert page["strokes"] == STROKES
    assert page["book"] == BOOK
    assert page["session_id"] == "sess-1"
    assert page["rev"] == 1
    assert page["page_id"] == PAGE


@pytest.mark.asyncio
async def test_missing_page_is_404_not_empty(mongo_db):
    with pytest.raises(NotFound):
        await service.get_page(WS_A, USER, PAGE)


@pytest.mark.asyncio
async def test_tenant_isolation_same_page_id_two_workspaces(mongo_db, recording_bus):
    await service.upsert_page(WS_A, USER, PAGE, _req())
    await service.upsert_page(WS_B, USER, PAGE, _req(strokes=[], book=None))

    a = await service.get_page(WS_A, USER, PAGE)
    b = await service.get_page(WS_B, USER, PAGE)
    assert a["strokes"] == STROKES
    assert b["strokes"] == []
    assert b["book"] is None

    listed_a = await service.list_pages(WS_A, USER)
    assert [p["page_id"] for p in listed_a["pages"]] == [PAGE]
    assert listed_a["pages"][0]["stroke_count"] == len(STROKES)
    assert listed_a["pages"][0]["has_book"] is True
    listed_b = await service.list_pages(WS_B, USER)
    assert listed_b["pages"][0]["stroke_count"] == 0
    assert "strokes" not in listed_b["pages"][0]


@pytest.mark.asyncio
async def test_stale_base_rev_is_refused_and_carries_server_copy(mongo_db, recording_bus):
    await service.upsert_page(WS_A, USER, PAGE, _req(strokes=STROKES[:1]))  # rev 1
    tab_a = await service.upsert_page(WS_A, USER, PAGE, _req(base_rev=1))  # rev 2
    assert tab_a["rev"] == 2

    # Tab B still thinks rev is 1 and PUTs its old strokes.
    with pytest.raises(OtherhandPageConflict) as exc:
        await service.upsert_page(WS_A, USER, PAGE, _req(strokes=STROKES[:1], base_rev=1))
    assert exc.value.status_code == 409
    assert exc.value.page["rev"] == 2
    assert exc.value.page["strokes"] == STROKES
    body = exc.value.to_dict()
    assert body["error"]["code"] == "other_hand.page_conflict"
    assert body["page"]["rev"] == 2

    # Nothing changed on the server.
    assert (await service.get_page(WS_A, USER, PAGE))["strokes"] == STROKES


@pytest.mark.asyncio
async def test_base_rev_zero_against_existing_page_is_a_conflict(mongo_db, recording_bus):
    """A localStorage migration push against a page the server already has
    must not clobber it — the client has never seen the server copy."""
    await service.upsert_page(WS_A, USER, PAGE, _req())
    with pytest.raises(OtherhandPageConflict):
        await service.upsert_page(WS_A, USER, PAGE, _req(strokes=[], base_rev=0))


@pytest.mark.asyncio
async def test_racing_creates_lose_as_409_not_500(mongo_db, recording_bus, monkeypatch):
    """Two tabs creating the same new page: the unique index refuses the second
    insert and the caller gets the conflict envelope with the winner's copy."""
    from pocketpaw_ee.cloud.models.other_hand_page import OtherhandPage

    await service.upsert_page(WS_A, USER, PAGE, _req())  # the winner
    real = OtherhandPage.find_one
    calls = {"n": 0}

    async def first_read_sees_nothing(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real(*args, **kwargs)

    monkeypatch.setattr(OtherhandPage, "find_one", first_read_sees_nothing)
    with pytest.raises(OtherhandPageConflict) as exc:
        await service.upsert_page(WS_A, USER, PAGE, _req(strokes=[]))
    assert exc.value.page is not None
    assert exc.value.page["strokes"] == STROKES
    assert exc.value.page["rev"] == 1


@pytest.mark.asyncio
async def test_stale_rev_for_a_page_the_server_lacks_carries_page_null(mongo_db):
    with pytest.raises(OtherhandPageConflict) as exc:
        await service.upsert_page(WS_A, USER, PAGE, _req(base_rev=3))
    assert exc.value.to_dict()["page"] is None


@pytest.mark.asyncio
async def test_identical_resave_is_a_noop_that_does_not_bump_rev_or_emit(mongo_db, recording_bus):
    await service.upsert_page(WS_A, USER, PAGE, _req())
    assert len(recording_bus.events) == 1

    again = await service.upsert_page(WS_A, USER, PAGE, _req(base_rev=1))
    assert again["rev"] == 1
    assert len(recording_bus.events) == 1  # no second emit

    changed = await service.upsert_page(WS_A, USER, PAGE, _req(strokes=[], base_rev=1))
    assert changed["rev"] == 2
    assert len(recording_bus.events) == 2
    evt = recording_bus.events[-1]
    assert evt.type == "other_hand.page.saved"
    assert evt.data["page_id"] == PAGE
    assert evt.data["workspace_id"] == WS_A
    assert evt.data["rev"] == 2
    assert "strokes" not in evt.data


@pytest.mark.asyncio
async def test_oversize_page_is_refused_whole_and_stored_page_untouched(
    mongo_db, recording_bus, monkeypatch
):
    await service.upsert_page(WS_A, USER, PAGE, _req())
    monkeypatch.setattr(service, "MAX_PAGE_BYTES", 200)

    big = [{"id": "x", "owner": "user", "points": [{"x": i, "y": i, "p": 0.5} for i in range(50)]}]
    with pytest.raises(PayloadTooLarge) as exc:
        await service.upsert_page(WS_A, USER, PAGE, _req(strokes=big, base_rev=1))
    assert exc.value.code == "other_hand.page_too_large"

    page = await service.get_page(WS_A, USER, PAGE)
    assert page["strokes"] == STROKES
    assert page["rev"] == 1


@pytest.mark.asyncio
async def test_page_id_must_be_one_safe_segment(mongo_db):
    with pytest.raises(CloudError) as exc:
        await service.upsert_page(WS_A, USER, "../etc", _req())
    assert exc.value.status_code == 400
    with pytest.raises(CloudError):
        await service.get_page(WS_A, USER, "a/b")


# ── router ─────────────────────────────────────────────────────────────────


def _app(workspace_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(other_hand_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[current_user_id] = lambda: USER

    @app.exception_handler(CloudError)
    async def _cloud_error(_request, exc: CloudError):  # noqa: ANN202
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    return app


@pytest_asyncio.fixture
async def client(mongo_db, recording_bus) -> AsyncClient:
    transport = ASGITransport(app=_app(WS_A))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_wire_contract(client: AsyncClient):
    url = f"/api/v1/other-hand/pages/{PAGE}"

    assert (await client.get(url)).status_code == 404

    r = await client.put(url, json={"strokes": STROKES, "book": BOOK, "session_id": "s1"})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"page_id", "rev", "updated_at"}
    assert r.json()["rev"] == 1

    r = await client.get(url)
    assert r.status_code == 200
    body = r.json()
    assert body["strokes"] == STROKES
    assert body["book"] == BOOK
    assert set(body) == {
        "page_id",
        "session_id",
        "strokes",
        "book",
        "rev",
        "updated_at",
        "created_at",
    }

    # Stale tab: 409 with the current page in the body.
    r = await client.put(url, json={"strokes": [], "base_rev": 0})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "other_hand.page_conflict"
    assert r.json()["page"]["strokes"] == STROKES

    r = await client.put(url, json={"strokes": [], "base_rev": 1})
    assert r.status_code == 200
    assert r.json()["rev"] == 2

    r = await client.get("/api/v1/other-hand/pages")
    assert r.status_code == 200
    assert r.json() == {"pages": [{**r.json()["pages"][0], "page_id": PAGE, "stroke_count": 0}]}

    # An unsafe id (a space, once decoded) is refused at the wire as 400, not 500.
    # ``..`` would be a better example but the ASGI stack normalises it away
    # into a route miss before the handler ever sees it.
    r = await client.put("/api/v1/other-hand/pages/bad%20id", json={"strokes": []})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "other_hand.invalid_page_id"
