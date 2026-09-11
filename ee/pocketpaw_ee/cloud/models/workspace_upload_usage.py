# ee/pocketpaw_ee/cloud/models/workspace_upload_usage.py — the per-workspace
# daily upload counter.
#
# Created 2026-09-11 (feat/abuse-budgets) — the storage cap in
# ``cloud/storage/service.py`` is gated on ``billing_enforced``, which is False
# by default, so a deployment that has not switched billing on has NO
# per-workspace bound on uploads at all. This counter is the bound that does
# not depend on billing being configured.
#
# One row per workspace per UTC day, keyed on ``<workspace>:<YYYY-MM-DD>`` so
# the only operation it serves — "claim N files and M bytes for this workspace
# today" — is one atomic upsert rather than a read followed by a write.
#
# TWO counters in one row on purpose. A file-count cap alone is beaten by fifty
# 25 MiB files; a byte cap alone is beaten by a hundred thousand 1-byte files,
# each of which still costs a Mongo row, an S3 object and a FileReady event.
# Both move in the same ``$inc``, so they cannot drift apart.
#
# Lives in ``cloud.models`` rather than beside its service because the cloud
# rules keep every Beanie document here and let exactly one module import each
# one. See ``file_comprehension_usage`` for what happens otherwise.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceUploadUsage(TimestampedDocument):
    """How many files, and how many bytes, one workspace has uploaded today.

    ``used`` / ``bytes_used`` rather than ``count`` / ``size``: ``count``
    shadows a query method on the parent Document, and a cap whose field
    silently resolves to a method is the shape of bug that reads as "the
    ceiling works most of the time".
    """

    #: ``<workspace>:<YYYY-MM-DD>``. UNIQUE — the upsert keys on it.
    key: Indexed(str, unique=True)  # type: ignore[valid-type]
    workspace: str
    day: str
    used: int = 0
    bytes_used: int = 0

    class Settings:
        name = "workspace_upload_usage"
