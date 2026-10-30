# ee/pocketpaw_ee/cloud/llm_provisioning/__init__.py — LLM-provisioning entity
# package marker (MCG-8).
#
# This entity owns the per-tenant LiteLLM virtual-key lifecycle: it mints a
# budgeted, rate-limited virtual key per workspace on the proxy (POST
# /key/generate), persists the workspace -> key mapping (the LiteLLMTenantKey
# doc), and ingests that key's proxy spend (/spend/logs) into the EXISTING credit
# ledger (credits.service.debit) — it does NOT own a ledger, it plugs into BC-1's.
#
# The 3-file shape (domain / service / this marker) lives here; the proxy admin
# client it calls lives in the catalog entity (catalog.admin_client), reusing the
# one deployment proxy-config path. Plain marker — nothing is imported eagerly so
# importing this package never pulls FastAPI / Beanie.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): new entity.

"""Per-tenant LiteLLM virtual-key provisioning + spend -> credits ingestion."""

from __future__ import annotations
