<!--
VIP Onboarding Phase B — Manual Smoke Test Checklist
Created 2026-06-08 alongside the cross-cutting e2e suite
(tests/cloud/test_phaseb_e2e.py). Internal working aid for the captain's
local walk-through of the assembled Phase B feature: a workspace member
connects their own Gmail/Calendar at onboarding, lands on home, and the agent
greets them knowing their day + an intent board shows it — while a SECOND
member sees none of it. Grounded in the feat/phaseb-e2e branch code
(member_ingest, member_day_digest, chat gate, kb scope validator, purge).
-->

# VIP Onboarding Phase B — Manual Smoke Test

**Date:** 2026-06-08  **Branch:** `feat/phaseb-e2e` (full Phase B backend stack)

This walks the assembled VIP-onboarding flow end to end by hand: an admin
invites a member, the member connects their **own** Gmail + Calendar during
onboarding, lands on home, and the agent greets them already knowing their day
while the intent board renders it. Then a **second** member proves the
isolation: none of the first member's data reaches them through any door.

> **What the automated suite already covers (so you don't re-test it):**
> `tests/cloud/test_phaseb_e2e.py` proves the leak-prevention across every door
> (chat gate, REST kb router, digest API, shared room) plus the happy path and
> purge, with the kb-go subprocess and the Gmail/Calendar reads mocked. This
> checklist is the **real-stack** pass — live OAuth, a real kb binary, the
> actual UI — that mocks can't give you. Run the suite first; treat this as the
> confirmation that the wiring holds with real I/O.

---

## 0. What you're verifying

| # | Behaviour | "Working" looks like |
|---|-----------|----------------------|
| **A** | Member connects own Gmail/Calendar at onboarding | OAuth completes; a **per-user** (`scope="user"`) connector row appears for that member |
| **B** | First ingest lands in the member's private KB | Their `user:{id}` KB scope fills with recent mail + upcoming events (no other member's) |
| **C** | Agent greets the member knowing their day | In the member's **own** chat, the agent proactively references today's meetings / unread mail |
| **D** | Intent board shows the member's day | `GET /api/v1/member-day-digest` returns the member's events + unread count; the board renders it |
| **E** | **Isolation** — a second member sees none of it | Member B's chat, KB search, digest, and any shared room contain **zero** of A's data |
| **F** | Disconnect / offboard purges everything | After `/me/disconnect`, A's `user:{id}` KB is empty, tokens gone, digest empty |

---

## 1. Prerequisites

### 1a. Google OAuth credentials (the real-path prerequisite)

The member-ingest worker and the digest both read with the member's **own**
per-user Google token. For a true end-to-end pass you need Google OAuth set up
so a member can actually authorize Gmail + Calendar:

- A Google Cloud OAuth client (`POCKETPAW_GOOGLE_CLIENT_ID` /
  `POCKETPAW_GOOGLE_CLIENT_SECRET`) with the Gmail readonly + Calendar
  readonly scopes enabled, and your redirect URI registered.
- Per-user tokens are stored under the service keys `google_gmail` and
  `google_calendar`, bucketed by the member's cloud user id (this is what the
  per-user `GmailClient(user_id)` / `CalendarClient(user_id)` resolve).

**No OAuth creds?** Use the **manual-seed fallback** in §1c — you can exercise
B, C, E, and F (the KB-scope side of the chain) without ever touching Google.
Only the live-digest pieces of C/D (the "right now" calendar + unread count)
need real tokens.

### 1b. kb-go binary

The private KB scope is a real kb-go scope. Make sure the `kb` binary is on
`PATH` or point at it explicitly:

```bash
export POCKETPAW_KB_BIN=/path/to/kb-go/kb
kb --help   # sanity
```

### 1c. Manual-seed fallback (test the KB chain without OAuth)

You can prove the whole KB side of the feature — ingest target, the agent's
private retrieval, the leak gate, and purge — by dropping a doc straight into a
member's `user:{id}` scope with the keyless `accept` path (no LLM, no API key):

```bash
# Replace MEMBER_ID with the member's cloud user id (see §2 for how to get it).
echo '{"scope":"user:MEMBER_ID","articles":[{
  "title":"Email: Re: PROJECT-NIGHTINGALE merger",
  "content":"From: ceo@acme.test\n\nConfidential: PROJECT-NIGHTINGALE closes Friday.",
  "summary":"merger closes Friday",
  "categories":["email","gmail"]
}]}' | "$POCKETPAW_KB_BIN" accept --scope "user:MEMBER_ID" --json

# Confirm it landed:
"$POCKETPAW_KB_BIN" search "PROJECT-NIGHTINGALE" --scope "user:MEMBER_ID" --json
```

