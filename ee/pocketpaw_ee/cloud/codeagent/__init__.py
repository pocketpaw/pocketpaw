# __init__.py — the Code Mode delegate channel.
#
# History: this package started (2026-07-21, CA-1) as a 4-file cloud entity for a
# STATELESS agent turn — a second, self-contained model loop reached at
# ``POST /codeagent/turn``. That turn-agent was removed 2026-07-23
# (remove/codeagent-turn-agent): the /code surface runs on the MAIN PocketPaw
# cloud agent now, which already streams and handles tools through the claude_sdk
# backend, so a parallel in-module agent (with its own keyless-CLI transport and
# prompts) was redundant and weaker.
#
# What remains is the browser-delegate channel (CD-1, ``delegates.py`` + the
# ``/codeagent/resolve`` route). The main agent's ``code_mode`` tool parks on a
# future and the browser wakes it by POSTing its answer back — the same "the work
# happens in the browser" constraint that once made the turn stateless is what
# makes a backend tool have to park and wait for the tab. ``service.py`` is now a
# thin router→service pass-through for the resolve route; the rendezvous logic
# lives in ``delegates.py``.
