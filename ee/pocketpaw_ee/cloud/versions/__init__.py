# ee/pocketpaw_ee/cloud/versions/ — the pocket-content version-history module
# (pocketpaw#1345 Phase 1, plan §5/§6). A draft/published state machine over a
# versions collection (PocketVersion): edit/refine writes a new draft, publish
# promotes the draft to published, rollback clones an old version into a new
# draft. The draft/published pointers are DERIVED from the collection, so no
# Pocket-model change is needed.
#
# This is the foundation Phase 2 (history UI, rollback, diff) builds on. Sites
# layer publish/deploy/Live on top (see ee/pocketpaw_ee/sites/service.py).
# Created 2026-06-06 (feat/1345-draft-published).
