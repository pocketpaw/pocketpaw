# ee/pocketpaw_ee/integrations/providers/newsletter.py: the built-in ``newsletter`` list.
#
# Kind ``builtin``: no connection, the owner just links it to a site. One public
# write action, ``subscribe(email)``: the address is trimmed, lowercased and
# validated, then stored once per site in ``NewsletterSubscriber`` (a unique index
# makes a repeat a no-op). The answer is the same for a new and a repeat address,
# so the endpoint can't be used to test who is on a list.
#
# ``export_csv`` backs the owner's CSV download. Cells that a spreadsheet would
# read as a formula are prefixed with a quote.

from __future__ import annotations

import csv
import io
import re
from typing import Any

from pydantic import Field, field_validator

from pocketpaw_ee.integrations.providers.base import (
    ActionArgs,
    ActionContext,
    ActionSpec,
    BaseProvider,
)

MAX_EMAIL = 254
# Pragmatic shape check: one @, a dotted domain, no spaces or control chars.
_EMAIL_RE = re.compile(
    r"^[^\s@\x00-\x1f\x7f]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class SubscribeArgs(ActionArgs):
    email: str = Field(max_length=MAX_EMAIL)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("Enter a valid email address.")
        return value


async def _subscribe(ctx: ActionContext, args: SubscribeArgs) -> dict[str, Any]:
    from pymongo.errors import DuplicateKeyError

    from pocketpaw_ee.cloud.models.integration import NewsletterSubscriber as Doc

    existing = await Doc.find_one(
        Doc.workspace == ctx.workspace_id, Doc.pocket_id == ctx.pocket_id, Doc.email == args.email
    )
    if existing is None:
        try:
            await Doc(
                workspace=ctx.workspace_id,
                pocket_id=ctx.pocket_id,
                site_id=ctx.site_id,
                email=args.email,
            ).insert()
        except DuplicateKeyError:
            pass  # a concurrent subscribe won the race; same outcome
    return {"subscribed": True}


async def subscriber_count(workspace_id: str, pocket_id: str) -> int:
    from pocketpaw_ee.cloud.models.integration import NewsletterSubscriber as Doc

    return await Doc.find(Doc.workspace == workspace_id, Doc.pocket_id == pocket_id).count()


def _cell(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


async def export_csv(workspace_id: str, pocket_id: str) -> str:
    """Every subscriber of one site as ``email,subscribed_at`` CSV, oldest first."""
    from pocketpaw_ee.cloud.models.integration import NewsletterSubscriber as Doc

    rows = (
        await Doc.find(Doc.workspace == workspace_id, Doc.pocket_id == pocket_id)
        .sort("+createdAt")
        .to_list()
    )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["email", "subscribed_at"])
    for row in rows:
        stamp = row.createdAt.isoformat() if row.createdAt else ""
        writer.writerow([_cell(row.email), stamp])
    return buf.getvalue()


class NewsletterProvider(BaseProvider):
    id = "newsletter"
    name = "Newsletter"
    kind = "builtin"
    category = "newsletter"
    plan = "free"
    actions = {
        "subscribe": ActionSpec(
            args=SubscribeArgs,
            handler=_subscribe,
            description="Add an email address to this site's newsletter list.",
            public=True,
            write=True,
        ),
    }
