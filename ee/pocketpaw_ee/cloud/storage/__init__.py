# ee/pocketpaw_ee/cloud/storage/__init__.py — Storage entity package marker.
# Created 2026-08-08 (feat/billing-storage-caps): the workspace S3 STORAGE
# metering entity — used-bytes aggregation over the live ``FileUpload`` docs
# (the Files → Knowledge Base store) + the plan cap gate + the read surface
# (GET /storage/usage). The 4-file entity shape (domain / service / dto /
# router) lives here. Plain marker — nothing is imported eagerly so importing
# this package never pulls FastAPI / Beanie.
