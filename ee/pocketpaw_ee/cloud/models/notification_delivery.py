# ee/pocketpaw_ee/cloud/models/notification_delivery.py
# Created: 2026-07-08 (feat/external-alerting-delivery) — Per-workspace external
# notification delivery configuration. Backs Criterion 1 of external alerting:
# a cloud notification (today WebSocket-only / in-app) can now ALSO fan out to a
# Slack incoming-webhook and/or a generic HTTPS webhook. One row per workspace;
# the ``workspace`` key is Indexed unique so the read/upsert path stays O(1)
# (mirrors BeltWorkspaceConfig / ForesightWorkspaceConfig).
#
# Only ``ee.cloud.notifications.service`` WRITES this doc (upsert via the PUT
# /notifications/delivery-config route) and only ``ee.cloud.notifications``
# (service + delivery) READS it — same single-owner discipline the other
# per-workspace config docs use, pinned by the import-linter "Notifications"
# contract (router/dto/domain may never import this module).
#
# Routing: ``routes`` maps a notification kind -> the sink names it should reach
# ("slack", "webhook"). The default (empty ``routes`` OR a kind absent from it)
# is deliver-to-every-configured-sink when ``enabled`` is True. A present entry
# NARROWS delivery of that kind to the named sinks. The shape is
# extension-additive: a third sink ("email" via the gmail connector) layers on
# as a new optional URL field + a new sink name with safe defaults.

from __future__ import annotations

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

    class Settings:
        name = "notification_delivery_configs"
        # ``workspace`` already carries a unique single-field index from the
        # ``Indexed(..., unique=True)`` annotation; the upsert path uses it
        # directly. No composite indexes needed in v1.
