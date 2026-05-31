from pocketpaw_ee.cloud._core.realtime.events import Event, GroupCreated


def test_event_has_type_data_ts():
    ev = Event(type="x.y", data={"a": 1})
    assert ev.type == "x.y"
    assert ev.data == {"a": 1}
    assert ev.ts is not None


def test_typed_event_subclass_sets_type():
    ev = GroupCreated(data={"group_id": "g1", "member_ids": ["u1"]})
    assert ev.type == "group.created"
    assert ev.data["group_id"] == "g1"
