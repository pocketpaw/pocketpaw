# ee/pocketpaw_ee/cloud/models/file_transcription_usage.py — the media
# transcription spend counter.
#
# Created 2026-08-29 (T2 "Audio/video transcription at ingest"). Registered in
# ``cloud.models.__init__`` so ``init_beanie`` wires the collection. That
# registration is not paperwork: without it ``get_pymongo_collection()`` raises
# at claim time, the raise lands in the budget's fail-CLOSED except, every
# transcription is refused, and the feature reads as "transcription is off"
# rather than as a missing line in a list.
#
# A SEPARATE counter from ``FileComprehensionUsage``, deliberately. The two
# gates guard different bills — comprehension is a text-model call per upload,
# transcription is a per-audio-minute charge on a media file — and sharing one
# row would let a bulk photo import exhaust the ceiling that exists to stop a
# podcast library, or the reverse. One meter per thing being metered.
#
# WHY IT LIVES HERE and not beside its service: the cloud rules keep Beanie
# documents in ``cloud.models`` and let exactly one module import each one.
# ``models.other_hand_usage`` records what happens otherwise — a document
# declared in its own service file imports ``models.base`` while
# ``models.__init__`` imports the class back, and the cycle surfaces as an
# unrelated-looking import error somewhere else entirely.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class FileTranscriptionUsage(TimestampedDocument):
    """How many media files one workspace has had transcribed today.

    ``used`` rather than ``count``: ``count`` shadows a query method on the
    parent Document and Pydantic warns about the collision. That matters more
    than tidiness here — the field the whole cap depends on silently resolving
    to a method is exactly the shape of bug that reads as "the ceiling works
    most of the time".
    """

    #: ``<workspace>:<YYYY-MM-DD>``. UNIQUE — the upsert keys on it.
    key: Indexed(str, unique=True)  # type: ignore[valid-type]
    workspace: str
    day: str
    used: int = 0

    class Settings:
        name = "file_transcription_usage"
