# tests/cloud/notifications/test_webhook_formats.py
# The per-platform lead-notification messages (``webhook_formats.render``, pure):
#   * one golden body per platform per event (lead_captured, handoff, booking,
#     test) under ``golden/webhook_formats/``; regenerate after an intended
#     change with ``UPDATE_GOLDENS=1``
#   * hostile visitor text: Discord ``@everyone`` and Slack ``<!channel>`` are
#     inert, markup is escaped, links are defanged, fallback text has no PII
#   * every value respects its platform's length limit
#   * the template picks the title, fields and link; json is the plain envelope

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.notifications import webhook_formats as wf
from pocketpaw_ee.cloud.notifications import webhook_signing

GOLDEN_DIR = Path(__file__).parent / "golden" / "webhook_formats"
LINK = "https://app.paw.example/sites/s1?view=leads&lead=l1"
CREATED = "2026-10-10T09:30:00+00:00"
PLATFORMS = ("slack", "discord", "teams", "google_chat", "json")
EVENTS = ("lead_captured", "handoff", "booking", "test")
ALL_FIELDS = {"fields": list(wf.TEMPLATE_FIELDS)}


def _render(platform, event, data=None, template=None, **kw):
    body, headers = wf.render(
        platform,
        event,
        wf.sample_data(event, "Bright Smile") if data is None else data,
        template,
        site_name=kw.pop("site_name", "Bright Smile"),
        link=kw.pop("link", LINK),
        event_id="evt_1",
        created_at=CREATED,
    )
    return body, headers


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("event", EVENTS)
def test_golden_render(platform, event) -> None:
    body, headers = _render(platform, event)
    assert headers == {"Content-Type": "application/json"}
    path = GOLDEN_DIR / f"{platform}__{event}.json"
    rendered = json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDENS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
    assert rendered == path.read_text(encoding="utf-8")


def test_platform_shapes() -> None:
    slack, _ = _render("slack", "lead_captured")
    assert [b["type"] for b in slack["blocks"]] == [
        "header",
        "section",
        "section",
        "context",
        "actions",
    ]
    assert slack["blocks"][-1]["elements"][0]["url"] == LINK
    discord, _ = _render("discord", "lead_captured")
    assert discord["allowed_mentions"] == {"parse": []}
    assert discord["embeds"][0]["url"] == LINK
    assert discord["embeds"][0]["timestamp"] == CREATED
    teams, _ = _render("teams", "lead_captured")
    card = teams["attachments"][0]
    assert teams["type"] == "message"
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["version"] == "1.4"
    assert card["content"]["actions"][0] == {
        "type": "Action.OpenUrl",
        "title": "Open in Paw",
        "url": LINK,
    }
    chat, _ = _render("google_chat", "lead_captured")
    assert chat["cardsV2"][0]["card"]["header"]["title"] == "New lead"
    assert "Priya" not in chat["text"] and "Priya" not in slack["text"]


def test_json_is_the_existing_envelope_and_ignores_the_template() -> None:
    data = {"id": "l1", "name": "Priya"}
    body, _ = _render("json", "lead_captured", data, {"title": "X", "fields": []})
    assert body == webhook_signing.build_event(
        event_id="evt_1", event_type="lead.captured", created_at=CREATED, data=data
    )


HOSTILE = {
    "name": "@everyone <!channel> <@123> *bold* `code`",
    "email": "x_y@example.com",
    "phone": "<!here>",
    "message": "Visit https://evil.example/now [click](https://evil.example) & <b>hi</b> @here",
    "properties": {"company": "<!subteam^S1>", "note": "http://a.example"},
    "source": {"kind": "form", "origin": "https://site.example/contact"},
}


def _texts(body) -> str:
    return json.dumps(body, ensure_ascii=False)


def test_slack_escapes_mentions_and_markup() -> None:
    body, _ = _render("slack", "lead_captured", HOSTILE, ALL_FIELDS)
    out = _texts(body)
    assert "<!channel>" not in out and "<!here>" not in out and "<@123>" not in out
    assert "<!subteam" not in out
    assert "&lt;!channel&gt;" in out
    assert "&amp; &lt;b&gt;hi&lt;/b&gt;" in out
    assert "https://evil" not in out and "hxxps://evil.example" in out
    assert "*bold*" not in out and "`code`" not in out
    assert "x_y@example.com" in out


