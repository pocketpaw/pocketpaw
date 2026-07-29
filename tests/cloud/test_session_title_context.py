"""The cloud placeholder title must not leak the client's context preamble.

``_generate_session_title`` writes a placeholder to Mongo and pushes it over
SSE *before* Haiku runs, so the placeholder is what the user actually sees in
the sidebar first. Titling the raw wire content named every home-started chat
"[Home page snapshot] Time of day: afternoon…" — fixing only the Haiku path
would have left that visible until the model replied (and permanently whenever
Haiku is unavailable, which is the default in local/dev deployments).
"""

from __future__ import annotations

from pocketpaw_ee.cloud.chat.runs.run_core import _truncate_for_title

# The exact shape paw-enterprise sends (see home-context.ts).
WRAPPED = (
    "[Home page snapshot]\n"
    "Time of day: afternoon\n"
    "Status: 6 pinned widgets\n"
    "\n"
    "[User message]\n"
    "how do I add a chart to my home?"
)


def test_placeholder_titles_the_user_text_not_the_snapshot():
    assert _truncate_for_title(WRAPPED) == "how do I add a chart to my home?"


def test_placeholder_leaves_ordinary_messages_alone():
    assert _truncate_for_title("ship the release") == "ship the release"


def test_placeholder_still_truncates_long_user_text():
    title = _truncate_for_title("ctx\n[User message]\n" + ("word " * 40))
    assert title.endswith("…")
    assert "ctx" not in title


def test_placeholder_is_empty_when_only_a_preamble_was_sent():
    # Empty placeholder → the caller skips writing a title rather than naming
    # the session after scaffolding.
    assert _truncate_for_title("ctx\n[User message]\n   ") == ""
