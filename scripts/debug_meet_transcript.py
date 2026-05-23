"""Debug helper: hit Google's Meet API directly to see what's actually there.

Usage:
    cd backend
    uv run python scripts/debug_meet_transcript.py <workspace_id> [space_name_or_meeting_title]

If the second arg starts with ``spaces/`` it's used as-is; otherwise the
script looks up your most recent meeting whose title contains that
string and uses its space.

Output walks the full Meet hierarchy:
    space (created OK?)
      └── conferenceRecords (did anyone actually join?)
            └── transcripts (was 'Take transcript' enabled?)
                  └── transcriptEntries (did Meet hear any words?)

Anything missing at a given level tells you exactly where the chain
broke. The on-demand fetch path can't surface what Google doesn't have.
"""

from __future__ import annotations

import asyncio
import sys

from pocketpaw_ee.cloud.meetings.providers.recall.clients.google_meet import (
    GoogleMeetAPIError,
    GoogleMeetClient,
)
from pocketpaw_ee.cloud.models.meeting import Meeting as _MD
from pocketpaw_ee.cloud.models.meeting import MeetingProviderCredentials as _CD

from pocketpaw.clients.token_store import TokenStore


async def main(workspace_id: str, lookup: str) -> int:
    # Resolve credentials (matches the meetings adapter factory).
    creds_doc = await _CD.find_one(_CD.workspace == workspace_id, _CD.provider == "google_meet")
    if creds_doc is None:
        print("ERROR: no Google Meet credentials configured for this workspace.")
        return 1
    service = f"workspace-{workspace_id}-google_meet"
    tokens = TokenStore().load(service)
    if tokens is None or not tokens.extra.get("client_id"):
        print("ERROR: token blob missing or incomplete — reconnect Meet.")
        return 1
    client = GoogleMeetClient(
        service_name=service,
        client_id=tokens.extra["client_id"],
        client_secret=tokens.extra["client_secret"],
    )

    # Resolve the space name to inspect.
    if lookup.startswith("spaces/"):
        space_name = lookup
    else:
        meeting = await _MD.find_one(
            _MD.workspace == workspace_id,
            _MD.provider == "google_meet",
            {"title": {"$regex": lookup, "$options": "i"}},
        )
        if meeting is None:
            print(f"ERROR: no meeting whose title matches '{lookup}'")
            return 1
        space_name = meeting.provider_meeting_id
        print(f"Found meeting '{meeting.title}' → space {space_name}")

    print("\n── 1. Space ─────────────────────────────────────────────")
    try:
        space = await client.get_space(space_name)
        print(f"  exists. meetingUri: {space.get('meetingUri')}")
    except GoogleMeetAPIError as e:
        print(f"  FAILED: {e}")
        return 1

    print("\n── 2. Conference records (did anyone join?) ────────────")
    try:
        records = await client.list_conference_records(filter_=f'space.name="{space_name}"')
    except GoogleMeetAPIError as e:
        print(f"  FAILED: {e}")
        return 1
    rec_list = records.get("conferenceRecords", [])
    print(f"  found {len(rec_list)}")
    if not rec_list:
        print("  → Nobody joined this space, OR the conferenceRecord is")
        print("    still propagating (try again in a few minutes).")
        return 0

    for record in rec_list:
        record_name = record["name"]
        print(f"\n  ── {record_name} ──────────────────────────────")
        print(f"    started: {record.get('startTime')}")
        print(f"    ended:   {record.get('endTime')}")

        print("\n    ── 3. Transcripts (was 'Take transcript' ON?) ──")
        try:
            tx = await client.list_transcripts(record_name)
        except GoogleMeetAPIError as e:
            print(f"      FAILED: {e}")
            continue
        sessions = tx.get("transcripts", [])
        print(f"      found {len(sessions)}")
        if not sessions:
            print("      → Transcription was NOT enabled during this call.")
            print("        (Or your Workspace tier doesn't include it.)")
            continue

        for t in sessions:
            print(f"\n      ── {t['name']}")
            print("\n        ── 4. Entries (did Meet hear words?) ──")
            try:
                entries = await client.list_transcript_entries(t["name"])
            except GoogleMeetAPIError as e:
                print(f"          FAILED: {e}")
                continue
            ent_list = entries.get("transcriptEntries", [])
            print(f"          found {len(ent_list)}")
            for e in ent_list[:5]:
                speaker = e.get("participant", "?")
                text = e.get("text", "")[:60]
                print(f"          [{speaker}] {text}")
            if len(ent_list) > 5:
                print(f"          ... +{len(ent_list) - 5} more")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
