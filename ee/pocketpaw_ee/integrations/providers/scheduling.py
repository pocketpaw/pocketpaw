# ee/pocketpaw_ee/integrations/providers/scheduling.py: the booking-page embeds.
#
# ``calendly`` and ``cal_com`` are kind ``embed``: no connection, no actions, no
# token. The owner pastes their PUBLIC booking link (one ``url`` setting, host
# pinned to the provider's own domains) and the Sites agent drops the provider's
# standard inline embed markup into the page with that link. The binding reads
# ``connected`` once the link is valid and ``setup_required`` until then; the
# gateway refuses every call to an embed.

from __future__ import annotations

from pocketpaw_ee.integrations.providers.base import BaseProvider, SettingsField


class CalendlyProvider(BaseProvider):
    id = "calendly"
    name = "Calendly"
    kind = "embed"
    category = "scheduling"
    plan = "free"
    settings_fields = (
        SettingsField(
            key="url",
            label="Calendly booking link",
            type="url",
            help="Your public scheduling link, like https://calendly.com/your-name/30min.",
            required=True,
            allowed_hosts=("calendly.com",),
        ),
    )


class CalComProvider(BaseProvider):
    id = "cal_com"
    name = "Cal.com"
    kind = "embed"
    category = "scheduling"
    plan = "free"
    settings_fields = (
        SettingsField(
            key="url",
            label="Cal.com booking link",
            type="url",
            help="Your public booking link, like https://cal.com/your-name/30min.",
            required=True,
            allowed_hosts=("cal.com", "app.cal.com"),
        ),
    )