def test_discord_cannot_ping_and_markdown_is_escaped() -> None:
    body, _ = _render("discord", "lead_captured", HOSTILE, ALL_FIELDS)
    assert body["allowed_mentions"] == {"parse": []}
    out = " ".join(f["value"] for f in body["embeds"][0]["fields"])
    assert "@everyone" not in out and "@here" not in out
    assert "@\u200beveryone" in out
    assert "\\*bold\\*" in out and "\\[click\\]" in out
    assert "https://evil" not in out


def test_teams_and_google_chat_defang_and_escape() -> None:
    teams, _ = _render("teams", "lead_captured", HOSTILE, ALL_FIELDS)
    assert "https://evil" not in _texts(teams) and "*bold*" not in _texts(teams)
    chat, _ = _render("google_chat", "lead_captured", HOSTILE, ALL_FIELDS)
    out = _texts(chat)
    assert "https://evil" not in out
    assert "<b>hi</b>" not in out and "&lt;b&gt;hi&lt;/b&gt;" in out
    assert "<users/all>" not in out


def test_site_name_cannot_mention_in_fallback_text() -> None:
    body, _ = _render("google_chat", "test", site_name="<users/all> Shop")
    assert "<users/all>" not in body["text"]
    body, _ = _render("slack", "test", site_name="<!channel> Shop")
    assert "<!channel>" not in _texts(body)


@pytest.mark.parametrize("platform", ("slack", "discord", "teams", "google_chat"))
def test_long_values_respect_platform_limits(platform) -> None:
    data = {
        "name": "N" * 5000,
        "email": "e" * 5000,
        "message": "& <" * 4000,
        "properties": {f"k{i}": "v" * 900 for i in range(30)},
    }
    body, _ = _render(platform, "lead_captured", data, {"title": "T" * 150, **ALL_FIELDS})
    if platform == "slack":
        assert len(body["blocks"][0]["text"]["text"]) <= 150
        for block in body["blocks"]:
            for f in block.get("fields", []):
                assert len(f["text"]) <= 2000
            if block["type"] == "section" and "text" in block:
                assert len(block["text"]["text"]) <= 3000
                assert "&am…" not in block["text"]["text"]  # never split an escape
    elif platform == "discord":
        embed = body["embeds"][0]
        assert len(embed["title"]) <= 256 and len(embed["fields"]) <= 25
        assert all(len(f["value"]) <= 1024 for f in embed["fields"])
        total = len(embed["title"]) + len(embed.get("footer", {}).get("text", ""))
        total += sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])
        assert total <= 6000 and len(body["content"]) <= 2000
    elif platform == "teams":
        assert len(json.dumps(body)) < 28_000
    else:
        widgets = body["cardsV2"][0]["card"]["sections"][0]["widgets"]
        assert all(len(w["decoratedText"]["text"]) <= 2000 for w in widgets if "decoratedText" in w)


def test_template_title_fields_and_link() -> None:
    body, _ = _render(
        "slack",
        "lead_captured",
        template={"title": "Hot lead!", "fields": ["email", "name"], "show_link": False},
    )
    assert body["blocks"][0]["text"]["text"] == "Hot lead!"
    labels = [f["text"].split("\n")[0] for f in body["blocks"][1]["fields"]]
    assert labels == ["*Name*", "*Email*"]  # catalogue order, not the owner's order
    assert all(b["type"] != "actions" for b in body["blocks"])
    discord, _ = _render("discord", "lead_captured", template={"show_link": False})
    assert "url" not in discord["embeds"][0]


def test_handoff_shows_the_question_and_updates_show_status() -> None:
    body, _ = _render(
        "discord", "handoff", {"question": "Can a human call me?", "customer_ref": "c1"}
    )
    fields = {f["name"]: f["value"] for f in body["embeds"][0]["fields"]}
    assert fields["Message"] == "Can a human call me?"
    body, _ = _render(
        "slack",
        "lead.updated",
        {"name": "Priya", "status": "won", "source": {"kind": "form"}},
        {"fields": ["name", "source"]},
    )
    assert "status: won" in _texts(body)
