# Cloud meetings entity — re-exports for mount_cloud wiring.
# Created: 2026-05-19 — Native meetings integration (Google Meet + Zoom).
# See docs/plans/2026-05-19-meetings-integration-design.md.

from __future__ import annotations

from ee.cloud.meetings.router import router

__all__ = ["router"]
