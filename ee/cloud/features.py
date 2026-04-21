"""Cloud-mode feature overrides.

When ee.cloud is installed, certain OSS-optional features are force-enabled
because the cloud product depends on them (chat titles drive the sidebar,
outbound webhooks drive realtime UI updates, etc.).
"""

from __future__ import annotations


def chat_titles_enabled() -> bool:
    return True
