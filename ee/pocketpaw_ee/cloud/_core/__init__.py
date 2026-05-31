"""Cross-cutting framework for the cloud module.

Underscore prefix signals that this is internal infrastructure: routers
and services inside `ee/cloud/<module>/` may import from here, but code
outside `ee/cloud/` should not.

See `docs/superpowers/specs/2026-04-27-ee-cloud-restructure-design.md`
for the architectural rationale.
"""
