import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.router import AgentRouter
from pocketpaw.config import Settings


class FailingBackend:
    @staticmethod
    def info():
        class Info:
            display_name = "Failing Backend"

        return Info()

    def __init__(self, settings):
        pass

    async def run(self, *args, **kwargs):
        raise RuntimeError("backend failure")
        yield  # required so pytest treats it async generator

    async def stop(self):
        pass


class ErrorEventBackend:
    """Backend that emits an error event instead of raising."""

    @staticmethod
    def info():
        class Info:
            display_name = "Error Event Backend"

        return Info()

    def __init__(self, settings):
        pass

    async def run(self, *args, **kwargs):
        yield AgentEvent(type="error", content="backend error")

    async def stop(self):
        pass


class StreamingBackend:
    """Backend that emits multiple streaming events before done."""

    @staticmethod
    def info():
        class Info:
            display_name = "Streaming Backend"

        return Info()

    def __init__(self, settings):
        pass

    async def run(self, *args, **kwargs):
        yield AgentEvent(type="message", content="chunk1")
        yield AgentEvent(type="message", content="chunk2")
        yield AgentEvent(type="message", content="chunk3")
        yield AgentEvent(type="done", content="")

    async def stop(self):
        pass


class SuccessBackend:
    """Backend that always succeeds."""

    @staticmethod
    def info():
        class Info:
            display_name = "Success Backend"

        return Info()

    def __init__(self, settings):
        pass

    async def run(self, *args, **kwargs):
        yield AgentEvent(type="message", content="fallback worked")
        yield AgentEvent(type="done", content="")

    async def stop(self):
        pass


@pytest.mark.asyncio
async def test_router_fallback_success(monkeypatch):
    """Primary backend fails → fallback backend succeeds."""

    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "failing_backend",
        ("tests.test_router_fallback", "FailingBackend"),
    )

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "success_backend",
        ("tests.test_router_fallback", "SuccessBackend"),
    )

    settings = Settings(
        agent_backend="failing_backend",
        fallback_backends=["success_backend"],
    )

    router = AgentRouter(settings)

    events = []
    async for event in router.run("hello"):
        events.append(event)

    assert any(e.content == "fallback worked" for e in events)


@pytest.mark.asyncio
async def test_router_error_event_fallback(monkeypatch):
    """Primary backend emits error event → fallback backend succeeds."""

    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "error_backend",
        ("tests.test_router_fallback", "ErrorEventBackend"),
    )

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "success_backend",
        ("tests.test_router_fallback", "SuccessBackend"),
    )

    settings = Settings(
        agent_backend="error_backend",
        fallback_backends=["success_backend"],
    )

    router = AgentRouter(settings)

    events = []
    async for event in router.run("hello"):
        events.append(event)

    assert any(e.content == "fallback worked" for e in events)


@pytest.mark.asyncio
async def test_router_all_backends_fail(monkeypatch):
    """Primary and fallback both fail → router returns error."""

    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "fail_backend",
        ("tests.test_router_fallback", "FailingBackend"),
    )

    settings = Settings(
        agent_backend="fail_backend",
        fallback_backends=["fail_backend"],
    )

    router = AgentRouter(settings)

    events = []
    async for event in router.run("hello"):
        events.append(event)

    assert any(e.type == "error" for e in events)


@pytest.mark.asyncio
async def test_router_streaming_happy_path(monkeypatch):
    """Router should stream multiple events from backend without triggering fallback."""

    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "stream_backend",
        ("tests.test_router_fallback", "StreamingBackend"),
    )

    settings = Settings(agent_backend="stream_backend")

    router = AgentRouter(settings)

    events = []

    async for event in router.run("hello"):
        events.append(event)

    contents = [e.content for e in events if e.type == "message"]

    assert contents == ["chunk1", "chunk2", "chunk3"]


@pytest.mark.asyncio
async def test_router_no_fallback(monkeypatch):
    """Primary backend fails and no fallback configured."""

    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY,
        "fail_backend",
        ("tests.test_router_fallback", "FailingBackend"),
    )

    settings = Settings(agent_backend="fail_backend")

    router = AgentRouter(settings)

    events = []
    async for event in router.run("hello"):
        events.append(event)

    assert any(e.type == "error" for e in events)