This is exactly what `member_ingest.service` does internally on a real connect,
so a seeded scope is indistinguishable from an ingested one for steps B/C/E/F.

---

## 2. Bring up the stack & get two members

```bash
# Backend (cloud / ee). From the repo root:
uv run pocketpaw serve          # binds 127.0.0.1:8888 by default
```

You need **two** members in one workspace:

1. As **admin**, invite a member (`POST /api/v1/workspaces/{ws}/invites`) — this
   is **Alice**. Accept the invite as Alice in a second browser/profile.
2. Invite a second member — this is **Bob**. Accept as Bob.

Grab each member's **user id** and a bearer token. The id is what keys the
`user:{id}` scope and the per-user token bucket. Easiest source:

```bash
# As each member, hit /me and note the id field:
curl -s -H "Authorization: Bearer $ALICE_TOKEN" http://127.0.0.1:8888/api/v1/me
curl -s -H "Authorization: Bearer $BOB_TOKEN"   http://127.0.0.1:8888/api/v1/me
```

Keep `ALICE_ID`, `BOB_ID`, `ALICE_TOKEN`, `BOB_TOKEN`, and the workspace id
(`WS`) handy.

---

## 3. The walk-through

### A — Alice connects her own Gmail + Calendar (onboarding)

**UI path:** As Alice, go through onboarding / Settings → Connectors and
connect **Gmail** and **Google Calendar** as *your* accounts. Complete the
Google OAuth consent for each.

**API check** — a per-user connector row now exists for Alice:

```bash
# The enable step records intent + scope; OAuth itself runs via the
# oauth_integrations flow during the UI connect.
curl -s -H "Authorization: Bearer $ALICE_TOKEN" \
  http://127.0.0.1:8888/api/v1/connectors | python3 -m json.tool
```

- [ ] **A1** Gmail + Calendar show as connected for Alice, at `scope="user"`.
- [ ] **A2** They are connected to **Alice's** Google account, not a shared one.

> No OAuth creds? Skip A and use the §1c seed for Alice's `user:{ALICE_ID}`
> scope. B's KB assertions, C's retrieval, E, and F all still hold.

### B — First ingest fills Alice's private KB

The sweep runs on an interval (default **5 min**; override with
`POCKETPAW_MEMBER_INGEST_INTERVAL_SECONDS`). To avoid waiting, trigger one
ingest tick directly, or just wait for the scheduler.

```bash
# One sweep tick across all connected members (or call ingest_member for Alice).
# (From a Python shell against the running app, or wait for the 5-min loop.)
python3 - <<'PY'
import asyncio
from pocketpaw_ee.cloud.member_ingest import service
print(asyncio.run(service.ingest_member("WS", "ALICE_ID")))
PY

# Confirm Alice's mail/calendar text is searchable in HER scope:
"$POCKETPAW_KB_BIN" search "merger" --scope "user:ALICE_ID" --json
```

- [ ] **B1** Alice's `user:{ALICE_ID}` scope now carries recent mail + upcoming
  events (the ingest result reports `status:ok`, `documents > 0`).
- [ ] **B2** Nothing landed in any **other** member's scope
  (`kb search "<alice term>" --scope user:BOB_ID` → empty).

### C — Agent greets Alice knowing her day

**UI path:** As **Alice**, open her **own** chat (a solo session — just her and
the agent). Send a neutral opener like "hey".

- [ ] **C1** The agent's reply is **anticipatory** — it references today's
  meetings and/or her unread mail without being asked (a `<your-day>` briefing
  was injected into its prompt). Ask "what's on my plate today?" if the opener
  was too generic.
- [ ] **C2** Ask the agent something only the ingested mail would know (e.g.
  "what did the CEO say about the merger?"). It answers from her private KB.

### D — Intent board shows Alice's day

**API check** — the digest the board renders, for the **authenticated** caller:

```bash
curl -s -H "Authorization: Bearer $ALICE_TOKEN" \
  http://127.0.0.1:8888/api/v1/member-day-digest | python3 -m json.tool
```

- [ ] **D1** Returns `member_id == ALICE_ID`, with `events[]` (next ~7 days)
  and `unread_mail_count`.
- [ ] **D2** The intent board UI on Alice's home renders those events + the
  unread count.

> Note: the digest is a **live pull** (current calendar + unread), distinct
> from the ingested KB. With seeded-only data (no OAuth), the digest is empty —
> that's correct; D needs real tokens. B/C's KB side does not.

