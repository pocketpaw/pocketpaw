# constants.py — shared Web Cursor sandbox constants.
# Created 2026-07-15: single source of truth for the in-VM workspace directory.
#
# Everything the user touches lives under ONE directory in the VM: the repo is
# cloned here, the file tree/RPC is jailed here, and the terminal opens here.
# We deliberately use ``/home/daytona`` (the sandbox user's home) rather than the
# SDK's ``get_project_dir()`` — which on the current Daytona image returns
# ``/root``, mismatching the PTY's default cwd (``/home/daytona``). Pinning one
# constant keeps the clone target, the jail root, and the terminal cwd identical
# so files a user clones are exactly what the tree lists and the shell sees.
from __future__ import annotations

WEBSANDBOX_WORKDIR = "/home/daytona"
