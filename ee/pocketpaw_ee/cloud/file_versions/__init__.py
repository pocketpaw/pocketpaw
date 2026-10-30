# __init__.py — file_versions entity package.
# Created: 2026-06-26 (ART-1) — versioned cloud file-write storage spine
#   ported from dewani12's origin/feature/files. Holds the file-version
#   core only (write/update/list/get); slides/spreadsheet/editor (Slice D)
#   were intentionally excluded from the port.
# Updated: 2026-07-03 (FL-2, port of #1193) — completed the history spine:
#   revert + unified-diff service helpers and routes, the DiffResponse DTO, and
#   the cohesive slides_dto / spreadsheet_dto transport modules (their AI-edit
#   routes still land in FL-5). Stale If-Match now returns 412.
