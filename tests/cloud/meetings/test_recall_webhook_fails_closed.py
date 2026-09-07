"""An unsigned Recall webhook is refused, unless a deployment explicitly asks.

``_verify_signature`` used to be a NO-OP when ``RECALL_WEBHOOK_SECRET`` was
unset: it logged a warning and accepted the request. Three facts together turn
that from lenient into a hole:

  1. ``/api/v1/meetings/webhooks/`` is on ``dashboard_auth.exempt_paths``, so
     there is no auth in front of this route at all. The signature IS the trust
     boundary — there is no second one.
  2. An accepted event drives ``update_bot_status_for_recall_bot``,
     ``start_async_transcript`` and ``ingest_transcript_for_recall_bot``. It
     mutates meeting state and ingests transcript content, selected only by a
     ``bot_id`` read out of the body.
  3. ``RECALL_WEBHOOK_SECRET`` appeared NOWHERE outside that module and its
     tests. Not in the env template, not in any doc. An operator following the
     documented deploy could not learn the variable exists, so the warning was
     addressed to somebody who would never see it.

The sibling ``growth`` webhook already fails closed on an unconfigured secret
and its module docstring says so by contrast — "Unlike the Recall webhook it
FAILS CLOSED". That comment is what pointed here.

The wiring-up case is preserved and now has to be asked for:
``RECALL_WEBHOOK_ALLOW_UNSIGNED=1``. Silence is safe; leniency is deliberate.

Mutations that must fail these tests: returning instead of raising when the
secret is unset, and treating any unset/empty ALLOW_UNSIGNED value as truthy.
"""

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden

from .test_recall_client import _WEBHOOK_SECRET, _make_request, _svix_headers


def _body() -> bytes:
    return json.dumps({"event": "transcript.done", "data": {"bot": {"id": "bot-victim"}}}).encode()


async def test_an_unsigned_webhook_is_refused_when_no_secret_is_configured(monkeypatch):
    """The reproduction. Before the fix this returned 200 and ingested."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.delenv("RECALL_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RECALL_WEBHOOK_ALLOW_UNSIGNED", raising=False)

    ingested: list[str] = []

    async def _fake_ingest(bot_id):
        ingested.append(bot_id)
        return True

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.service.ingest_transcript_for_recall_bot", _fake_ingest
    )

    with pytest.raises(Forbidden) as exc:
        await webhooks.recall_webhook(_make_request(_body(), {}))

    assert exc.value.code == "meeting.webhook_unsigned"
    assert ingested == [], "the webhook reached the service without a signature"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
async def test_only_an_explicit_opt_in_accepts_an_unsigned_webhook(monkeypatch, value):
    """A present-but-not-truthy value must not open the gate.

    ``os.environ.get(...)`` being non-empty is the easy wrong check, and it
    would make ``RECALL_WEBHOOK_ALLOW_UNSIGNED=0`` mean "yes".
    """
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.delenv("RECALL_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("RECALL_WEBHOOK_ALLOW_UNSIGNED", value)

    with pytest.raises(Forbidden):
        await webhooks.recall_webhook(_make_request(_body(), {}))


async def test_the_documented_escape_hatch_still_works(monkeypatch):
    """A fresh deployment can still be wired up before the secret exists."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.delenv("RECALL_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("RECALL_WEBHOOK_ALLOW_UNSIGNED", "1")

    captured: list[str] = []

    async def _fake_ingest(bot_id):
        captured.append(bot_id)
        return True

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.service.ingest_transcript_for_recall_bot", _fake_ingest
    )

    result = await webhooks.recall_webhook(_make_request(_body(), {}))
    assert result["ok"] is True
    assert captured == ["bot-victim"]


async def test_a_correctly_signed_webhook_is_unaffected(monkeypatch):
    """The fix must not break the path that was already working."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    monkeypatch.delenv("RECALL_WEBHOOK_ALLOW_UNSIGNED", raising=False)

    captured: list[str] = []

    async def _fake_ingest(bot_id):
        captured.append(bot_id)
        return True

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.service.ingest_transcript_for_recall_bot", _fake_ingest
    )

    body = _body()
    result = await webhooks.recall_webhook(
        _make_request(body, _svix_headers(_WEBHOOK_SECRET, body))
    )
    assert result["ok"] is True
    assert captured == ["bot-victim"]


def test_the_secret_is_documented_where_an_operator_will_look():
    """The hole was half config and half documentation.

    A fail-closed check an operator cannot discover turns a silent security gap
    into a silent outage. The variable has to be in the env template.
    """
    import re
    from pathlib import Path

    template = Path(__file__).resolve().parents[3] / ".env.example"
    text = template.read_text(encoding="utf-8")

    # The ASSIGNMENT LINE, not merely the string. A mutation that deleted the
    # line escaped a substring check, because the paragraph explaining the
    # variable still mentioned it by name — and prose is not what an operator
    # copies out of a template.
    for var in ("RECALL_WEBHOOK_SECRET", "RECALL_WEBHOOK_ALLOW_UNSIGNED"):
        assert re.search(rf"^#?\s*{var}=", text, re.M), (
            f"{var} has no assignment line in .env.example; an operator "
            "cannot copy a variable that is only described in a comment"
        )
