"""Recall.ai meeting provider — external Zoom/Meet/Teams capture.

A Recall.ai bot joins a third-party meeting URL, records the call, and
produces a transcript via Deepgram (or any of Recall's 9 supported
async/streaming providers).

Phase 1 ships the package shell; the provider implementation is folded
in from #1140 in follow-up commits.
"""
