"""Ripple widget state must persist when the spec lives in an attachment.

``run_core._extract_ripple_attachment`` strips the ``ui-spec`` fence out of an
agent reply and stores the spec as ``{"type": "ripple", "meta": <spec>}``. The
frontend addresses those specs as ``att_<n>`` (the n-th ripple attachment,
0-based, other attachment types skipped). Before this fix every such PATCH
404'd because only the content fence was searched, so a refresh reset clicked
buttons and finished flows.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.realtime.events import MessageUiStateUpdated
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.chat.ripple_content_patcher import (
    patch_attachments_with_state,
    patch_content_with_state,
)
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.message import Attachment
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc
from pocketpaw_ee.cloud.shared.errors import NotFound


def _atts() -> list[dict]:
    return [
        {"type": "file", "url": "u", "name": "a.pdf", "meta": {}},
        {"type": "ripple", "url": "", "name": "", "meta": {"version": 1, "ui": {"type": "button"}}},
        {
            "type": "ripple",
            "url": "",
            "name": "",
            "meta": {"ui": {"type": "card"}, "state": {"keep": 1, "x": "old"}},
        },
    ]


# ---------------------------------------------------------------------------
# Pure function
# ---------------------------------------------------------------------------


def test_att_index_counts_only_ripple_attachments():
    out = patch_attachments_with_state(_atts(), "att_0", {"__paw_done": True})
    assert out is not None
    assert out[0] == _atts()[0]
    assert out[1]["meta"] == {"version": 1, "ui": {"type": "button"}, "state": {"__paw_done": True}}
    assert out[2] == _atts()[2]


def test_att_merges_into_existing_state():
    out = patch_attachments_with_state(_atts(), "att_1", {"x": "new", "__paw_done": True})
    assert out is not None
    assert out[2]["meta"]["state"] == {"keep": 1, "x": "new", "__paw_done": True}
    assert out[2]["meta"]["ui"] == {"type": "card"}


def test_att_does_not_mutate_input():
    src = _atts()
    patch_attachments_with_state(src, "att_0", {"a": 1})
    assert src == _atts()


def test_att_accepts_attachment_models():
    models = [Attachment(**a) for a in _atts()]
    out = patch_attachments_with_state(models, "att_0", {"a": 1})
    assert out is not None
    assert out[1]["meta"]["state"] == {"a": 1}
    assert all(isinstance(a, dict) for a in out)


@pytest.mark.parametrize("spec_id", ["att_2", "att_-1", "att_x", "spec_0", "att_"])
def test_att_bad_ids_return_none(spec_id):
    assert patch_attachments_with_state(_atts(), spec_id, {"a": 1}) is None


def test_att_non_dict_meta_returns_none():
    atts = [{"type": "ripple", "url": "", "name": "", "meta": "nope"}]
    assert patch_attachments_with_state(atts, "att_0", {"a": 1}) is None


def test_content_patcher_ignores_att_ids():
    assert patch_content_with_state('```ui-spec\n{"type":"button"}\n```', "att_0", {}) is None


# ---------------------------------------------------------------------------
# Service level
# ---------------------------------------------------------------------------


async def _group_message(*, attachments: list[dict], content: str = "Here you go") -> _MessageDoc:
    group = _GroupDoc(
        workspace="w1", name="G", slug="g", type="private", members=["u1"], owner="u1"
    )
    await group.insert()
    msg = _MessageDoc(
        context_type="group",
        group=str(group.id),
        sender="agent-1",
        sender_type="agent",
        content=content,
        attachments=[Attachment(**a) for a in attachments],
    )
    await msg.insert()
    return msg


@pytest.mark.asyncio
async def test_patch_ui_state_persists_on_ripple_attachment(mongo_db, recording_bus):
    msg = await _group_message(attachments=_atts())

    await message_service.patch_ui_state(
        str(msg.id), "u1", "att_1", {"choice": "b", "__paw_done": True}
    )

    after = await _MessageDoc.get(msg.id)
    assert after.content == "Here you go"
    assert after.attachments[2].meta["state"] == {
        "keep": 1,
        "x": "old",
        "choice": "b",
        "__paw_done": True,
    }
    assert after.attachments[1].meta == {"version": 1, "ui": {"type": "button"}}
    assert after.attachments[0].type == "file"

    evs = [e for e in recording_bus.events if isinstance(e, MessageUiStateUpdated)]
    assert len(evs) == 1
    data = evs[0].data
    assert data["message_id"] == str(msg.id)
    assert data["spec_id"] == "att_1"
    assert data["state"] == {"choice": "b", "__paw_done": True}
    assert data["content"] == "Here you go"
    assert data["group_id"] == msg.group
    assert data["attachments"] == [a.model_dump() for a in after.attachments]
    assert all(isinstance(a, dict) for a in data["attachments"])


@pytest.mark.asyncio
async def test_patch_ui_state_att_missing_is_not_found(mongo_db, recording_bus):
    msg = await _group_message(attachments=[{"type": "file", "url": "u", "name": "a", "meta": {}}])

    with pytest.raises(NotFound):
        await message_service.patch_ui_state(str(msg.id), "u1", "att_0", {"a": 1})
    assert not [e for e in recording_bus.events if isinstance(e, MessageUiStateUpdated)]


@pytest.mark.asyncio
async def test_patch_ui_state_att_requires_membership(mongo_db, recording_bus):
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    msg = await _group_message(attachments=_atts())
    with pytest.raises((Forbidden, NotFound)):
        await message_service.patch_ui_state(str(msg.id), "outsider", "att_0", {"a": 1})
    after = await _MessageDoc.get(msg.id)
    assert "state" not in after.attachments[1].meta


@pytest.mark.asyncio
async def test_patch_ui_state_spec_path_unchanged(mongo_db, recording_bus):
    content = 'hi\n```ui-spec\n{"type":"checkbox"}\n```'
    msg = await _group_message(attachments=[], content=content)

    await message_service.patch_ui_state(str(msg.id), "u1", "spec_0", {"_auto_checkbox_0": True})

    after = await _MessageDoc.get(msg.id)
    assert after.content == patch_content_with_state(content, "spec_0", {"_auto_checkbox_0": True})
    ev = [e for e in recording_bus.events if isinstance(e, MessageUiStateUpdated)][0]
    assert ev.data["content"] == after.content


@pytest.mark.asyncio
async def test_live_spec_id_falls_back_to_ripple_attachment(mongo_db, recording_bus):
    """During a live run the client still holds the fence, so it sends spec_0.

    The server already stripped that fence into the ripple attachment, so the
    content has nothing to splice; the patch must land on att_0 instead.
    """
    msg = await _group_message(attachments=_atts(), content="No fence here")

    await message_service.patch_ui_state(str(msg.id), "u1", "spec_0", {"__paw_done": True})

    after = await _MessageDoc.get(msg.id)
    assert after.content == "No fence here"
    assert after.attachments[1].meta["state"] == {"__paw_done": True}
    assert after.attachments[2].meta["state"] == {"keep": 1, "x": "old"}

    ev = [e for e in recording_bus.events if isinstance(e, MessageUiStateUpdated)][0]
    assert ev.data["spec_id"] == "spec_0"
    assert ev.data["content"] == "No fence here"
    assert ev.data["attachments"] == [a.model_dump(mode="json") for a in after.attachments]


@pytest.mark.asyncio
async def test_spec_id_with_no_fence_and_no_attachment_is_not_found(mongo_db, recording_bus):
    msg = await _group_message(attachments=[], content="No fence here")

    with pytest.raises(NotFound):
        await message_service.patch_ui_state(str(msg.id), "u1", "spec_0", {"a": 1})
    assert not [e for e in recording_bus.events if isinstance(e, MessageUiStateUpdated)]
