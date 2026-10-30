"""A reply quote's preview carries the parent's first media attachment.

A reply to a photo, video or file sent with no text quoted nothing: the preview
held only the (empty) content. ``preview_attachment`` picks the first
file/image/audio attachment, skipping non-file ones like ripple specs.
"""

from pocketpaw_ee.cloud.chat.domain import Attachment
from pocketpaw_ee.cloud.chat.dto import preview_attachment


def test_picks_first_media_attachment_with_mime():
    attachments = (
        Attachment(type="ripple", url="", name="flow", meta=()),
        Attachment(type="file", url="/u/clip.mp4", name="clip.mp4", meta=(("mime", "video/mp4"),)),
        Attachment(type="image", url="/u/a.png", name="a.png", meta=()),
    )
    assert preview_attachment(attachments) == {
        "type": "file",
        "url": "/u/clip.mp4",
        "name": "clip.mp4",
        "mime": "video/mp4",
    }


def test_none_without_media():
    assert preview_attachment(()) is None
    assert preview_attachment((Attachment(type="ripple", url="", name="flow"),)) is None
