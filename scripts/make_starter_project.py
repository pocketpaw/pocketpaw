# make_starter_project.py — hand-create a scaffolded Code Mode project row.
#
# Why this exists: CS-3 taught the workspace to OPEN a project whose files come
# from a starter, but nothing yet CREATES one — that is CS-4 (the landing page
# still shows a "coming soon" stub). So a manual test needs the row minted by
# hand.
#
# Deletable the day CS-4 lands.
#
# Usage (from the pocketpaw repo root):
#   uv run python scripts/make_starter_project.py --list
#   uv run python scripts/make_starter_project.py --starter react
#
# It writes straight to Mongo, bypassing auth entirely, which is why it is a
# local dev aid and not a route.
from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime

from pymongo import MongoClient

MONGO_URI = os.environ.get("CLOUD_MONGODB_URI", "mongodb://localhost:27017/paw-enterprise")
STARTERS = ("react", "vue", "svelte", "next")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--starter", choices=STARTERS, default="react")
    parser.add_argument("--name", default="", help="display name (defaults to '<starter> starter')")
    parser.add_argument(
        "--list", action="store_true", help="list existing projects and exit, do not create"
    )
    args = parser.parse_args()

    client = MongoClient(MONGO_URI)
    db = client.get_default_database()
    projects = db["code_projects"]

    if args.list:
        for doc in projects.find().sort("updated_at", -1).limit(20):
            print(
                f"{str(doc['_id']):26} provider={doc.get('provider'):9} "
                f"repo={doc.get('repo'):40} name={doc.get('name')}"
            )
        return

    # Reuse the tenancy of whatever project already exists, so the row lands in
    # the workspace the browser session is actually looking at. Guessing the ids
    # produces a row that exists but 404s on every read (tenant filter, Rule 7)
    # — which looks exactly like a broken feature.
    seed = projects.find_one(sort=[("updated_at", -1)])
    if seed is None:
        raise SystemExit(
            "No existing project to copy tenancy from. Open one repo through the "
            "UI first, then re-run this."
        )

    now = datetime.now(UTC)
    doc = {
        "workspace_id": seed["workspace_id"],
        "user_id": seed["user_id"],
        "name": args.name or f"{args.starter} starter",
        # THE bit that matters: `provider` is the discriminator the client reads
        # (core/codeproject/source.ts). "starter" means the files come from the
        # catalog; `repo` carries the starter id rather than a git remote.
        "provider": "starter",
        "repo": args.starter,
        "snapshot_file_id": None,
        "current_sandbox_id": None,
        "last_opened_at": None,
        "created_at": now,
        "updated_at": now,
    }
    existing = projects.find_one(
        {
            "workspace_id": doc["workspace_id"],
            "user_id": doc["user_id"],
            "provider": "starter",
            "repo": args.starter,
        }
    )
    if existing is not None:
        print(f"Already exists — open  /code/{existing['_id']}")
        return

    result = projects.insert_one(doc)
    print(f"Created. Open  http://localhost:1420/code/{result.inserted_id}")


if __name__ == "__main__":
    main()
