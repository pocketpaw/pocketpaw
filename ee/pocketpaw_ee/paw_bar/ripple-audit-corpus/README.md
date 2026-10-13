# Audit corpus

Planted-fault specs shared by ripple core and pocketpaw. Each `*.json` entry is
`{ about, spec, expect: { core: AuditItem[], python: { code, path }[] } }`, with
`path` a JSON Pointer from the spec root.

- `core` is exactly what `specAudit(spec, { widgetTypes })` reports, every
  report's items in order (`src/core/spec-audit.test.ts`).
- `python` is exactly what pocketpaw's strict walk reports on the ripple
  profile (`tests/cloud/test_paw_bar_audit_corpus.py`). The walk stops at its
  first fault, so the list holds at most one item.

The two sides may differ; the difference is the record of where they drift.
Pocketpaw vendors this directory to `paw_bar/ripple-audit-corpus/` with a
`.source` stamp. Use only widget types both catalogs know (`card`, `text`,
`button`, `quiz`), except where the fault is the unknown type itself.
