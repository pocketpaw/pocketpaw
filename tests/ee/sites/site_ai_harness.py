# tests/ee/sites/site_ai_harness.py: shared fixtures for the site AI suites.
#
# Builds a FastAPI app over the site AI routers with a stubbed session (role per
# test), inserts pockets and sites, and exposes ``WS``/``OTHER_WS`` workspaces.
# No network: the LLM seam is faked per suite.
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

WS = "ws_ai"
OTHER_WS = "ws_ai_other"
OWNER = "user-owner"
SITE_KEY = "site_key_" + "a" * 32
SITE_HOST = "app.example.com"
ORIGIN = f"https://{SITE_HOST}"
GATEWAY_TOKEN = "gw-token-" + "z" * 40


class _Membership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


def build_app(workspace_id: str = WS, user_id: str = OWNER, role: str = "admin") -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.site_ai import router as site_ai_router

    user = SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[_Membership(workspace_id, role)],
    )
    app = FastAPI()
    add_error_handler(app)
    app.include_router(site_ai_router.router, prefix="/api/v1")
    public = getattr(site_ai_router, "public_router", None)
    if public is not None:
        app.include_router(public, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def client(workspace_id: str = WS, user_id: str = OWNER, role: str = "admin") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=build_app(workspace_id, user_id, role)),
        base_url="https://api.test",
    )


async def make_pocket(workspace: str = WS, owner: str = OWNER) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=workspace,
        name="App",
        type="site",
        owner=owner,
        visibility="workspace",
        engine="html",
        widgets=[],
    )
    await doc.insert()
    return str(doc.id)


async def make_site(pocket_id: str, workspace: str = WS, plan_tier: str | None = "staff"):
    from pocketpaw_ee.cloud.models.site import Site

    site = Site(
        workspace=workspace,
        pocket_id=pocket_id,
        owner=OWNER,
        script_name=f"app-{pocket_id[-6:]}",
        signed_key=SITE_KEY,
        allowed_origins=[SITE_HOST],
        plan_tier=plan_tier,
    )
    await site.insert()
    return site


async def gateway_token(site) -> str:
    from pocketpaw_ee.integrations import bindings
    from pocketpaw_ee.sites import site_secrets

    await bindings.ensure_gateway_token(site.workspace, site.pocket_id)
    return (await site_secrets.secrets_for_deploy(site))["PAW_GATEWAY_TOKEN"]


class FakeLlm:
    """Stands in for ``site_ai.llm.stream`` / ``complete``. ``script`` is a list of
    attempts; each attempt is a list of ``LlmEvent``s and/or an exception to raise
    at that point. Payloads sent are kept in ``payloads``."""

    def __init__(self, monkeypatch, script=None, result=None) -> None:
        from pocketpaw_ee.site_ai import llm

        self.payloads: list[dict] = []
        self.script = list(script or [])
        self.result = result
        self.gate = None  # an asyncio.Event a stream waits on, for burst tests
        monkeypatch.setattr(llm, "stream", self._stream)
        monkeypatch.setattr(llm, "complete", self._complete)

    def _next(self):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    async def _stream(self, payload):
        self.payloads.append(payload)
        if self.gate is not None:
            await self.gate.wait()
        for item in self._next():
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _complete(self, payload):
        self.payloads.append(payload)
        if self.gate is not None:
            await self.gate.wait()
        item = self._next() if self.script else self.result
        if isinstance(item, list):
            item = item[0]
        if isinstance(item, BaseException):
            raise item
        return item


def text_events(text: str = "Hello there", tokens_in: int = 10, tokens_out: int = 5):
    from pocketpaw_ee.site_ai.llm import LlmEvent

    return [
        LlmEvent("text", text=text),
        LlmEvent("finish", reason="stop"),
        LlmEvent("usage", input_tokens=tokens_in, output_tokens=tokens_out),
    ]


def parse_sse(body: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines.get("event", ""), json.loads(lines.get("data", "{}"))))
    return events
