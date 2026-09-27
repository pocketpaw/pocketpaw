"""Regression: history backfills display fields on nameless upload attachments.

Created 2026-09-27 (fix/chat-attachment-name-backfill). Agent-chat sends used
to carry bare ``{"url": "/api/v1/uploads/<id>"}`` attachments, and the run path
persisted them verbatim, so stored user messages have ``name=""``, no
``meta.mime``/``meta.size`` and ``type="file"`` even for images. After a reload
the client rendered a nameless "file" chip with no thumbnail.

``get_history`` now fills ``name``, ``meta.{mime,size,id}`` and an image/audio
``type`` from the upload record at read time, scoped to the session's
workspace. Stored rows are not rewritten; unknown / foreign / non-upload urls
and already-named attachments come back untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.models.message import Message
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest
from pocketpaw_ee.cloud.uploads.models import FileUpload

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _seed_upload(
    file_id: str,
    *,
    workspace: str = "w1",
    filename: str = "photo.png",
    mime: str = "image/png",
    size: int = 1234,
) -> None:
    await FileUpload(
        file_id=file_id,
        storage_key=f"k/{file_id}",
        filename=filename,
        mime=mime,
        size=size,
        workspace=workspace,
        owner="u1",
    ).insert()


async def _history_with(attachments: list[dict]) -> list[dict]:
    session = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t"))
    await message_service.persist_user_message_for_scope(
        kind="session",
        scope_id=session.id,
        user_id="u1",
        workspace_id="w1",
        session_key=f"cloud:session:{session.id}:agent-x",
        content="look",
        attachments=attachments,
    )
    result = await sessions_service.get_history(session.id, "u1")
    [msg] = result["messages"]
    return msg["attachments"]


async def test_nameless_image_upload_gets_name_meta_and_type() -> None:
    await _seed_upload("img1", filename="photo.png", mime="image/png", size=1234)

    [att] = await _history_with([{"url": "/api/v1/uploads/img1"}])

    assert att["name"] == "photo.png"
    assert att["type"] == "image"
    assert att["meta"]["mime"] == "image/png"
    assert att["meta"]["size"] == 1234
    assert att["meta"]["id"] == "img1"


async def test_audio_and_plain_file_types() -> None:
    await _seed_upload("a1", filename="memo.m4a", mime="audio/mp4", size=10)
    await _seed_upload("d1", filename="report.pdf", mime="application/pdf", size=20)

    audio, doc = await _history_with([{"url": "/api/v1/uploads/a1"}, {"url": "/api/v1/uploads/d1"}])

    assert (audio["type"], audio["name"]) == ("audio", "memo.m4a")
    assert (doc["type"], doc["name"]) == ("file", "report.pdf")
    assert doc["meta"] == {"mime": "application/pdf", "size": 20, "id": "d1"}


async def test_stored_row_is_not_rewritten() -> None:
    await _seed_upload("img2")
    await _history_with([{"url": "/api/v1/uploads/img2"}])

    [stored] = await Message.find_all().to_list()
    assert stored.attachments[0].name == ""
    assert stored.attachments[0].meta == {}


async def test_other_workspace_upload_is_not_leaked() -> None:
    await _seed_upload("foreign", workspace="w2", filename="secret-plan.pdf")

    [att] = await _history_with([{"url": "/api/v1/uploads/foreign"}])

    assert att["name"] == ""
    assert att["meta"] == {}
    assert att["type"] == "file"


async def test_missing_record_is_left_unchanged() -> None:
    [att] = await _history_with([{"url": "/api/v1/uploads/gone"}])

    assert att == {"type": "file", "url": "/api/v1/uploads/gone", "name": "", "meta": {}}


async def test_named_link_and_artifact_attachments_are_untouched() -> None:
    await _seed_upload("img3", filename="real.png")
    given = [
        {"type": "image", "url": "/api/v1/uploads/img3", "name": "renamed.png", "meta": {}},
        {"url": "https://example.com/page"},
        {"type": "artifact", "url": "/api/v1/uploads/img3", "name": "", "meta": {}},
    ]

    named, link, artifact = await _history_with(given)

    assert named["name"] == "renamed.png"
    assert named["meta"] == {}
    assert link == {"type": "file", "url": "https://example.com/page", "name": "", "meta": {}}
    assert artifact["name"] == ""
    assert artifact["meta"] == {}


async def test_lookup_failure_does_not_fail_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from pocketpaw_ee.cloud.uploads import service as uploads_service

    async def _boom(*_a: object, **_k: object) -> dict:
        raise RuntimeError("mongo down")

    monkeypatch.setattr(uploads_service, "get_records_scoped", _boom, raising=False)
    await _seed_upload("img4")

    [att] = await _history_with([{"url": "/api/v1/uploads/img4"}])

    assert att["name"] == ""
