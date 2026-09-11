# tests/cloud/test_belt_entity_events.py — the per-FILE live feed within a Belt
# station run (feat/belt-entity-events).
#
# Created: 2026-09-12. Pins the contract a /belt UI follows a run with:
#   * ``mint_entity_id`` / ``repo_slug_for`` — pure id minting in loom's
#     ``<repo_slug>:file:<relpath>`` form, tested directly on several paths.
#   * ``emit_belt_entity_changed`` — the full payload on the WORKSPACE BUS
#     (primary, captured via the conftest ``recording_bus``) plus the in-turn
#     SSE (secondary). A bus failure is swallowed.
#   * ``maybe_emit_belt_entity_changed`` — the filter: BELT surface only,
#     Write/Edit only, inside the run's bound repo only.
#
# The bus is the REAL one (conftest swaps in a RecordingBus), not a mock of the
# emitter — over-mocking the seam under test hides live bugs. Only the two
# genuinely external things are substituted: the per-stream SSE sink (there is
# no stream in a unit test) and, in the failure test, the bus publish itself.
#
# NOT covered here, and deliberately: driving ``run_core._drive_agent_loop``
# end-to-end. ``test_run_core_calls_the_bridge`` asserts the call site exists by
# reading the source instead — it catches a refactor that drops the call, but it
# does NOT prove the arguments run_core passes are right. See the module comment
# in ``belt/service.py``.
#
# ``pocketpaw_ee`` is import-skipped on an OSS-only install. These tests are
# under ``tests/cloud``, which the root pytest addopts ignores — run them with an
# explicit path: ``uv run pytest tests/cloud/test_belt_entity_events.py -q``.

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt.service import (  # noqa: E402
    emit_belt_entity_changed,
    maybe_emit_belt_entity_changed,
    mint_entity_id,
    repo_slug_for,
)

_BELT = "belt"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _SSECapture:
    """Capture push_sse_event calls in place of the real per-stream sink."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, name: str, data: dict) -> None:
        self.events.append((name, data))

    def entity_events(self) -> list[dict]:
        return [data for name, data in self.events if name == "belt_entity_changed"]


@pytest.fixture
def sse(monkeypatch) -> _SSECapture:
    cap = _SSECapture()
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.push_sse_event", cap, raising=True)
    return cap


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repo root named like a real checkout (mixed case, to pin the slug)."""
    root = tmp_path / "pocketPaw"
    (root / "src" / "pocketpaw").mkdir(parents=True)
    return root


def _bus_entity_events(recording_bus) -> list[dict]:
    return [e.data for e in recording_bus.events if e.type == "belt_entity_changed"]


async def _bridge(recording_bus, **overrides) -> list[dict]:
    """Run the bridge with sane BELT defaults and return what hit the bus."""
    kwargs: dict = {
        "surface": _BELT,
        "tool_name": "Write",
        "tool_input": {},
        "workspace_id": "w1",
        "run_id": "run-1",
        "repo_root": None,
        **overrides,
    }
    await maybe_emit_belt_entity_changed(**kwargs)
    return _bus_entity_events(recording_bus)


# ---------------------------------------------------------------------------
# The pure id minting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo_slug", "relpath", "expected"),
    [
        ("pocketpaw", "src/pocketpaw/config.py", "pocketpaw:file:src/pocketpaw/config.py"),
        ("pocketpaw", "README.md", "pocketpaw:file:README.md"),
        ("soul-protocol", "src/soul/cli.py", "soul-protocol:file:src/soul/cli.py"),
        # A path with a dot segment in a directory name stays verbatim — the id
        # is the world-model key, not a display string.
        ("kb-go", ".github/workflows/ci.yml", "kb-go:file:.github/workflows/ci.yml"),
    ],
)
def test_mint_entity_id(repo_slug: str, relpath: str, expected: str) -> None:
    assert mint_entity_id(repo_slug, relpath) == expected


def test_repo_slug_is_the_lowercased_dir_name() -> None:
    """loom's ids are lowercase (``pocketpaw:file:...``) while the checkout on
    disk is ``pocketPaw`` — the slug must bridge that or no id ever joins."""
    assert repo_slug_for("/Users/x/paw-workspace/pocketPaw") == "pocketpaw"
    assert repo_slug_for("/Users/x/soul-protocol") == "soul-protocol"
    # A trailing slash must not produce an empty slug.
    assert repo_slug_for("/Users/x/kb-go/") == "kb-go"


