# __init__.py — the Code Mode agent turn (CA-1).
# Created 2026-07-21 (feat/codeagent-turn): a 4-file cloud entity (domain / dto /
# service / router) for a STATELESS agent turn. Unlike every other Code Mode
# module it owns no persisted state and reaches no sandbox — the client sends
# the context it read through its own CodeFileSession, which is what lets one
# endpoint serve both the Daytona and WebContainer runtimes. Supersedes
# websandbox/edit.py (deleted in CA-4).
#
# Modified: 2026-07-22 (CD-1). Adds a fifth file, ``delegates.py`` — the
# browser-delegate channel. It breaks the 4-file shape on purpose: it is not
# another entity but the transport under one route, and the same "the work
# happens in the browser" constraint that made the turn stateless is what makes
# a backend tool have to park on a future and wait for the tab to answer.
# Keeping that rendezvous out of ``service.py`` keeps that file about calling a
# model.
