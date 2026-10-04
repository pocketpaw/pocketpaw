# PartnerApplication Beanie document — one public "join Paw Partners" form submission.
#
# Filed by POST /partners/apply (``partners.service_admin.apply``) after Turnstile
# passes; read and reviewed by operators through GET / PATCH
# /platform/partners/applications (``cloud/platform/partners.py``, via the same
# service_admin). It belongs to the platform, not to any workspace: the applicant
# has no tenant yet. ``source_ip_hash`` is sha256 of the submitting address, a
# dedupe / abuse key and NOT anonymisation (an IPv4 space is enumerable).
# ``status`` moves new -> contacted | rejected | accepted; ``reviewed_by`` is the
# operator's user id. Only ``partners.service_admin`` imports this doc
# (import-linter "PartnerApplications" contract).

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument

PartnerApplicationStatus = Literal["new", "contacted", "rejected", "accepted"]


class PartnerApplication(TimestampedDocument):
    """One partner application, from the public form to an operator's decision."""

    name: str
    email: str
    city: str
    country: str
    services: list[str] = Field(default_factory=list)
    message: str = ""
    source_ip_hash: str | None = None
    status: PartnerApplicationStatus = "new"
    note: str = ""
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    class Settings(TimestampedDocument.Settings):
        name = "partner_applications"
        indexes = [
            # The operator list: by status, newest first; also the daily-cap count.
            IndexModel([("status", 1), ("_id", -1)]),
            IndexModel([("createdAt", -1)]),
        ]


__all__ = ["PartnerApplication", "PartnerApplicationStatus"]