### E — Isolation: Bob sees NONE of Alice's data (the centerpiece)

Do all of these as **Bob**.

- [ ] **E1 — chat:** In Bob's **own** chat, the agent does **not** know
  anything about Alice's day or her mail. Ask "what's on the merger?" → it has
  no idea (Bob's private scope is empty / only his own).
- [ ] **E2 — REST kb (read):** Bob tries to read Alice's private scope directly
  → **403**, never any of Alice's content:

  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Bearer $BOB_TOKEN" -H "Content-Type: application/json" \
    -X POST http://127.0.0.1:8888/api/v1/kb/search \
    -d '{"query":"merger","scope":"user:ALICE_ID"}'
  # expect: 403
  ```

- [ ] **E3 — REST kb (write/poison):** Bob tries to ingest into Alice's scope →
  **403**, and Alice's scope is unchanged:

  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Bearer $BOB_TOKEN" -H "Content-Type: application/json" \
    -X POST http://127.0.0.1:8888/api/v1/kb/ingest/text \
    -d '{"text":"poison","source":"evil","scope":"user:ALICE_ID"}'
  # expect: 403
  ```

- [ ] **E4 — digest API:** Bob's digest is **his own**, never Alice's. There is
  no `member_id` parameter to abuse:

  ```bash
  curl -s -H "Authorization: Bearer $BOB_TOKEN" \
    http://127.0.0.1:8888/api/v1/member-day-digest | python3 -m json.tool
  # member_id must be BOB_ID; none of Alice's content present.
  ```

- [ ] **E5 — shared room:** Put Alice + Bob in a **shared** group/room with an
  agent. In that room the agent gets **no** member-private briefing for anyone
  — Alice's `<your-day>` does not appear, and neither member's private KB is
  searched. (Confirm the agent can't surface Alice's merger detail in the
  shared room even when Alice is present.)

### F — Disconnect / offboard purges everything

As **Alice**, disconnect her accounts (UI: Settings → Connectors → Disconnect;
or the API below). This runs the full purge.

```bash
curl -s -H "Authorization: Bearer $ALICE_TOKEN" \
  -X POST http://127.0.0.1:8888/api/v1/connectors/me/disconnect | python3 -m json.tool
```

- [ ] **F1** Response shows `status:ok`, `kb_cleared:true`, and the token /
  connector / ingest-state counts.
- [ ] **F2** Alice's KB scope is empty: `kb search "merger" --scope
  user:ALICE_ID --json` → no hits.
- [ ] **F3** Alice's digest is now empty (`empty:true`, no events, unread 0),
  and her chat agent no longer greets her with a day briefing.
- [ ] **F4** Re-running the disconnect is a clean no-op (idempotent).

---

## 4. Pass/fail summary

| Check | Pass? | Notes |
|-------|:-----:|-------|
| A1 per-user Gmail/Calendar connected |  |  |
| A2 connected to Alice's own account |  |  |
| B1 ingest fills Alice's `user:` KB |  |  |
| B2 nothing in other members' scopes |  |  |
| C1 agent gives proactive day briefing |  |  |
| C2 agent answers from private KB |  |  |
| D1 digest API returns Alice's day |  |  |
| D2 intent board renders it |  |  |
| **E1 Bob's chat has none of Alice's data** |  |  |
| **E2 Bob → Alice KB read = 403** |  |  |
| **E3 Bob → Alice KB write = 403** |  |  |
| **E4 Bob's digest is his own only** |  |  |
| **E5 shared room: no private briefing** |  |  |
| F1 disconnect purge ok |  |  |
| F2 KB scope empty after purge |  |  |
| F3 digest + briefing empty after purge |  |  |
| F4 disconnect idempotent |  |  |

---

## 5. Expected-not-bugs

- **A member with no connected accounts gets an *empty* digest, not an error.**
  `GET /api/v1/member-day-digest` returns `empty:true` and the chat agent simply
  omits the `<your-day>` block — it behaves exactly as a pre-Phase-B agent.
- **The digest can be empty while the KB has data** (and vice-versa). The digest
  is a live pull from the per-user clients; the KB is the ingested snapshot.
  After a disconnect the live digest empties immediately; the KB empties via the
  purge. Seeded-only (no-OAuth) testing shows KB data but an empty digest — both
  correct.
- **A 403 on a foreign `user:` scope is the feature, not a failure.** The kb
  router binds every client `scope` override to the caller; a denial is also
  written to the RBAC audit log at ALERT.
- **The intent board / briefing only appears in a member's *own* solo session.**
  Shared and multi-member rooms intentionally show no member-private day data.
