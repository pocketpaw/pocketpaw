# ee/pocketpaw_ee/cloud/belt/__init__.py — the Belt & Pulley cloud package.
# Created: 2026-06-10 (feat/belt-gate, BS-3).
#
# Home of the apply-on-approve executor (``executor.py``) for the Belt develop
# station's code-change gate. A code-change Instinct Action proposed by the
# ``pocketpaw_belt`` MCP server is applied here — in a fresh worktree — after a
# human approves it, mirroring the pocket-write bridge's propose/execute split.
