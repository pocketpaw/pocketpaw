# ee/pocketpaw_ee/integrations/providers/__init__.py: the provider registry.
#
# ``_PROVIDERS`` is a tuple of module-level instances, in catalog order. To add a
# provider: one import line (ruff keeps the block sorted, so lanes land on
# different lines) and one line in the tuple below its marker comment.
#
# ``all_providers()`` is read per call, so the env switch that gates the ``demo``
# provider (``POCKETPAW_INTEGRATIONS_DEMO=1``) takes effect without a restart in
# tests. A provider that is not registered does not exist anywhere: not in the
# catalog, not at the connect routes, not at the gateway.

from __future__ import annotations

from pocketpaw_ee.integrations.providers.base import IntegrationProvider
from pocketpaw_ee.integrations.providers.demo import DemoProvider, demo_enabled
from pocketpaw_ee.integrations.providers.newsletter import NewsletterProvider
from pocketpaw_ee.integrations.providers.scheduling import CalComProvider, CalendlyProvider

_PROVIDERS: tuple[IntegrationProvider, ...] = (
    NewsletterProvider(),
    CalendlyProvider(),
    CalComProvider(),
    # ---- add new providers below, one per line ----
)

_DEMO = DemoProvider()


def all_providers() -> list[IntegrationProvider]:
    providers = list(_PROVIDERS)
    if demo_enabled():
        providers.append(_DEMO)
    return providers


def get_provider(provider_id: str) -> IntegrationProvider | None:
    return next((p for p in all_providers() if p.id == provider_id), None)


__all__ = ["all_providers", "get_provider"]
