# ee/pocketpaw_ee/cloud/models/partner_client.py — Paw Partners client record.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1). A "client" is a shop owner
# a partner (print shop / designer) builds sites for. It is a RECORD inside the
# partner's workspace — not a workspace and not a user. Only
# ``cloud.partners.service`` reads or writes it (import-linter contract
# "Partners — Beanie writes only from service.py").

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class PartnerClient(TimestampedDocument):
    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    whatsapp: str  # E.164, validated at the DTO
    whatsapp_opt_in_at: datetime | None = None
    gstin: str | None = None
    notes: str = ""

    class Settings:
        name = "partner_clients"
        indexes = [IndexModel([("workspace", 1), ("_id", -1)], name="workspace_1__id_-1")]
