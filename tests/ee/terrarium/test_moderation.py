# tests/ee/terrarium/test_moderation.py — both directions of the text gate.
# Inbound (a viewer line) is rejected before anything is written; outbound (a
# citizen's say / write, a moment headline) is WITHHELD at the write so the
# Journal's seq stays continuous and the cost still lands.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import moderation  # noqa: E402

from .conftest import create_universe  # noqa: E402

SLUR = "kill yourself"


def test_the_check_is_a_deny_list_plus_a_length_cap(monkeypatch):
    assert moderation.allowed("the spring is cursed")
    assert not moderation.allowed(f"you should {SLUR} now")
    assert not moderation.allowed("x" * (moderation.MAX_LEN + 1))
    monkeypatch.setenv("TERRARIUM_DENY_TERMS", "zorblax, quux")
    assert not moderation.allowed("a Zorblax walked in")
    assert moderation.allowed("zorblaxian")  # word boundary, not substring


async def test_an_inbound_slur_is_rejected_and_nothing_is_written(client):
    uni = create_universe(client, founders=1)
    before = client.get(f"/terrarium/universes/{uni['id']}/events").json()["events"]
    res = client.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": SLUR})
    assert res.status_code == 422, res.text
    assert "not accepted" in res.text
    after = client.get(f"/terrarium/universes/{uni['id']}/events").json()["events"]
    assert len(after) == len(before)
    assert client.get(f"/terrarium/universes/{uni['id']}").json()["universe"]["pool"] == uni["pool"]


async def test_a_clean_line_passes_both_ways_untouched(client):
    uni = create_universe(client, founders=1)
    res = client.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": "hello world"})
    assert res.status_code == 200
    assert res.json()["event"]["body"] == "hello world"
    assert res.json()["event"]["data"] == {}

    citizen_llm.set_mock_decision(
        {"thought": "fine", "acts": [{"verb": "speak", "text": "the spring is sweet"}]}
    )
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    says = [
        e
        for e in client.get(f"/terrarium/universes/{uni['id']}/events").json()["events"]
        if e["kind"] == "say" and not e["viewer_origin"]
    ]
    assert says and says[-1]["body"] == "the spring is sweet"
    assert "withheld" not in says[-1]["data"]


async def test_an_outbound_slur_is_withheld_with_cost_and_seq_intact(client):
    uni = create_universe(client, founders=1)
    citizen_llm.set_mock_decision(
        {"thought": "angry", "acts": [{"verb": "speak", "text": f"{SLUR}, stranger"}]}
    )
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    events = client.get(f"/terrarium/universes/{uni['id']}/events").json()["events"]
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    say = next(e for e in events if e["kind"] == "say" and not e["viewer_origin"])
    assert say["body"] == moderation.WITHHELD
    assert say["data"]["withheld"] is True
    assert say["cost"] == -uni["physics"]["costs"]["speak"] != 0
    assert SLUR not in str(events)


async def test_the_eleventh_line_in_a_minute_is_refused(client):
    uni = create_universe(client, founders=1)
    for i in range(10):
        res = client.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": f"line {i}"})
        assert res.status_code == 200, (i, res.text)
    res = client.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": "one more"})
    assert res.status_code == 429, res.text


async def test_withheld_text_stays_withheld_on_every_public_projection(client, monkeypatch):
    """The Journal row is not the only place a citizen's words land: a moment's
    ``data.headline`` echoes its body, a first write becomes the charter on the
    citizen wire, and any write becomes an artifact with a public name."""
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    monkeypatch.setenv("TERRARIUM_PUBLIC_DELAY_EVENTS", "0")
    uni = create_universe(client, public=True, founders=2)
    citizen_llm.set_mock_decision(
        {
            "thought": "spite",
            "acts": [{"verb": "write", "name": f"{SLUR} tract", "text": f"page one: {SLUR}."}],
        }
    )
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    base = f"/terrarium/public/universes/{uni['id']}"
    assert SLUR not in client.get(f"{base}/events").text
    assert SLUR not in client.get(f"{base}/artifacts").text
    assert SLUR not in client.get(f"{base}/citizens").text
    assert SLUR not in client.get(f"/terrarium/universes/{uni['id']}").text
    citizens = client.get(f"{base}/citizens").json()["citizens"]
    assert all(c["charter"] == moderation.WITHHELD for c in citizens)
    arts = client.get(f"{base}/artifacts").json()["artifacts"]
    assert arts and all(a["name"] == moderation.WITHHELD for a in arts)
    # A moment's payload echoes its headline; the echo is scrubbed with it.
    from pocketpaw_ee.terrarium.domain import UniverseDoc
    from pocketpaw_ee.terrarium.service import _append_event

    doc = await UniverseDoc.get(uni["id"])
    row = await _append_event(
        doc, kind="moment", actor="Nim", body=SLUR, cost=0, data={"headline": SLUR, "n": 2}
    )
    assert row.body == moderation.WITHHELD
    assert row.data == {"headline": moderation.WITHHELD, "n": 2, "withheld": True}
