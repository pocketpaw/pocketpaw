# ee/pocketpaw_ee/cloud/jobs/__init__.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — package marker for the
# workspace jobs primitive. The 4-file entity shape (domain / dto / service /
# router) plus the registry, the ARQ worker entrypoint, and the built-in jobs
# package live here. The package __init__ stays import-light: the router and
# built-ins are imported explicitly by `mount_cloud`, not eagerly here, so
# importing `pocketpaw_ee.cloud.jobs` never pulls FastAPI / Beanie.

"""Workspace jobs — named, server-side async callables run in the ARQ worker."""

from __future__ import annotations
