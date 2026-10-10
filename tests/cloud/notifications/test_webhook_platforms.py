# tests/cloud/notifications/test_webhook_platforms.py
# Platform detection for lead-notification webhook URLs (pure): the table of
# real incoming-webhook URL shapes per chat app, the look-alikes that must stay
# json, and the override rule.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.notifications.webhook_platforms import (
    PLATFORM_LABELS,
    PLATFORMS,
    detect_platform,
    effective_platform,
    is_signed,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://hooks.slack.com/services/T000/B000/XXXX", "slack"),
        ("https://HOOKS.SLACK.COM/services/T000/B000/XXXX", "slack"),
        # Slack workflow webhooks take flat variables, not Block Kit.
        ("https://hooks.slack.com/workflows/T000/A000/123/abc", "json"),
        ("https://hooks.slack.com/triggers/T000/123/abc", "json"),
        ("https://discord.com/api/webhooks/123/abc-def", "discord"),
        ("https://discordapp.com/api/webhooks/123/abc", "discord"),
        ("https://ptb.discord.com/api/webhooks/123/abc", "discord"),
        ("https://discord.com/api/v10/webhooks/123/abc", "discord"),
        ("https://discord.com/channels/123/456", "json"),
        ("https://acme.webhook.office.com/webhookb2/abc@def/IncomingWebhook/x/y", "teams"),
        (
            "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke",
            "teams",
        ),
        (
            "https://default123.environment.api.powerplatform.com/powerautomate/automations/x",
            "teams",
        ),
        ("https://chat.googleapis.com/v1/spaces/AAAA/messages?key=k&token=t", "google_chat"),
        ("https://chat.googleapis.com/v1/spaces/AAAA/members", "json"),
        ("https://hooks.zapier.com/hooks/catch/123/abc/", "json"),
        ("https://hook.eu1.make.com/abc", "json"),
        ("https://n8n.acme.io/webhook/lead", "json"),
        ("https://eo123.m.pipedream.net", "json"),
        # Look-alike hosts are not the platform.
        ("https://hooks.slack.com.evil.example/services/a/b/c", "json"),
        ("https://evil-discord.com/api/webhooks/1/a", "json"),
        ("https://webhook.office.com.evil.example/x", "json"),
        ("https://notwebhook.office.com/x", "json"),
        ("", "json"),
        (None, "json"),
        ("not a url", "json"),
    ],
)
def test_detect_platform(url, expected) -> None:
    assert detect_platform(url) == expected


def test_override_wins_and_unknown_override_is_ignored() -> None:
    slack = "https://hooks.slack.com/services/a/b/c"
    assert effective_platform(slack, None) == "slack"
    assert effective_platform(slack, "json") == "json"
    assert effective_platform("https://x.example/h", "discord") == "discord"
    assert effective_platform(slack, "carrier-pigeon") == "slack"


def test_only_json_is_signed_and_every_platform_has_a_label() -> None:
    assert [p for p in PLATFORMS if is_signed(p)] == ["json"]
    assert set(PLATFORM_LABELS) == set(PLATFORMS)
