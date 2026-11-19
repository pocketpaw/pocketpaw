# ee/pocketpaw_ee/cloud/models/daily_usage.py — the ONE per-subject daily usage
# counter behind every daily cap.
#
# Created 2026-10-01 (fix/canon-daily-caps, CN-3). Replaces six near-identical
# docs that each keyed one counter on ``<subject>:<YYYY-MM-DD>``:
# ``WorkspaceTurnUsage``, ``WorkspaceUploadUsage`` (files + bytes),
# ``FileComprehensionUsage``, ``FileTranscriptionUsage``, ``IllustrationUsage``
# and ``GuestTurnUsage`` (keyed on a USER, not a workspace — hence the subject
# axis). One row per ``(subject_type, subject_id, meter, day)``, claimed with one
# atomic ``$inc`` upsert by ``cloud.metering.service.try_spend``.
#
# New collection: the old per-meter counters are not migrated, so every daily
# counter starts from zero once at deploy. They reset at UTC midnight anyway.
#
# Only ``cloud.metering.service`` imports this class (cloud rule 2).
#
# ``used`` rather than ``count``: ``count`` shadows a query method on the parent
# Document, and a cap whose field resolves to a method mostly works.
#
# Rows expire 90 days after ``createdAt`` (set by ``try_spend``'s
# ``$setOnInsert``). A cap only ever reads today's row, so older rows are
# history nobody queries; 90 days keeps a quarter for support questions.

from __future__ import annotations

from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument

#: How long a day's counter row is kept.
DAILY_USAGE_TTL_SECONDS = 90 * 86400


class DailyUsage(TimestampedDocument):
    """How much of one meter one subject has used on one UTC day."""

    #: ``"workspace"`` or ``"user"``.
    subject_type: str
    subject_id: str
    #: A ``metering.domain.DailyMeter`` value.
    meter: str
    #: ``YYYY-MM-DD`` (UTC).
    day: str
    used: int = 0

    class Settings:
        name = "daily_usage"
        indexes = [
            # UNIQUE — the upsert keys on the full tuple, so two concurrent
            # first-claims cannot mint two rows.
            IndexModel(
                [("subject_type", 1), ("subject_id", 1), ("meter", 1), ("day", 1)],
                unique=True,
                name="uq_daily_usage_subject_meter_day",
            ),
            IndexModel([("createdAt", 1)], expireAfterSeconds=DAILY_USAGE_TTL_SECONDS),
        ]


__all__ = ["DailyUsage"]
