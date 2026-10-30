"""Move every site's lead-notification webhook URLs into the integrations vault.

A destination's URL (a Slack / Discord / Teams / Google Chat incoming-webhook URL,
or a JSON endpoint) is a credential. New destinations already store it in the
vault; this moves the ones saved before: the flat pre-list ``webhook_url``,
destinations with their own Fernet ``url_enc``, and old plaintext ``url`` rows.
The work is ``pocketpaw_ee.cloud.leads.webhook_vault.migrate_all``.

OPTIONAL. Every webhook write already moves that site's URLs, and send time reads
both storages, so a database that never runs this keeps working. It finishes the
move for sites nobody edits.

SAFE TO RUN AT ANY TIME, any number of times: a destination already in the vault
is skipped, a vault row is upserted on its ``"<site_id>:<webhook_id>"`` ref, and
each pointer update is conditional on what was read. It prints counts only, never
a URL. Needs the server's ``CLOUD_ENCRYPTION_KEY`` (the vault key) and the key the old
``url_enc`` was written with (``POCKETPAW_SSO_ENCRYPTION_KEY``, else derived from
``AUTH_SECRET``); without the vault key it moves nothing.

Usage (the URI comes from the environment so it stays out of shell history):

    export POCKETPAW_CLOUD_MONGO_URI=...
    uv run python scripts/migrations/2026_10_23_lead_webhooks_to_vault.py
    uv run python scripts/migrations/2026_10_23_lead_webhooks_to_vault.py --apply

Without ``--apply`` it is a dry run: it counts the sites that still hold a URL
outside the vault and writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

_PENDING = {
    "$or": [
        {"webhook_url": {"$nin": [None, ""]}},
        {"webhooks": {"$elemMatch": {"url_enc": {"$nin": [None, ""]}}}},
        {"webhooks": {"$elemMatch": {"url": {"$nin": [None, ""]}}}},
    ]
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually move the URLs")
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable")
    args = parser.parse_args()

    uri = os.environ.get("POCKETPAW_CLOUD_MONGO_URI", "").strip()
    if not uri:
        print("POCKETPAW_CLOUD_MONGO_URI is not set (env, not argument).", file=sys.stderr)
        return 2

    from pocketpaw_ee.cloud.leads import notification_settings as ns
    from pocketpaw_ee.cloud.leads import webhook_vault
    from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

    await init_cloud_db(uri)
    try:
        if args.apply:
            result = await webhook_vault.migrate_all()
        else:
            pending = await ns._coll().count_documents(_PENDING)
            result = {"dry_run": True, "sites_pending": pending, "vault": webhook_vault.enabled()}
    finally:
        await close_cloud_db()

    if args.as_json:
        print(json.dumps(result, indent=2))
    elif not args.apply:
        print(f"DRY RUN: {result['sites_pending']} site(s) hold a webhook URL outside the vault.")
        if not result["vault"]:
            print("CLOUD_ENCRYPTION_KEY is unset: --apply would move nothing.")
    elif result.get("skipped"):
        print("CLOUD_ENCRYPTION_KEY is unset: nothing moved.")
    else:
        print(
            f"Moved {result['moved']} destination(s) over {result['sites']} site(s); "
            f"{result['failed']} failed (re-run to retry)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
