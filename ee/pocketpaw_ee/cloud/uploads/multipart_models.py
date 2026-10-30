"""Beanie document for one resumable/multipart upload session.

Created 2026-09-14 (feat/uploads-multipart-endpoints). A multipart upload is the
first upload in this package that exists as SERVER STATE between two requests.
Every other path here is one request: bytes in, ``FileUpload`` row out. Here the
row is not written until ``complete``, and between ``init`` and ``complete`` the
only record that the upload exists — which key it is landing on, which provider
upload id to hand back to S3, which parts have arrived, what was claimed against
the workspace's daily budget — is this document.

**Why a separate collection and not columns on ``FileUpload``.** A session is
not a file. It has no ``file_id`` yet, it may never become one, and it expires;
``FileUpload`` rows are the library and are read by every listing path in the
product. Sessions parked in that collection would have to be filtered out of
every one of those reads, which is the class of rule that silently stops being
followed — and a half-uploaded 5 GB object appearing in someone's library is
exactly the failure the filtering would exist to prevent.

**``size`` is what the CLIENT said.** It is recorded because the ceilings are
checked against it at init (the point of the whole feature: refuse before the
transfer, not after), and it is not trusted at complete — the service re-checks
against what storage actually reports and rolls back if they disagree. Anything
reading this field for a decision after init is reading an untrusted number.

**``budget_day`` is load-bearing.** ``upload_budget`` counts per workspace per
UTC DAY, so a claim made on the 14th must be released against the 14th's row.
The session TTL is 7 days; releasing an expired session's claim against
"today" would decrement a row the claim was never made against and hand out
free quota. The day the claim was made travels with the session.

TTL index on ``expires_at`` with a day of grace rather than zero: expiry is
answered in code as 409 ``multipart.expired`` (with the budget released on the
way past), and a document Mongo reaped the instant it expired would answer 404
instead — a client that reloaded a minute late would be told its upload never
existed rather than that it timed out.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Indexed
from pydantic import BaseModel, Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument

#: Session states. ``open`` is the only one that accepts parts or completes;
#: the other two are terminal and exist so a second DELETE is idempotent and a
#: replayed complete is a 404 rather than a second ``FileUpload`` row.
STATE_OPEN = "open"
STATE_COMPLETED = "completed"
STATE_ABORTED = "aborted"

#: Transfer modes. The client does not choose — ``init`` reports which one the
#: configured storage adapter can actually do and the client obeys it.
MODE_PRESIGNED = "presigned"
MODE_RELAY = "relay"

#: Grace between a session expiring (409) and Mongo reaping the row (404).
_TTL_GRACE_SECONDS = 86400


class MultipartPart(BaseModel):
    """One part that storage has confirmed it holds.

    ``size`` is known on the relay path (we counted the bytes) and ``None`` on
    the presigned path, where the part went browser→bucket and we only ever see
    the etag the client reports. Consumers must treat ``None`` as "unknown",
    never as zero — summing these to recover a size is only honest when every
    part has one.
    """

    part_number: int
    etag: str
    size: int | None = None


class MultipartUpload(TimestampedDocument):
    """Server-side state for one in-flight multipart upload."""

    upload_id: Indexed(str, unique=True)  # type: ignore[valid-type]
    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    storage_key: str
    #: The STORAGE provider's id (S3's ``UploadId``), not ours. Handed back to
    #: the adapter on every part, complete and abort.
    provider_upload_id: str
    filename: str
    mime: str
    #: Client-declared. Untrusted after init — see the module docstring.
    size: int
    part_size: int
    part_count: int
    parts: list[MultipartPart] = Field(default_factory=list)
    chat_id: str | None = None
    pocket_id: str | None = None
    folder_path: str = "/"
    mode: str = MODE_PRESIGNED
    state: str = STATE_OPEN
    #: What was claimed against the daily upload budget at init, and the UTC day
    #: it was claimed on, so a release lands on the right row.
    budget_files: int = 0
    budget_bytes: int = 0
    budget_day: str = ""
    #: False once the claim has been given back, so abort-then-expire (or two
    #: aborts racing) cannot refund the same bytes twice.
    budget_held: bool = True
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "multipart_uploads"
        indexes = [
            # The only read shape there is: one session, scoped to its tenant.
            # Compound rather than relying on the unique ``upload_id`` index
            # alone, because the workspace filter is the tenant boundary and it
            # is on every read.
            [("workspace", 1), ("upload_id", 1)],
            # Resume listing: a client asking "what is still open for me".
            [("workspace", 1), ("owner", 1), ("state", 1)],
            IndexModel([("expires_at", 1)], expireAfterSeconds=_TTL_GRACE_SECONDS),
        ]
