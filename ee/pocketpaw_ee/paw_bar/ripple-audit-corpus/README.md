# Audit corpus

Planted-fault specs shared by ripple core and pocketpaw. Each `*.json` entry is
`{ about, spec, expect: { core: AuditItem[], python: { code, path }[], warnings? } }`,
with `path` a JSON Pointer from the spec root.

- `core` is exactly what `specAudit(spec, { widgets, now })` reports, every
  report's items in order (`src/core/spec-audit.test.ts`, which fixes `now` at
  2026-10-13T12:00:00Z and copies the full manifest's entries for the widgets
  used here). `warnings`, when present, is exactly the reports' warnings
  (unknown props), which never count.
- `python` is exactly what pocketpaw's strict walk reports on the ripple
  profile (`tests/cloud/test_paw_bar_audit_corpus.py`). The walk stops at its
  first fault, so the list holds at most one item.

Every check `specAudit` runs has at least one entry that fails without it.
The two sides may differ; the difference is the record of where they drift.
Leftovers, each, bind, expr, two controls, size and future dates are core-only
today, so their `python` list is empty. Pocketpaw vendors this directory to
`paw_bar/ripple-audit-corpus/` with a `.source` stamp. Use only widget types
both catalogs know (`card`, `text`, `button`, `quiz`, `input`, `each`), except
where the fault is the unknown type itself.
