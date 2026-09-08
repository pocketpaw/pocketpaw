<!-- Operator runbook for moving the Studio generation history off the legacy
     JSONL file and into Mongo. Created 2026-09-08 (feat/studio-history-store). -->
# Studio history: JSONL → Mongo

The `/studio` generation history used to live in a single append-only file,
`~/.pocketpaw/studio/generations.jsonl`, shared by the whole deployment with the
owning workspace stored as a `_workspace` field filtered in Python on read. It is
now the `studio_generations` Mongo collection, indexed on `workspace`.

## Do I need to run this?

**Yes, if the deployment has ever generated anything on `/studio`.** The file sits
on the `backend-data` named volume, so it survived every redeploy and holds real
galleries. Without the import those galleries render **empty** — nothing errors,
the tiles are simply gone.

**No, on a fresh install.** There is no file, the migration reports zero and
exits 0.

## Run it

Containers have no shell, so run it the same way as the credit-wallet migration —
as a one-off command against the same image:

```bash
python -m pocketpaw_ee.cloud.studio.migrate_generations_jsonl --dry-run
python -m pocketpaw_ee.cloud.studio.migrate_generations_jsonl
```

It reads `CLOUD_MONGODB_URI` (falling back to `POCKETPAW_MONGO_URL`), the same
variables `init_cloud_db` reads, so the migration and the app cannot point at
different databases.

**It is idempotent.** A record already present under its `(workspace,
generation_id)` is counted as "already present" and not rewritten, so a re-run
converges and an interrupted run can simply be repeated. It does **not** delete
the JSONL — the file stays as a copy while the change is young.

**Run it once, not per container.** `backend` and `worker` share the volume;
there is no lock, and the check-then-insert is not atomic, so two simultaneous
runs could race.

## What it deliberately leaves behind

Records with **no `_workspace` tag** are counted and skipped. They cannot be
attributed to anyone — and the old reader's `or _workspace is None` clause meant
they were readable by **every** tenant. Importing one would mean choosing an
owner, and any choice is wrong.

Their disappearance from the gallery is the leak closing, not data loss. The run
reports how many it skipped; if that count is large and someone wants them, they
are still in the JSONL and can be attributed by hand.

Malformed lines are counted separately and stepped over, the same way the old
reader skipped them, so one bad row cannot strand every record after it.

## Verifying

```
migration complete: 128 imported, 0 already present, 3 skipped (untagged), 0 skipped (unreadable)
```

Then open `/studio` as a workspace that had history and confirm the gallery
renders. A second run should report `0 imported, 128 already present`.
