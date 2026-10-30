<!-- Visual smoke-test runbook — cloud agent artifacts → per-tenant blob.
     Created 2026-06-27. Covers ART-1..4 (file_versions spine, cwd jail,
     jail lifecycle, deliver_artifact + boot guard). For the captain's
     live smoke before merging integration/cloud-agent-artifacts → dev. -->

# Cloud Agent Artifacts → Per-Tenant Blob — Visual Smoke Test

What this proves end to end: when a cloud user asks the agent to build something
downloadable, the artifact lands in **that tenant's** blob storage, comes back as
a **real download link** in their Files, stays **isolated** from other tenants,
and the agent's scratch filesystem is **bounded** so it can't fill the box.

This replaces the old broken behavior (agent writes to the shared
`/home/pocketpaw/...` and serves a preview from `127.0.0.1`, which the cloud user
can't reach and which co-mingles across tenants).

---

## 0. Prerequisites

```
[ ] Cloud backend running (multi-tenant: CLOUD_MONGODB_URI set, init_cloud_db ran)
[ ] S3 storage configured:  POCKETPAW_UPLOAD_ADAPTER=s3  +  S3_PRIVATE_BUCKET,
    S3_REGION, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY  (S3_ENDPOINT for R2/MinIO)
[ ] Two cloud workspaces with a logged-in user each (call them WS-A and WS-B)
```

**Boot-guard check (do this first):** start the backend and watch the logs.
- On S3: no storage warning. Good.
- On the default local adapter in cloud: you'll see
  `WARNING ... cloud is not using the S3 upload adapter ...`. That's the guard
  telling you artifacts would hit local disk. Set the S3 env, or set
  `POCKETPAW_REQUIRE_S3_IN_CLOUD=1` to make a misconfigured deploy refuse to boot.

---

## 1. The headline test — downloadable landing page (WS-A)

In WS-A chat:

> build me a downloadable html/css/js landing page

```
EXPECT                                              FAILURE SIGNAL
──────────────────────────────────────────────     ─────────────────────────────
[ ] agent replies with a real download LINK         a /home/pocketpaw/... path,
    (https… presigned S3 URL)                        or a 127.0.0.1:<port> link
[ ] the file shows up in WS-A → Files                nothing appears in Files
[ ] clicking the link DOWNLOADS the zip/file         the browser RENDERS it inline
    (does not render in the browser)                  (that's the XSS guard failing)
```

The agent should narrate something like "I've saved it to your Files — download
here: <link>". Under the hood it called the `deliver_artifact` tool, which zipped
the project and uploaded it to `s3://…/<storage-key>` scoped to WS-A.

---

## 2. Per-tenant isolation (WS-A vs WS-B)

```
[ ] In WS-B → Files: WS-A's artifact is NOT listed
[ ] WS-A's presigned link, opened while authenticated as WS-B's user, is denied
    (404 / not found — not B silently downloading A's file)
[ ] In WS-B, build a different artifact; confirm A and B each see only their own
```

This is the core multi-tenant property. Any cross-visibility here is a stop-ship.

---

## 3. Jail isolation on the box (optional, if you have shell access)

While an agent run is active in WS-A, on the backend host:

```
[ ] WS-A's agent files are under  ~/.pocketpaw/workspaces/<WS-A-id>/agent/<session>/
[ ] NOT in  /home/pocketpaw/  (the old shared dir)
[ ] WS-B has its own  ~/.pocketpaw/workspaces/<WS-B-id>/agent/...  — separate tree
```

Note this sits alongside the ISO store isolation already on dev
(`~/.pocketpaw/workspaces/<ws>/fabric.db`, `instinct.db`) — same per-workspace
parent, the agent dir is the new sibling.

---

## 4. The "downloadable HTML can't XSS" check (the security fix)

Ask WS-A:

> save this as an index.html and give me the link:  <!doctype html><script>alert(document.domain)</script>

```
[ ] the returned presigned link DOWNLOADS index.html (Content-Disposition: attachment)
[ ] it does NOT execute / show an alert when opened
```

If the html renders/executes from the storage origin, the attachment-disposition
fix regressed — stop and flag it.

---

## 5. Boundedness (optional, advanced)

```
[ ] Set POCKETPAW_AGENT_JAIL_QUOTA_MB low (e.g. 5) and restart
[ ] Ask the agent to create a large file / many files exceeding it
[ ] The run is rejected cleanly with an "over quota" message — the worker keeps
    running, the box does not fill or crash
```

The TTL/watermark garbage collection runs on the 5-minute sweep; idle jails get
reclaimed automatically, and a jail backing an active run is never evicted.

---

## What "pass" looks like

A cloud user gets a real, private, downloadable link for anything the agent
builds; another tenant can't see or fetch it; the agent's scratch never touches
the shared home dir and can't fill the box; and a delivered `.html` downloads
instead of executing.

## Known non-issues

- `tests/cloud/uploads/` shows ~23 pre-existing red tests (auth-harness 401s in
  the upload router / pocket-ACL / folders suites). They fail identically on
  `dev` without this change and are unrelated — the deliver, jail, lifecycle, and
  bootstrap suites are green.

## Reference — env flags

| Flag | Default | Effect |
|---|---|---|
| `POCKETPAW_UPLOAD_ADAPTER` | `local` | set `s3` for cloud blob storage |
| `POCKETPAW_REQUIRE_S3_IN_CLOUD` | unset (warn) | set truthy → hard-fail boot if cloud + non-s3 |
| `POCKETPAW_AGENT_JAIL_QUOTA_MB` | `2048` | per-workspace scratch cap |
| `POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS` | `3600` | idle-jail reap grace |
| `POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT` | `90` | disk % that triggers LRU eviction |
| `POCKETPAW_AGENT_JAIL_GC_ENABLED` | `true` | master switch for the jail sweeper |
