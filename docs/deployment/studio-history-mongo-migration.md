<!-- Operator runbook for moving the Studio generation history off the legacy
     JSONL file and into Mongo. Created 2026-09-08 (feat/studio-history-store). -->
# Studio history: JSONL → Mongo

The `/studio` generation history used to live in a single append-only file,
`~/.pocketpaw/studio/generations.jsonl`, shared by the whole deployment with the
owning workspace stored as a `_workspace` field filtered in Python on read. It is
now the `studio_generations` Mongo collection, indexed on `workspace`.

## Do I need to run this? No — it runs itself

The file sits on the `backend-data` named volume, survived every redeploy and
holds real galleries. Without the import those galleries render **empty** —
nothing errors, the tiles are simply gone. That failure mode is exactly why this
is no longer a manual step.

`init_cloud_db` calls `migrate_on_boot()` on every cloud start, beside the
workspace-VM map import. It is best-effort and never blocks a boot: a missing
file returns immediately, and any error is logged and swallowed.

That is deliberate. The alternative was a one-off command a human had to
remember, in an environment with no shell — and forgetting it left every gallery
silently empty, with nothing in the logs to say why.

**Running from both containers is safe.** `backend` and `worker` boot
independently and share the volume. The import does find-then-insert per record
with no lock, which would once have raced; the unique index on
`(workspace, generation_id)` closes it, so a concurrent double-insert raises
`DuplicateKeyError` and the row is applied instead of duplicated.

## Running it by hand

Still useful for a dry run, or to re-run after fixing something:

```bash
python -m pocketpaw_ee.cloud.studio.migrate_generations_jsonl --dry-run
python -m pocketpaw_ee.cloud.studio.migrate_generations_jsonl
```

The CLI suppresses the boot import before it initialises the database — without
that, `--dry-run` would report what it "would" do *after* the automatic import
had already done it.

It reads `CLOUD_MONGODB_URI` (falling back to `POCKETPAW_MONGO_URL`), the same
variables `init_cloud_db` reads, so the migration and the app cannot point at
different databases. It is idempotent, and it does **not** delete the JSONL.

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
