# Game worlds — the cloud-side glue that composes a described "living world"
# into a Pocket type="game" AND runs it. This package is a sibling of
# `cloud/` (not nested under it), mirroring the `sites/` package layout:
#   * service.py — the deterministic create path (v0 vibe→dials preset table,
#     shared plan gate). Created: 2026-07-02 (feat/game-surface, PW-2).
#   * runtime.py — the in-process GameWorld runtime over soul-protocol's Game
#     Profile (guarded dep; worlds are v0-ephemeral). dto.py + router.py —
#     the /api/v1/game REST surface (start / beat / events / snapshot /
#     reputation / seed_example), registered in cloud/__init__.py beside the
#     sites router. Updated: 2026-07-02 (feat/game-surface, PE-A).
