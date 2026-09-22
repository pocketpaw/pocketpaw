# tests/test_claude_sdk_persistent_client_images.py — the LEGACY persistent
# client carries an attached image too.
#
# Created: 2026-09-15 (fix/chat-image-persistent-client).
#
# The bug this file exists for, reported live: "I can see screenshot-compare.jpg
# is attached, but the image itself isn't coming through to me on this turn —
# I'm only getting the filename and size (20.6 KB), not the pixels."
#
# That is a turn where the TEXT note arrived and the picture did not, which is
# exactly what happens when ``image_attachments`` never reaches the wire.
#
# #2171 wired the image payload into TWO of the three places this backend sends
# a turn, and both of them live inside ``_leased_dispatch`` — the warm-reuse
# send and the supervised-fresh-build send. ``_leased_dispatch`` runs ONLY when
# the caller passes ``warm_client`` or ``on_client_built``, i.e. when the
# SessionSupervisor is driving. The module's own docstring calls the other route
# "the unchanged legacy ``self._client`` path" and it is what runs when neither
# is set — and it sends ``await _persistent_client.query(message)``, the bare
# string. So on that path the images are silently dropped.
#
# ``_get_or_create_client`` is documented as returning "a persistent
# ClaudeSDKClient", which is precisely the class the SDK docs say supports image
# attachments:
#
#   "Streaming input mode ... Image uploads: attach images directly to messages
#    for visual analysis and understanding"
#   "Single message input mode does NOT support: Direct image attachments in
#    messages"
#   — https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode
#
# So this is not a capability limit. The persistent client can carry the image;
# this path just never handed it one. (The stateless ``query()`` fallback truly
# cannot, per the second quote — that one is a real limit, not a wiring gap.)

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pocketpaw.agents.backend import ImageAttachment
from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 8
IMG = ImageAttachment(data=PNG, media_type="image/png", filename="screenshot-compare.png")


def _make_settings(**overrides):
    defaults = {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": None,
        "sdk_load_bundled_skills": False,
        "anthropic_api_key": "sk-test-key",
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])


class _ResultMsg:
    def __init__(self):
        self.is_error = False
        self.result = "ok"
        self.total_cost_usd = None
        self.usage = {}


class _FakeClient:
    """Records what the backend actually handed ``query()``."""

    def __init__(self, sink: list, options=None, **_kw):
        self._sink = sink
        self.options = options
        self.connected = False

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, prompt, session_id="default"):
        self._sink.append(prompt)

    async def receive_messages(self):
        yield _ResultMsg()

    async def disconnect(self):
        self.connected = False

    async def interrupt(self):
        pass


def _make_sdk(sink):
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(_make_settings())
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._ClaudeAgentOptions = _Options
    sdk._ResultMessage = _ResultMsg
    sdk._ClaudeSDKClient = lambda **kwargs: _FakeClient(sink, **kwargs)
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None
    return sdk


async def _drive(sdk, message, **run_kwargs):
    async def _go():
        out = []
        async for ev in sdk.run(
            message,
            system_prompt="identity",
            session_key="s1",
            **run_kwargs,
        ):
            out.append(ev)
        return out

    selection = ModelSelection(
        complexity=TaskComplexity.MODERATE,
        model="claude-sonnet-4-5-20250929",
        reason="test",
    )
    with patch(_LLM_CLIENT) as mock_resolve:
        mock_llm = MagicMock()
        for flag in (
            "is_ollama",
            "is_openai_compatible",
            "is_gemini",
            "is_litellm",
            "is_openrouter",
        ):
            setattr(mock_llm, flag, False)
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm
        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}):
                return await _go()


async def _collect(payload) -> list:
    """Drain whatever was handed to ``query()`` into a list of messages."""
    if isinstance(payload, str):
        return []
    return [m async for m in payload]


async def test_the_legacy_persistent_client_sends_the_image() -> None:
    """A turn with an attachment, on the path that runs when no supervisor is
    driving, must put the image on the wire.

    FAILS before the fix: the legacy ``self._client`` branch sends the bare
    ``message`` string, so the model gets the attachments note and no pixels —
    the reported symptom."""
    sink: list = []
    sdk = _make_sdk(sink)

    await _drive(sdk, "what changed between these two?", image_attachments=(IMG,))

    assert sink, "the turn must have reached a client at all"
    msgs = await _collect(sink[0])
    assert msgs, (
        "the persistent client was handed a bare string, so the attached image "
        "never reached the model — it is a streaming ClaudeSDKClient and the SDK "
        "docs list image uploads as a streaming-input capability"
    )
    content = msgs[0]["message"]["content"]
    kinds = [b["type"] for b in content]
    assert "image" in kinds, f"no image block on the wire: {kinds}"
    image = next(b for b in content if b["type"] == "image")
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"


async def test_a_text_only_turn_still_sends_a_plain_string() -> None:
    """The withhold-when-empty half. Every turn that came without an attachment
    must keep the exact call it has always made — a bare string, not a
    one-element parts list — because that is what every existing run sends."""
    sink: list = []
    sdk = _make_sdk(sink)

    await _drive(sdk, "no attachment here")

    assert sink, "the turn must have reached a client at all"
    assert isinstance(sink[0], str), (
        "a text-only turn must stay byte-identical to before; wrapping it in a "
        "streaming envelope changes the shape of every existing turn"
    )
    assert sink[0] == "no attachment here"
