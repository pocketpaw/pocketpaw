# __init__.py — prompt-to-project scaffolding (CS-1, rewritten CS-1b).
# Created 2026-07-21; rewritten 2026-07-22. A cloud entity (domain / dto /
# registry / service / router) that turns a prompt into a framework starter.
# Starters come from PINNED, integrity-checked npm tarballs (create-vite,
# create-next-app) rather than a vendored template — see domain.py for why
# cloning the upstream repos does not work. Owns no persisted state and
# reaches no sandbox: `compose` returns a SOURCE MAP and the runtime
# materializes it, which is what lets one endpoint serve both runtimes.
