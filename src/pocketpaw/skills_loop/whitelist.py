# skills_loop/whitelist.py — the write-only tool whitelist for the reviewer.
# Created: 2026-06-16 (feat/self-improving-skills) — the safety machinery that
#   restricts the forked session reviewer to soul-write ONLY.
# Updated: 2026-06-16 (review) — the set is the CONTRACT a caller MUST forward to
#   the Claude SDK backend's per-run tool allowlist (agents/backend.py
#   ``allow_sdk_tools`` / the ``allowed_tools`` assembly in claude_sdk) so the
#   reviewer cannot call Bash/Read/Write/Edit/any MCP tool — only write learned
#   procedures into the soul. NOTE: this PR does NOT spawn the SDK run, so that
#   runtime restriction is not enforced here; the session-finalize spawn that
#   forwards this set lands with the runtime-hookup follow-up (see reviewer.py).
#   ``assert_write_only`` is the invariant guard a caller runs before launching.

from __future__ import annotations

# The ONLY tool the reviewer may use: the soul-write tool
# (``SoulRememberTool.name`` in ``pocketpaw.paw.tools``). Kept as a frozenset so
# it is immutable and hashes into the SDK client cache key cleanly.
SOUL_WRITE_TOOL_IDS: frozenset[str] = frozenset({"soul_remember"})


def build_reviewer_whitelist() -> frozenset[str]:
    """Return the write-only tool whitelist for the session reviewer.

    The reviewer is granted exactly the soul-write tool and nothing else. This
    is the additive allowlist passed to the Claude SDK backend; combined with a
    deny-everything base policy it leaves soul-write as the only callable tool.
    """
    return SOUL_WRITE_TOOL_IDS


def assert_write_only(whitelist: frozenset[str]) -> None:
    """Assert ``whitelist`` grants soul-write tools ONLY.

    Raises ``ValueError`` if the set contains any id outside
    :data:`SOUL_WRITE_TOOL_IDS` (e.g. Bash, Read, Write, Edit, or any MCP
    tool). Run this immediately before spawning the reviewer so a
    mis-assembled allowlist can never let the reviewer reach a non-write tool.
    """
    extra = set(whitelist) - SOUL_WRITE_TOOL_IDS
    if extra:
        raise ValueError(
            "Skills-loop reviewer must be write-only: "
            f"whitelist carries non-soul-write tools {sorted(extra)}"
        )
    if not whitelist:
        raise ValueError(
            "Skills-loop reviewer must be write-only: whitelist is empty "
            "(the reviewer would be unable to write any learned procedure)"
        )


__all__ = ["SOUL_WRITE_TOOL_IDS", "build_reviewer_whitelist", "assert_write_only"]
