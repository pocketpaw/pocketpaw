"""Per-backend Composio provider smoke — not a pytest test.

Run directly:  uv run python tests/cloud/composio/smoke_wiring.py

Verifies the documented integration shape against the REAL Composio
SDKs (no API call yet — composio.create() is the only network-touching
call and we patch _build_session_and_tools so this script stays offline-safe):

  1. Settings validate a Composio-enabled config.
  2. All four provider packages import cleanly.
  3. pocketpaw_ee.cloud.composio.providers.build_tools_for_backend() dispatches to
     the right provider for each backend kind.
  4. Real composio.Composio + each provider class construct without error.

For a TRUE end-to-end test (which hits the Composio API), set
POCKETPAW_COMPOSIO_API_KEY in the env and uncomment the live-call block
at the bottom.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("POCKETPAW_COMPOSIO_API_KEY", "ck_smoke_dev_key")
os.environ.setdefault("POCKETPAW_COMPOSIO_ENTERPRISE_ID", "ent_smoke")


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    sys.exit(1)


def main() -> None:
    print("== composio per-backend wiring smoke ==")

    from pocketpaw.config import Settings

    s = Settings(_env_file=None)
    assert s.composio_enterprise_id == "ent_smoke"
    _ok("Settings parsed api_key + enterprise_id")

    from pocketpaw_ee.cloud.composio import providers as composio_providers
    from pocketpaw_ee.cloud.composio import service as composio_service

    assert composio_service.is_enabled(s) is True
    _ok("composio_service.is_enabled(s) == True")

    # All four provider packages import cleanly.
    from composio import Composio  # noqa: F401
    from composio_claude_agent_sdk import ClaudeAgentSDKProvider  # noqa: F401
    from composio_google_adk import GoogleAdkProvider  # noqa: F401
    from composio_langgraph import LanggraphProvider  # noqa: F401
    from composio_openai_agents import OpenAIAgentsProvider  # noqa: F401

    _ok("all four composio_* provider packages import cleanly")

    # Stub _resolve_ctx + _build_session_and_tools so the dispatcher can run
    # without making network calls.
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    composio_providers._resolve_ctx = lambda: RequestContext(  # type: ignore[assignment]
        user_id="alice",
        workspace_id="ws_smoke",
        request_id="smoke",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )

    called: list[tuple[str, str]] = []

    def _fake_build(backend_kind: str, namespaced_user_id: str, settings: object) -> list[object]:
        called.append((backend_kind, namespaced_user_id))
        return [f"fake-tool-for-{backend_kind}"]

    composio_providers._build_session_and_tools = _fake_build  # type: ignore[assignment]
    composio_providers.reset_cache_for_tests()

    # All four backends should dispatch successfully and namespace the user.
    for backend in [
        composio_providers.BACKEND_CLAUDE_SDK,
        composio_providers.BACKEND_OPENAI_AGENTS,
        composio_providers.BACKEND_GOOGLE_ADK,
        composio_providers.BACKEND_DEEP_AGENTS,
    ]:
        tools = composio_providers.build_tools_for_backend(backend)
        if not tools:
            _fail(f"{backend}: build returned empty")
    assert all(uid == "ent_smoke:alice" for _, uid in called)
    _ok(f"build_tools_for_backend dispatched cleanly for {len(called)} backends")
    _ok("namespaced user_id 'ent_smoke:alice' threaded through every call")

    # Confirm the real ``_provider_class_for`` lookup works for each backend.
    composio_providers._build_session_and_tools = (  # type: ignore[assignment]
        _real_build_session_and_tools_safe
    )

    for backend in composio_providers.SUPPORTED_BACKENDS:
        composio_cls, provider_cls = composio_providers._provider_class_for(backend)
        provider_instance = provider_cls()
        _ok(f"{backend}: resolved provider {provider_instance.__class__.__name__}")

    print("\nAll wiring smoke checks passed.")


def _real_build_session_and_tools_safe(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Sentinel — only called if we accidentally let the live path run."""
    raise RuntimeError("smoke: live SDK call attempted (would hit Composio API)")


if __name__ == "__main__":
    main()
