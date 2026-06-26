# bootstrap.py — cloud upload/blob-storage boot guard (ART-4, 2026-06-26).
#
# Mirrors ee/pocketpaw_ee/cloud/memory/bootstrap.py::verify_cloud_memory_backend.
# The memory guard refuses to boot the cloud on a local memory backend (which
# would write chat history to disk); this guard does the same for the upload
# backend so a cloud deploy that left POCKETPAW_UPLOAD_ADAPTER on its local
# default doesn't silently write delivered agent artifacts (deliver_artifact,
# ART-4) to the box's filesystem instead of tenant blob storage.
#
# Warn-then-error: a misconfigured upload backend WARNs loudly by default (the
# common "I forgot to set the adapter" case stays bootable for local/dedicated
# trials), and POCKETPAW_REQUIRE_S3_IN_CLOUD escalates it to a hard boot failure
# for deploys that must never fall back to local disk.
"""ee upload/blob-storage backend boot guard.

Called from ``init_cloud_db`` right after ``verify_cloud_memory_backend`` so a
cloud deployment whose upload adapter is still the local default surfaces it at
startup — as a loud warning by default, or a refused boot when
``POCKETPAW_REQUIRE_S3_IN_CLOUD`` is set.
"""

from __future__ import annotations

import logging
import os

from pocketpaw_ee.cloud.shared.db import is_multi_tenant_cloud

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def _require_s3_in_cloud() -> bool:
    """Whether a non-s3 upload adapter should HARD-FAIL the cloud boot.

    Default ``False`` (warn only) — set ``POCKETPAW_REQUIRE_S3_IN_CLOUD`` to a
    truthy value on a deploy that must refuse to start without S3-backed blob
    storage.
    """
    return os.environ.get("POCKETPAW_REQUIRE_S3_IN_CLOUD", "").strip().lower() in _TRUTHY


def verify_cloud_storage_backend() -> None:
    """Guard the cloud upload/blob backend at startup.

    No-op OFF multi-tenant cloud (OSS / a process that never initialized the
    cloud DB): there is no tenant blob storage to require, so local-disk uploads
    are correct there.

    In cloud, when ``POCKETPAW_UPLOAD_ADAPTER`` is not ``s3`` (the default is
    ``local``), delivered artifacts and uploads land on the box's filesystem
    rather than the tenant's object store — the ``deliver_artifact`` download
    URL would point at a local path that disappears with the container and never
    reaches the tenant's blob storage. That is almost always a misconfigured
    deploy, so:

    * WARN loudly by default (so the operator sees it in the boot logs but a
      local/dedicated trial still comes up), and
    * RAISE — refusing to boot — when ``POCKETPAW_REQUIRE_S3_IN_CLOUD`` is set,
      for deploys that must never silently degrade to local disk.

    Mirrors :func:`verify_cloud_memory_backend`, which refuses to boot the cloud
    on a local memory backend for the same "don't silently write to disk"
    reason.
    """
    if not is_multi_tenant_cloud():
        return

    adapter = os.environ.get("POCKETPAW_UPLOAD_ADAPTER", "local").strip().lower()
    if adapter == "s3":
        return

    message = (
        f"cloud startup: POCKETPAW_UPLOAD_ADAPTER={adapter!r}, expected 's3'. "
        "In cloud, delivered artifacts and uploads must land in tenant blob "
        "storage (S3 / S3-compatible); a local adapter writes them to the box's "
        "filesystem, so deliver_artifact download URLs point at disk that "
        "vanishes with the container. Set POCKETPAW_UPLOAD_ADAPTER=s3 (and the "
        "S3_* settings). Set POCKETPAW_REQUIRE_S3_IN_CLOUD=1 to make this a hard "
        "boot failure."
    )

    if _require_s3_in_cloud():
        raise RuntimeError(message)
    logger.warning(message)
