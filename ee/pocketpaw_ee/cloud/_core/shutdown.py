# ee/pocketpaw_ee/cloud/_core/shutdown.py — "this worker process is going away" flag.
#
# The worker supervisor sets it once its drain window has run out, right before it
# cancels the jobs still running. A cancelled chat run reads it to tell the two kinds of
# host cancel apart: the worker shutting down (a deploy or restart; the run is requeued
# for the next worker) and the run's own arq job_timeout (retrying would just time out
# again; the run ends ``interrupted``). Process-wide on purpose: the supervisor and the
# runs share one event loop and one process, and nothing else needs to carry it.
"""Process-wide flag marking that the worker has started cancelling its jobs."""

from __future__ import annotations

_stopping = False


def mark_stopping() -> None:
    """Record that this process is cancelling its in-flight jobs to shut down."""
    global _stopping
    _stopping = True


def is_stopping() -> bool:
    """True once :func:`mark_stopping` ran in this process."""
    return _stopping


def reset() -> None:
    """Clear the flag. Tests only: a real process never un-stops."""
    global _stopping
    _stopping = False
