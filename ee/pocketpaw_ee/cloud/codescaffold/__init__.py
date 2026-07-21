# __init__.py — prompt-to-project scaffolding (CS-1).
# Created 2026-07-21 (feat/codescaffold): a cloud entity (domain / dto / engine /
# service / router) that turns a prompt into a composed SvelteKit project. Owns
# no persisted state and reaches no sandbox — `compose` returns a SOURCE MAP and
# the runtime materializes it (tar-upload for Daytona, `fs.mount` in a tab),
# which is what lets one endpoint serve both. `engine.py` is the only file that
# shells out; `_template/` is the vendored recipe engine it runs.
