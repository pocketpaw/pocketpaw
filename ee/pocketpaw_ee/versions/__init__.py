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
# What lives elsewhere (later BP tasks — do NOT add here in BP-1):
#   * BP-3 — the Instinct review/merge gate over a version candidate.
#   * BP-4 — revert + a Journal read projection (version history view).
#   * BP-5/6 — the site editor + the review/merge UI.
#
# Re-exports are intentionally minimal: the doc class and the service module.
from __future__ import annotations

from pocketpaw_ee.versions.models import ArtifactVersion

__all__ = ["ArtifactVersion"]
