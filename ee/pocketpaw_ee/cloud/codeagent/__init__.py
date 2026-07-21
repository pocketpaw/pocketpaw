# __init__.py — the Code Mode agent turn (CA-1).
# Created 2026-07-21 (feat/codeagent-turn): a 4-file cloud entity (domain / dto /
# service / router) for a STATELESS agent turn. Unlike every other Code Mode
# module it owns no persisted state and reaches no sandbox — the client sends
# the context it read through its own CodeFileSession, which is what lets one
# endpoint serve both the Daytona and WebContainer runtimes. Supersedes
# websandbox/edit.py (deleted in CA-4).
