# ee/pocketpaw_ee/versions/__init__.py
# Created: 2026-06-18 (feat/branch-primitive-versions, BP-1) — package init
# for the universal "Branch primitive" versions core: propose → branch →
# review → merge/publish + diff + revert, for ANY versionable artifact.
#
# BP-1 scope (this package today): the version MODEL (ArtifactVersion) and
# the version SERVICE spine (write_draft / branch / publish + pointer reads).
# It is EE-cloud-only for now but the model + state machine are kept
# artifact-generic (NOT sites-specific) so the whole thing can be lifted into
# an OSS protocol later. ``scope_type`` is the genericity seam — the only
# wired value today is ``"pocket"`` and other scope types slot in with no
# model change.
#
# BP-3 additions (feat/branch-primitive-instinct-gate):
#   * service.mark_merged / service.discard — the merge-gate state transitions
#     (accept → status="merged", reject → status="reverted") the executor calls.
#   * instinct_executor — the apply-on-approve (MERGE = publish + deploy) /
#     discard-on-reject executor the Instinct router dispatches to for an
#     ``_artifact_change`` Action. Kept in this package (not the router) so the
#     versions spine owns its merge logic; the router only gates (403) + emits.
#
# BP-4 additions (feat/branch-primitive-revert-history):
#   * service.revert — revert an artifact to a prior snapshot by writing a NEW
#     draft from the target version's content (revert moves forward; history is
#     never mutated). service.publish now also emits artifact.version.published.
#   * projection.VersionProjection — the READ projection that replays the
#     journal's artifact.version.* events into a per-artifact EVENT history
#     timeline (created / branched / merged / discarded / reverted / published),
#     mirroring the Decision/Fabric projection contract.
#
# What lives elsewhere (later BP tasks — do NOT add here):
#   * BP-5/6 — the site editor + the review/merge UI.
#
# Re-exports are intentionally minimal: the doc class and the service module.
from __future__ import annotations

from pocketpaw_ee.versions.models import ArtifactVersion

__all__ = ["ArtifactVersion"]
