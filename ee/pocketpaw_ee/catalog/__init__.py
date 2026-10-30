# ee/pocketpaw_ee/catalog/__init__.py — Model Catalog entity package marker
# (MCG-1). The catalog reads the models a self-hosted LiteLLM proxy serves
# (GET /v1/models + GET /model/info), maps each onto a ModelCatalogEntry grouped
# by modality, optionally enriches logo/description from models.dev, TTL-caches
# the assembled set in-process, and exposes it over GET /api/v1/catalog/models[/{id}].
# Plain marker — the router is re-exported by ee.cloud.__init__:mount_cloud.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1): new entity package.
