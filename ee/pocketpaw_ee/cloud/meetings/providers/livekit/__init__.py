"""LiveKit meeting provider — native real-time calls.

Wraps the existing ``ee.cloud.livekit`` service (room mgmt, in-call agent,
composite egress recording) behind the unified ``MeetingProvider`` contract.

Importing this package has a side effect: it registers ``LiveKitProvider``
with the meetings provider registry. ``mount_cloud()`` eager-imports this
package at startup so ``base.resolve("livekit")`` succeeds from the first
request.
"""

from pocketpaw_ee.cloud.meetings.providers import base
from pocketpaw_ee.cloud.meetings.providers.livekit.provider import LiveKitProvider

base.register(LiveKitProvider())

__all__ = ["LiveKitProvider"]
