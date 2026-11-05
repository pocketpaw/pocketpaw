# tests/ee/sites/site_ai_harness.py: shared fixtures for the site AI suites.
#
# Builds a FastAPI app over the site AI routers with a stubbed session (role per
# test), inserts pockets and sites, and exposes ``WS``/``OTHER_WS`` workspaces.
# No network: the model seam (``cloud.chat.side_completion``) is faked per suite
# by ``FakeLlm``; ``ok`` / ``failed`` / ``text_events`` build its results.
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


def client(
    workspace_id: str = WS, user_id: str = OWNER, role: str = "admin", ip: str = "203.0.113.7"
) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=build_app(workspace_id, user_id, role), client=(ip, 4000)),
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


DEFAULT_AGENT = "agent-default"
# Unpriced on purpose: a call then costs the alias fallback rate, the same rate its
# hold was sized at, so cap tests stay exact.
RAN_MODEL = "site-ai-test-model"


def ok(text: str = "Hello there", tokens_in: int = 10, tokens_out: int = 5, **kw):
    """A finished side completion, as ``agent_complete`` returns it."""
    from pocketpaw_ee.cloud.chat.side_completion import SideCompletion

    fields = {
        "text": text,
        "model": RAN_MODEL,
        "backend": "claude_agent_sdk",
        "agent_id": DEFAULT_AGENT,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        "payer": "platform",
        "sent": True,
    }
    fields.update(kw)
    return SideCompletion(**fields)


def failed(failure: str, code: str = "x", *, sent: bool = False, usage=None, **kw):
    from pocketpaw_ee.cloud.chat.side_completion import SideCompletion

    fields = {
        "failure": failure,
        "code": code,
        "sent": sent,
        "usage": usage,
        "agent_id": DEFAULT_AGENT,
        "backend": "claude_agent_sdk",
        "payer": "platform",
    }
    fields.update(kw)
    return SideCompletion(**fields)


def text_events(text: str = "Hello there", tokens_in: int = 10, tokens_out: int = 5):
    """One streamed answer: the delta, then the finished result."""
    return [text, ok(text, tokens_in, tokens_out)]


class FakeLlm:
    """Stands in for ``cloud.chat.side_completion``: ``agent_stream`` yields
    ``script`` (text deltas and then a ``SideCompletion``; an exception is raised
    where it stands), ``agent_complete`` returns the script's ``SideCompletion`` (or
    ``result``). Every call's kwargs are kept in ``calls``; ``payer`` is who
    ``resolve_target`` says pays (None = no agent resolves)."""

    def __init__(self, monkeypatch, script=None, result=None, payer="platform") -> None:
        from pocketpaw_ee.cloud.chat import side_completion

        self.calls: list[dict] = []
        script = list(script if script is not None else text_events())
        if script and isinstance(script[0], list):  # one attempt, as [[...]]
            script = list(script[0])
        self.script = script
        self.result = result
        self.payer = payer
        self.payer_calls: list[tuple] = []
        self.gate = None  # an asyncio.Event a call waits on, for burst tests
        monkeypatch.setattr(side_completion, "agent_stream", self._stream)
        monkeypatch.setattr(side_completion, "agent_complete", self._complete)
        monkeypatch.setattr(side_completion, "resolve_target", self._resolve_target)
        from pocketpaw_ee.site_ai import chat

        self.member = True  # what ``chat.owner_is_member`` answers

        async def _member(workspace_id, owner_id):
            return self.member

        monkeypatch.setattr(chat, "owner_is_member", _member)

    @property
    def payloads(self) -> list[dict]:
        return self.calls

    async def _resolve_target(self, workspace_id, user_id, agent_id):
        from pocketpaw_ee.cloud.chat.side_completion import SideTarget

        self.payer_calls.append((workspace_id, user_id, agent_id))
        if self.payer is None:
            return None
        return SideTarget(agent_id=agent_id or DEFAULT_AGENT, payer=self.payer)

    async def _stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate is not None:
            await self.gate.wait()
        for item in self.script:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate is not None:
            await self.gate.wait()
        if self.result is not None:
            item = self.result
        else:
            item = next(i for i in reversed(self.script) if not isinstance(i, str))
        if isinstance(item, BaseException):
            raise item
        return item


def parse_sse(body: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines.get("event", ""), json.loads(lines.get("data", "{}"))))
    return events
