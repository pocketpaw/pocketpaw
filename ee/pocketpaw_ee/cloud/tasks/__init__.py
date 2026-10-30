# __init__.py — Tasks entity package marker.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend.
#   Re-exports the router so ``ee.cloud.__init__:mount_cloud`` can mount
#   it the same way every other 4-file-shape entity is wired.
from pocketpaw_ee.cloud.tasks.router import router  # noqa: F401
