# ee/pocketpaw_ee/cloud/foresight/__init__.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
# Cloud-side Foresight package — exposes the REST router. The engine
# lives at ee/pocketpaw_ee/foresight/ (a runtime module); this package
# is the thin cloud surface that mounts under /api/v1/foresight/* via
# ee/pocketpaw_ee/cloud/__init__.py:mount_cloud.
#
# v0.1 ships a 2-file surface (dto.py + router.py) and writes to the
# foresight engine's in-memory RunStore. v1.0 adds domain.py +
# service.py per the cloud 4-file rule once Mongo persistence lands.