# ---------------------------------------------------------------------------
# The emitter — full payload on the bus (primary) + SSE (secondary)
# ---------------------------------------------------------------------------


async def test_emitter_publishes_the_full_payload(recording_bus, sse) -> None:
    await emit_belt_entity_changed(
        workspace_id="w1",
        run_id="run-1",
        entity_id="pocketpaw:file:src/a.py",
        file="src/a.py",
        change="write",
    )
    events = _bus_entity_events(recording_bus)
    assert len(events) == 1
    ev = events[0]
    # Every documented key is present, including the ones that are None, so a
    # consumer reads a fixed shape instead of probing for optional keys.
    assert set(ev) == {
        "workspace_id",
        "run_id",
        "action_id",
        "entity_id",
        "file",
        "change",
        "component",
        "ts",
    }
    assert ev["workspace_id"] == "w1"
    assert ev["run_id"] == "run-1"
    assert ev["entity_id"] == "pocketpaw:file:src/a.py"
    assert ev["file"] == "src/a.py"
    assert ev["change"] == "write"
    # None until a run row exists / a real loom resolver is injected.
    assert ev["action_id"] is None
    assert ev["component"] is None
    assert ev["ts"].endswith("+00:00")  # UTC ISO-8601
    # Secondary in-turn path mirrors it.
    assert sse.entity_events() == [ev]


async def test_component_seam_fills_the_field_and_never_breaks_the_emit(recording_bus, sse) -> None:
    """The seam is injected per call — no process-global to set. A resolver that
    raises degrades to ``component: None`` rather than dropping the event."""

    def owner(entity_id: str) -> str | None:
        assert entity_id == "pocketpaw:file:src/a.py"
        return "AgentRuntime"

    def boom(entity_id: str) -> str | None:
        raise RuntimeError("loom is down")

    for resolver, expected in ((owner, "AgentRuntime"), (boom, None)):
        await emit_belt_entity_changed(
            workspace_id="w1",
            run_id="run-1",
            entity_id="pocketpaw:file:src/a.py",
            file="src/a.py",
            change="edit",
            resolve_component=resolver,
        )
    events = _bus_entity_events(recording_bus)
    assert [e["component"] for e in events] == ["AgentRuntime", None]


async def test_bus_failure_does_not_raise(recording_bus, sse, monkeypatch) -> None:
    """A dead bus must not take the station run down with it. The emitter
    imports ``emit`` lazily, so patching the module attribute is the seam."""

    async def _boom(event) -> None:
        raise RuntimeError("bus is down")

    monkeypatch.setattr("pocketpaw_ee.cloud._core.realtime.emit.emit", _boom, raising=True)

    await emit_belt_entity_changed(
        workspace_id="w1",
        run_id="run-1",
        entity_id="pocketpaw:file:src/a.py",
        file="src/a.py",
        change="write",
    )
    assert _bus_entity_events(recording_bus) == []
    # The secondary path still ran — one dead transport doesn't disable the other.
    assert len(sse.entity_events()) == 1


async def test_bridge_survives_a_failing_emit(recording_bus, sse, monkeypatch, repo) -> None:
    """Same guarantee one level out: the bridge swallows an emitter blow-up so a
    tool_use event can never abort the turn that produced it."""

    async def _boom(**kwargs) -> None:
        raise RuntimeError("emit exploded")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.belt.service.emit_belt_entity_changed", _boom, raising=True
    )
    assert (
        await _bridge(
            recording_bus,
            tool_input={"file_path": str(repo / "src" / "a.py")},
            repo_root=str(repo),
        )
        == []
    )


# ---------------------------------------------------------------------------
# The bridge — the filter
# ---------------------------------------------------------------------------


async def test_write_emits_exactly_one_event_with_the_right_entity_id(
    recording_bus, sse, repo
) -> None:
    events = await _bridge(
        recording_bus,
        tool_name="Write",
        tool_input={"file_path": str(repo / "src" / "pocketpaw" / "config.py")},
        repo_root=str(repo),
    )
    assert len(events) == 1
    assert events[0]["entity_id"] == "pocketpaw:file:src/pocketpaw/config.py"
    assert events[0]["file"] == "src/pocketpaw/config.py"
    assert events[0]["change"] == "write"
    assert events[0]["run_id"] == "run-1"


async def test_edit_emits_change_edit(recording_bus, sse, repo) -> None:
    events = await _bridge(
        recording_bus,
        tool_name="Edit",
        tool_input={"file_path": str(repo / "src" / "pocketpaw" / "config.py")},
        repo_root=str(repo),
    )
    assert [e["change"] for e in events] == ["edit"]


