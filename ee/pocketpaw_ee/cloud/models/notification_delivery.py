# ee/pocketpaw_ee/cloud/models/notification_delivery.py
# Per-workspace external notification delivery config: a Slack incoming-webhook
# URL, a generic HTTPS webhook URL (signed, see ``notifications.webhook_signing``),
# a master ``enabled`` switch and per-kind ``routes``. One row per workspace; the
# ``workspace`` key is Indexed unique so read/upsert stays O(1).
#
# Only ``ee.cloud.notifications`` reads or writes this doc (service writes the
# config; the outbox bumps the webhook failure counter). The import-linter
# "Notifications" contract keeps router/dto/domain off it.
#
# Routing: ``routes`` maps a notification kind to the sink names it should reach
# ("slack", "webhook"). An empty ``routes``, or a kind absent from it, delivers to
# every configured sink while ``enabled`` is True; a present entry narrows it.
#
# The webhook signing secret is Fernet-encrypted (``webhook_secret_enc``), minted
# when a URL is first saved or changed, and shown to the admin once. A webhook
# saved before signing existed has no secret and keeps delivering unsigned. On
# rotation the old secret stays in ``webhook_secret_prev_enc`` and co-signs for
# a grace window. After 10 consecutive dead deliveries the webhook is switched
# off (``webhook_disabled_at``) until it is saved again.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class NotificationDeliveryConfig(TimestampedDocument):
    """Per-workspace external-delivery config for notifications.

    Fields:
      - ``workspace`` — tenancy key. Indexed unique so ``find_one`` / upsert
        stays O(1).
      - ``slack_webhook_url`` — a Slack *incoming webhook* URL
        (``https://hooks.slack.com/services/...``). ``None`` disables the Slack
        sink. POSTed as ``{"text": ...}`` (Slack's incoming-webhook shape).
      - ``webhook_url`` — a generic HTTPS endpoint. ``None`` disables the generic
        sink. Receives the full notification payload as JSON.
      - ``enabled`` — master switch. When ``False`` no external delivery happens
        regardless of the URLs (a workspace can save URLs but keep them dark).
      - ``routes`` — optional per-kind narrowing (see module docstring). Default
        empty => deliver every kind to every configured sink.
      - ``webhook_secret_enc`` — Fernet ciphertext of the HMAC signing secret.
      - ``webhook_failure_count`` / ``webhook_disabled_at`` — consecutive dead
        webhook deliveries, and when the webhook was switched off for them.
      - ``createdAt`` / ``updatedAt`` — inherited from
        :class:`TimestampedDocument`.

    The shape is extension-additive; new optional fields with safe defaults won't
    break callers reading the v1 config.
    """

    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    slack_webhook_url: str | None = None
    webhook_url: str | None = None
    enabled: bool = False
    routes: dict[str, list[str]] = Field(default_factory=dict)
    webhook_secret_enc: str = ""
    # The secret a rotation replaced, still signing until the grace window ends.
    webhook_secret_prev_enc: str = ""
    webhook_secret_rotated_at: datetime | None = None
    webhook_failure_count: int = 0
    webhook_disabled_at: datetime | None = None

    class Settings:
        name = "notification_delivery_configs"
        # ``workspace`` already carries a unique single-field index from the
        # ``Indexed(..., unique=True)`` annotation; the upsert path uses it.
