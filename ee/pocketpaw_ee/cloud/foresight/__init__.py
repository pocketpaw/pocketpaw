# ee/pocketpaw_ee/cloud/foresight/__init__.py
# Modified: 2026-05-25 (feat/foresight-v07-cloud-mount) — PR 7. Cloud
#   Foresight package now ships the canonical 4-file shape: domain.py
#   (frozen value objects), dto.py (Pydantic request/response schemas),
#   service.py (Beanie writes, business logic, event emission), and
#   router.py (thin FastAPI endpoints). The Beanie document lives at
#   ``ee.cloud.models.foresight_run`` per the cloud convention; only
#   this entity's service.py imports it (enforced by import-linter).
#   The router is mounted from ``mount_cloud`` alongside cycles_router.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Cloud-side Foresight package — exposes the REST router. The engine
# lives at ee/pocketpaw_ee/foresight/ (a runtime module); this package
# is the thin cloud surface that mounts under /api/v1/foresight/* via
# ee/pocketpaw_ee/cloud/__init__.py:mount_cloud.
