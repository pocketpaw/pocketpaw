# ee/pocketpaw_ee/cloud/_core/sweep_runtime.py — which background sweeps actually started.
#
# Created 2026-09-05. GET /api/v1/automations/status used to answer "are the
# sweeps on" by reading an environment variable and nothing else. For the whole
# period the mount_cloud hooks were being dropped, that endpoint reported every
# sweep as on while none of them existed, which is most of the reason the bug
# survived: the place you would look to check reported a confident yes.
#
# This is the smallest thing that cannot lie in that direction. A name lands here
# only when the hook that starts the sweep has actually run to completion, so a
# dropped hook, an import error, or a raise inside the hook all leave it absent.
# It is process-local on purpose: the question the endpoint answers is "is this
# process running the sweep", and with more than one replica each answers for
# itself.
"""Process-local record of which cloud background sweeps started."""

from __future__ import annotations

_STARTED: set[str] = set()


def mark_started(name: str) -> None:
    """Record that the hook named ``name`` completed without raising."""
    _STARTED.add(name)


def mark_stopped(name: str) -> None:
    """Record that the sweep started by ``name`` has been torn down."""
    _STARTED.discard(name)


def is_running(name: str | None) -> bool:
    """Whether ``name`` started in this process and has not been torn down.

    ``None`` means the caller has no hook to point at, which is itself an
    answer: nothing claims to have started it, so it is not running.
    """
    return bool(name) and name in _STARTED


def started_names() -> frozenset[str]:
    """Everything currently marked started. For diagnostics and tests."""
    return frozenset(_STARTED)


def reset() -> None:
    """Clear the record. Tests only — a leaked entry makes a later test lie."""
    _STARTED.clear()
