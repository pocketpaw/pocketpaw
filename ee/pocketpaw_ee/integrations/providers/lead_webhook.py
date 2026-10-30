# ee/pocketpaw_ee/integrations/providers/lead_webhook.py: the internal ``lead_webhook`` provider.
#
# A site's lead-notification destination (a Slack / Discord / Teams / Google Chat
# incoming-webhook URL, or a generic JSON endpoint) is a credential: whoever holds
# the URL can post into the owner's channel. Each one is a vault row
# (``IntegrationConnection``) of this provider, ``account_ref`` =
# ``"<site_id>:<webhook_id>"``, the bundle ``{"url": ...}``. The owner pastes the
# URL in the lead settings, not in the integrations catalog: these are Slack's
# and Discord's own "incoming webhook" credentials, not an OAuth grant.
#
# INTERNAL. The provider is registered as internal (``providers.internal_ids``):
# it is not in the catalog, can't be connected, linked to a site, resumed or
# revoked through the integrations routes, and its rows are never listed as
# connected accounts (``vault.get_connection`` reads them as missing unless the
# caller asks for internal rows). Only ``cloud.leads.webhook_vault`` writes and
# reads them.

from __future__ import annotations

from pocketpaw_ee.integrations.providers.base import BaseProvider

PROVIDER_ID = "lead_webhook"


class LeadWebhookProvider(BaseProvider):
    id = PROVIDER_ID
    name = "Lead webhook"
    kind = "builtin"
    category = "notifications"
    plan = "free"
    internal = True
