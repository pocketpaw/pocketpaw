from pocketpaw_ee.cloud._core.realtime.events import (
    EVENT_REGISTRY,
    ActivityLogged,
    ThreadClosed,
    ThreadCreated,
)


def test_thread_created_registered():
    ev = ThreadCreated(data={"id": "t1"})
    assert ev.type == "thread.created"
    assert EVENT_REGISTRY["thread.created"] is ThreadCreated


def test_thread_closed_registered():
    ev = ThreadClosed(data={"id": "t1"})
    assert ev.type == "thread.closed"
    assert EVENT_REGISTRY["thread.closed"] is ThreadClosed


def test_activity_logged_registered():
    ev = ActivityLogged(data={"entry_id": "a1"})
    assert ev.type == "activity.logged"
    assert EVENT_REGISTRY["activity.logged"] is ActivityLogged