@pytest.mark.parametrize("tool_name", ["Read", "Bash", "Glob", "Grep", "NotebookEdit", "WebFetch"])
async def test_non_mutating_tools_emit_nothing(recording_bus, sse, repo, tool_name: str) -> None:
    """Only the two tools that MUTATE a file are entity changes. Reading one is
    not a change, and a Bash command's path is a guess, not a fact."""
    assert (
        await _bridge(
            recording_bus,
            tool_name=tool_name,
            tool_input={"file_path": str(repo / "src" / "a.py"), "command": "sed -i s/a/b/ x.py"},
            repo_root=str(repo),
        )
        == []
    )


async def test_path_outside_the_repo_emits_nothing(recording_bus, sse, repo, tmp_path) -> None:
    """A run's feed only reports its own repo — an absolute path elsewhere, a
    sibling checkout, and a ``..`` traversal out all stay silent."""
    outside = tmp_path / "other-repo" / "secret.py"
    traversal = repo / ".." / "other-repo" / "secret.py"
    for path in ("/etc/passwd", str(outside), str(traversal)):
        assert (
            await _bridge(recording_bus, tool_input={"file_path": path}, repo_root=str(repo)) == []
        ), path


@pytest.mark.parametrize("surface", [None, "chat", "home", "code", "pocket", "sites"])
async def test_a_non_belt_run_emits_nothing(recording_bus, sse, repo, surface) -> None:
    """The /code surface has Write and Edit too. Only belt runs feed this."""
    assert (
        await _bridge(
            recording_bus,
            surface=surface,
            tool_input={"file_path": str(repo / "src" / "a.py")},
            repo_root=str(repo),
        )
        == []
    )


async def test_unbound_repo_emits_nothing(recording_bus, sse, repo) -> None:
    """/belt's ask-first behavior: before the page binds a repo there is nothing
    to make a path relative to, so the feed stays quiet rather than guessing."""
    assert (
        await _bridge(
            recording_bus, tool_input={"file_path": str(repo / "src" / "a.py")}, repo_root=None
        )
        == []
    )


@pytest.mark.parametrize(
    "tool_input",
    [
        {},
        {"file_path": ""},
        {"file_path": None},
        {"file_path": 42},
        {"path": "src/a.py"},  # the wrong key — Write/Edit use ``file_path``
        "not a dict",
        None,
    ],
)
async def test_unreadable_tool_input_emits_nothing(recording_bus, sse, repo, tool_input) -> None:
    assert await _bridge(recording_bus, tool_input=tool_input, repo_root=str(repo)) == []


async def test_a_relative_path_resolves_against_the_repo(recording_bus, sse, repo) -> None:
    """Claude Code sends absolute paths, but a relative one must land in the
    repo rather than against the process cwd (which is not the station's repo)."""
    events = await _bridge(
        recording_bus, tool_input={"file_path": "src/pocketpaw/config.py"}, repo_root=str(repo)
    )
    assert [e["file"] for e in events] == ["src/pocketpaw/config.py"]


# ---------------------------------------------------------------------------
# Audience — the event reaches the workspace, like belt_run_updated
# ---------------------------------------------------------------------------


async def test_audience_is_workspace_scoped() -> None:
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.events import BeltEntityChanged

    async def _members(wid: str) -> list[str]:
        assert wid == "w1"
        return ["u1", "u2"]

    resolver = AudienceResolver(workspace_members=_members)
    audience = await resolver.audience(BeltEntityChanged(data={"workspace_id": "w1"}))
    assert set(audience) == {"u1", "u2"}
    # No workspace_id → no fan-out (defensive, same as belt_run_updated).
    assert await resolver.audience(BeltEntityChanged(data={"file": "a.py"})) == []


# ---------------------------------------------------------------------------
# The call site
# ---------------------------------------------------------------------------


def test_run_core_calls_the_bridge() -> None:
    """The bridge is only useful if the agent stream actually reaches it, and
    nothing else in this file would notice its removal. A source assertion is
    the honest cheap guard: it catches a refactor that drops the call, and it
    does NOT prove the arguments passed are correct."""
    from pocketpaw_ee.cloud.chat.runs import run_core

    src = inspect.getsource(run_core)
    assert "maybe_emit_belt_entity_changed" in src
    # The provisional ``content_block_start`` announcement carries ``input={}``;
    # emitting on it as well as on the resolved AssistantMessage event would
    # double every change.
    assert "input_pending" in src
