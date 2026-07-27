"""Tests for the HerdrRuntime adapter (feat/herdr-runtime-adapter, HR-1).

Created: 2026-07-18.

These tests run WITHOUT a real herdr server. A fake ``herdr`` executable (a
small bash script emitting canned JSON envelopes) is installed on a temp PATH
via fixtures, so the adapter's subprocess/JSON plumbing, status mapping,
command construction, and fail-open behaviour are all exercised hermetically.

Coverage:
  * status mapping (herdr status <-> Mission Control AgentStatus, both ways),
  * happy-path parse for every contract method (spawn/status/read/send/wait/
    worktree_create/worktree_remove/attach_info/list_agents/list_panes),
  * exact command-line construction (via an argv-capture sidecar),
  * fail-open: flag off, binary missing, error envelope, non-JSON, timeout.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pocketpaw.agents.errors import HerdrUnavailable
from pocketpaw.agents.herdr_runtime import (
    HerdrRuntime,
    PaneRef,
    WorktreeRef,
    _to_herdr_status,
    map_agent_status,
)
from pocketpaw.config import Settings
from pocketpaw.mission_control.models import AgentStatus

# --------------------------------------------------------------------------- #
# Fake herdr shim
# --------------------------------------------------------------------------- #

# A stand-in `herdr` binary. Keyed on "$1 $2" (herdr group + subcommand); emits
# the same JSON envelope shapes real herdr v0.7.4 does. HERDR_FAKE_MODE forces a
# failure shape; HERDR_CAPTURE_FILE records the exact argv for assertions.
FAKE_HERDR_SCRIPT = r"""#!/usr/bin/env bash
if [ -n "$HERDR_CAPTURE_FILE" ]; then
  printf '%s\n' "$@" > "$HERDR_CAPTURE_FILE"
fi
mode="${HERDR_FAKE_MODE:-ok}"
if [ "$mode" = "badjson" ]; then
  echo "this is definitely not json"
  exit 0
fi
if [ "$mode" = "hang" ]; then
  sleep 30
  exit 0
fi
if [ "$mode" = "error" ]; then
  echo '{"id":"cli:fake","error":{"code":"boom","message":"simulated herdr failure"}}'
  exit 0
fi
case "$1 $2" in
  "agent list")
    echo '{"id":"cli:agent:list","result":{"type":"agent_list","agents":[{"agent":"claude","agent_status":"idle","pane_id":"w2:p1","terminal_id":"term_a","tab_id":"w2:t1","workspace_id":"w2"},{"agent":"codex","agent_status":"working","pane_id":"w3:p1","terminal_id":"term_b","tab_id":"w3:t1","workspace_id":"w3"}]}}'
    ;;
  "pane list")
    echo '{"id":"cli:pane:list","result":{"type":"pane_list","panes":[{"agent":"claude","agent_status":"idle","pane_id":"w2:p1","terminal_id":"term_a","tab_id":"w2:t1","workspace_id":"w2"}]}}'
    ;;
  "agent get")
    echo '{"id":"cli:agent:get","result":{"type":"agent_info","agent":{"agent":"claude","agent_status":"working","pane_id":"w2:p1","terminal_id":"term_a","tab_id":"w2:t1","workspace_id":"w2"}}}'
    ;;
  "agent read")
    echo '{"id":"cli:agent:read","result":{"type":"pane_read","read":{"format":"text","pane_id":"w2:p1","source":"visible","tab_id":"w2:t1","text":"hello from pane\n> ","truncated":false,"workspace_id":"w2"}}}'
    ;;
  "agent send")
    echo '{"id":"cli:agent:send","result":{"type":"ok"}}'
    ;;
  "agent start")
    echo '{"id":"cli:agent:start","result":{"type":"agent_started","argv":["claude"],"agent":{"agent":"claude","agent_status":"working","pane_id":"w4:p1","terminal_id":"term_new","tab_id":"w4:t1","workspace_id":"w4"}}}'
    ;;
  "wait agent-status")
    echo '{"id":"cli:wait:agent-status","result":{"type":"wait_matched","event":{"kind":"agent_status","pane_id":"w2:p1","status":"idle"}}}'
    ;;
  "wait output")
    echo '{"id":"cli:wait:output","result":{"type":"output_matched","pane_id":"w2:p1","matched_line":"BUILD OK","revision":7,"read":{"format":"text","pane_id":"w2:p1","source":"recent","tab_id":"w2:t1","text":"...BUILD OK...","truncated":false,"workspace_id":"w2"}}}'
    ;;
  "worktree create")
    echo '{"id":"cli:worktree:create","result":{"type":"worktree_created","worktree":{"branch":"feat/x","path":"/tmp/wt/x","open_workspace_id":"w9","is_bare":false,"is_detached":false,"is_linked_worktree":true,"is_prunable":false,"label":"repo"},"workspace":{"workspace_id":"w9","label":"repo","number":9},"root_pane":{"pane_id":"w9:p1","terminal_id":"term_w9","tab_id":"w9:t1","workspace_id":"w9","agent":null,"agent_status":"unknown"},"tab":{"tab_id":"w9:t1","workspace_id":"w9"}}}'
    ;;
  "worktree remove")
    echo '{"id":"cli:worktree:remove","result":{"type":"worktree_removed","forced":false,"path":"/tmp/wt/x","workspace_id":"w9"}}'
    ;;
  "pane close")
    echo '{"id":"cli:pane:close","result":{"type":"pane_closed","pane_id":"w2:p1"}}'
    ;;
  *)
    echo "{\"id\":\"cli:unknown\",\"error\":{\"code\":\"unknown_command\",\"message\":\"fake herdr got: $*\"}}"
    ;;
esac
exit 0
"""

# Keep coreutils + the bash interpreter reachable while excluding the real
# ~/.local/bin/herdr, so `shutil.which` only ever sees our fake (or nothing).
_SAFE_PATH_DIRS = ("/bin", "/usr/bin")


def _path_with(*dirs: str) -> str:
    return os.pathsep.join([*dirs, *_SAFE_PATH_DIRS])


@pytest.fixture
def fake_herdr_bin(tmp_path: Path) -> Path:
    """Write the fake `herdr` executable and return its path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "herdr"
    script.write_text(FAKE_HERDR_SCRIPT)
    script.chmod(0o755)
    return script


@pytest.fixture
def fake_herdr_on_path(fake_herdr_bin: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake `herdr` on a temp PATH (real herdr excluded)."""
    monkeypatch.setenv("PATH", _path_with(str(fake_herdr_bin.parent)))
    monkeypatch.delenv("HERDR_FAKE_MODE", raising=False)
    monkeypatch.delenv("HERDR_CAPTURE_FILE", raising=False)
    return fake_herdr_bin


def _make_runtime(**overrides) -> HerdrRuntime:
    """Build a HerdrRuntime from a hermetic Settings (no .env, explicit flags)."""
    kwargs = {"_env_file": None, "herdr_runtime_enabled": True}
    kwargs.update(overrides)
    return HerdrRuntime(Settings(**kwargs))


@pytest.fixture
def runtime(fake_herdr_on_path: Path) -> HerdrRuntime:
    return _make_runtime()


# --------------------------------------------------------------------------- #
# Pure status-mapping units (no subprocess)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("herdr_status", "expected"),
    [
        ("working", AgentStatus.ACTIVE),
        ("idle", AgentStatus.IDLE),
        ("blocked", AgentStatus.BLOCKED),
        ("unknown", AgentStatus.OFFLINE),
        ("done", AgentStatus.IDLE),  # herdr-only: finished turn ~= available
        ("weird-new-value", AgentStatus.OFFLINE),  # unknown -> fail safe
        (None, AgentStatus.OFFLINE),
        ("WORKING", AgentStatus.ACTIVE),  # case-insensitive
    ],
)
def test_map_agent_status(herdr_status, expected):
    assert map_agent_status(herdr_status) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("idle", "idle"),
        ("working", "working"),
        ("blocked", "blocked"),
        ("done", "done"),  # herdr-only wait status passes through
        ("unknown", "unknown"),
        (AgentStatus.ACTIVE, "working"),  # MC enum reverse-maps
        (AgentStatus.IDLE, "idle"),
        (AgentStatus.BLOCKED, "blocked"),
        (AgentStatus.OFFLINE, "unknown"),
    ],
)
def test_to_herdr_status(value, expected):
    assert _to_herdr_status(value) == expected


def test_to_herdr_status_rejects_garbage():
    with pytest.raises(ValueError, match="unknown wait status"):
        _to_herdr_status("nonsense")


def test_paneref_from_record_extracts_ids():
    ref = PaneRef.from_record(
        {
            "pane_id": "w2:p1",
            "terminal_id": "t",
            "workspace_id": "w2",
            "tab_id": "w2:t1",
            "agent": "claude",
        }
    )
    assert ref.pane_id == "w2:p1"
    assert ref.terminal_id == "t"
    assert ref.workspace_id == "w2"
    assert ref.agent == "claude"


# --------------------------------------------------------------------------- #
# Availability / fail-open
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_flag_off_reports_unavailable(fake_herdr_on_path):
    # Binary present on PATH, but the kill-switch is off.
    rt = _make_runtime(herdr_runtime_enabled=False)
    assert rt.available is False
    assert await rt.probe() is False
    with pytest.raises(HerdrUnavailable, match="flag is off"):
        await rt.status("w2:p1")


@pytest.mark.parametrize("truthy", ["1", "true", "YES", "on"])
@pytest.mark.asyncio
async def test_shared_cloud_mode_refuses_herdr(fake_herdr_on_path, monkeypatch, truthy):
    """The deployment boundary: a shared multi-tenant box refuses herdr outright.

    Flag ON and the binary present — the ONLY reason it stays unavailable is
    ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE``. herdr panes are not workspace-scoped,
    so one tenant could observe another's panes.
    """
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", truthy)
    rt = _make_runtime(herdr_runtime_enabled=True)
    assert rt.available is False
    assert await rt.probe() is False
    with pytest.raises(HerdrUnavailable, match="shared multi-tenant"):
        await rt.status("w2:p1")
    with pytest.raises(HerdrUnavailable, match="shared multi-tenant"):
        await rt.spawn("claude")


@pytest.mark.asyncio
async def test_dedicated_box_still_allows_herdr(fake_herdr_on_path, monkeypatch):
    """The boundary is precise: an unset/falsey scope env leaves herdr usable."""
    for value in ("", "0", "false", "off"):
        monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", value)
        rt = _make_runtime(herdr_runtime_enabled=True)
        assert rt.available is True, f"{value!r} should not trip the boundary"
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    assert _make_runtime(herdr_runtime_enabled=True).available is True


@pytest.mark.asyncio
async def test_binary_missing_reports_unavailable(monkeypatch, tmp_path):
    # Flag on, but no herdr anywhere on PATH.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", _path_with(str(empty)))
    rt = _make_runtime()
    assert rt.available is False
    assert rt.binary is None
    assert await rt.probe() is False
    with pytest.raises(HerdrUnavailable, match="binary not found"):
        await rt.spawn("claude")


@pytest.mark.asyncio
async def test_error_envelope_raises(runtime, monkeypatch):
    monkeypatch.setenv("HERDR_FAKE_MODE", "error")
    with pytest.raises(HerdrUnavailable, match=r"herdr error \[boom\]"):
        await runtime.status("w2:p1")


@pytest.mark.asyncio
async def test_non_json_output_raises(runtime, monkeypatch):
    monkeypatch.setenv("HERDR_FAKE_MODE", "badjson")
    with pytest.raises(HerdrUnavailable, match="non-JSON"):
        await runtime.list_agents()


@pytest.mark.asyncio
async def test_command_timeout_raises(fake_herdr_on_path, monkeypatch):
    monkeypatch.setenv("HERDR_FAKE_MODE", "hang")
    rt = _make_runtime(herdr_cli_timeout_ms=150)  # 0.15s, shim sleeps 30s
    with pytest.raises(HerdrUnavailable, match="timed out"):
        await rt.status("w2:p1")


@pytest.mark.asyncio
async def test_explicit_path_override(fake_herdr_bin, monkeypatch):
    # No herdr on PATH, but herdr_cli_path points straight at the fake.
    monkeypatch.setenv("PATH", _path_with())
    monkeypatch.delenv("HERDR_FAKE_MODE", raising=False)
    monkeypatch.delenv("HERDR_CAPTURE_FILE", raising=False)
    rt = _make_runtime(herdr_cli_path=str(fake_herdr_bin))
    assert rt.available is True
    assert await rt.probe() is True


def test_explicit_path_invalid_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", _path_with())
    rt = _make_runtime(herdr_cli_path=str(tmp_path / "does-not-exist"))
    assert rt.available is False
    assert rt.binary is None


# --------------------------------------------------------------------------- #
# Happy-path parse + map (fake shim)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_probe_true_when_server_answers(runtime):
    assert await runtime.probe() is True


@pytest.mark.asyncio
async def test_status_maps_working_to_active(runtime):
    assert await runtime.status("w2:p1") is AgentStatus.ACTIVE


@pytest.mark.asyncio
async def test_list_agents_parses_records(runtime):
    agents = await runtime.list_agents()
    assert [a.pane_id for a in agents] == ["w2:p1", "w3:p1"]
    assert agents[0].agent == "claude"
    assert agents[1].workspace_id == "w3"


@pytest.mark.asyncio
async def test_list_panes_parses_records(runtime):
    panes = await runtime.list_panes()
    assert [p.pane_id for p in panes] == ["w2:p1"]


@pytest.mark.asyncio
async def test_read_returns_text(runtime):
    text = await runtime.read("w2:p1", source="visible", lines=10)
    assert text == "hello from pane\n> "


@pytest.mark.asyncio
async def test_send_returns_none(runtime):
    assert await runtime.send("w2:p1", "hello") is None


@pytest.mark.asyncio
async def test_attach_info_shape(runtime):
    info = await runtime.attach_info("w2:p1")
    assert info == {
        "pane_id": "w2:p1",
        "workspace_id": "w2",
        "tab_id": "w2:t1",
        "terminal_id": "term_a",
        "agent": "claude",
        "agent_status": "working",
    }


@pytest.mark.asyncio
async def test_spawn_returns_paneref(runtime):
    ref = await runtime.spawn("claude", argv=["claude"])
    assert isinstance(ref, PaneRef)
    assert ref.pane_id == "w4:p1"
    assert ref.workspace_id == "w4"


@pytest.mark.asyncio
async def test_close_invokes_pane_close(runtime, monkeypatch, tmp_path):
    cap = tmp_path / "close_args.txt"
    monkeypatch.setenv("HERDR_CAPTURE_FILE", str(cap))
    assert await runtime.close(PaneRef(pane_id="w4:p1")) is None
    assert cap.read_text().split() == ["pane", "close", "w4:p1"]


@pytest.mark.asyncio
async def test_close_accepts_raw_pane_id(runtime):
    assert await runtime.close("w2:p1") is None


@pytest.mark.asyncio
async def test_close_raises_when_unavailable(fake_herdr_on_path):
    rt = _make_runtime(herdr_runtime_enabled=False)
    with pytest.raises(HerdrUnavailable):
        await rt.close("w2:p1")


@pytest.mark.asyncio
async def test_worktree_create_returns_ref(runtime):
    wt = await runtime.worktree_create(branch="feat/x")
    assert isinstance(wt, WorktreeRef)
    assert wt.workspace_id == "w9"
    assert wt.path == "/tmp/wt/x"
    assert wt.branch == "feat/x"
    assert wt.root_pane_id == "w9:p1"


@pytest.mark.asyncio
async def test_worktree_remove_returns_result(runtime):
    result = await runtime.worktree_remove(WorktreeRef(workspace_id="w9"))
    assert result["workspace_id"] == "w9"
    assert result["type"] == "worktree_removed"


@pytest.mark.asyncio
async def test_wait_requires_exactly_one_condition(runtime):
    with pytest.raises(ValueError, match="exactly one"):
        await runtime.wait("w2:p1")
    with pytest.raises(ValueError, match="exactly one"):
        await runtime.wait("w2:p1", status="idle", output_match="x")


@pytest.mark.asyncio
async def test_wait_status_returns_match(runtime):
    result = await runtime.wait("w2:p1", status="idle", timeout_ms=1000)
    assert result["type"] == "wait_matched"


@pytest.mark.asyncio
async def test_wait_output_returns_match(runtime):
    result = await runtime.wait("w2:p1", output_match="BUILD OK", regex=True, timeout_ms=1000)
    assert result["type"] == "output_matched"


# --------------------------------------------------------------------------- #
# Exact command-line construction (argv capture)
# --------------------------------------------------------------------------- #


@pytest.fixture
def capture(fake_herdr_on_path, tmp_path, monkeypatch):
    """Capture the exact argv the adapter passes to herdr."""
    cap = tmp_path / "argv.txt"
    monkeypatch.setenv("HERDR_CAPTURE_FILE", str(cap))

    def read_argv() -> list[str]:
        return cap.read_text().splitlines()

    return read_argv


@pytest.mark.asyncio
async def test_spawn_builds_full_command(runtime, capture):
    await runtime.spawn(
        "claude",
        argv=["claude", "--dangerously-skip-permissions"],
        cwd="/repo",
        workspace="w2",
        env={"FOO": "bar"},
        split="right",
    )
    assert capture() == [
        "agent",
        "start",
        "claude",
        "--cwd",
        "/repo",
        "--workspace",
        "w2",
        "--split",
        "right",
        "--env",
        "FOO=bar",
        "--no-focus",
        "--",
        "claude",
        "--dangerously-skip-permissions",
    ]


@pytest.mark.asyncio
async def test_spawn_worktree_ref_fills_workspace_and_cwd(runtime, capture):
    await runtime.spawn("claude", worktree=WorktreeRef(workspace_id="w9", path="/wt/x"))
    argv = capture()
    assert "--workspace" in argv and argv[argv.index("--workspace") + 1] == "w9"
    assert "--cwd" in argv and argv[argv.index("--cwd") + 1] == "/wt/x"


@pytest.mark.asyncio
async def test_wait_status_reverse_maps_mc_enum(runtime, capture):
    await runtime.wait("w2:p1", status=AgentStatus.ACTIVE, timeout_ms=500)
    argv = capture()
    assert argv[:4] == ["wait", "agent-status", "w2:p1", "--status"]
    assert argv[4] == "working"
    assert argv[-2:] == ["--timeout", "500"]


@pytest.mark.asyncio
async def test_wait_output_builds_command(runtime, capture):
    await runtime.wait("w2:p1", output_match="BUILD OK", regex=True, timeout_ms=1000)
    assert capture() == [
        "wait",
        "output",
        "w2:p1",
        "--match",
        "BUILD OK",
        "--regex",
        "--timeout",
        "1000",
    ]


@pytest.mark.asyncio
async def test_worktree_create_passes_json_flag(runtime, capture):
    await runtime.worktree_create(branch="feat/x", cwd="/repo")
    argv = capture()
    assert argv[:3] == ["worktree", "create", "--json"]
    assert "--branch" in argv and argv[argv.index("--branch") + 1] == "feat/x"
    assert "--cwd" in argv and argv[argv.index("--cwd") + 1] == "/repo"


@pytest.mark.asyncio
async def test_worktree_remove_builds_command(runtime, capture):
    await runtime.worktree_remove("w9", force=True)
    assert capture() == ["worktree", "remove", "--workspace", "w9", "--json", "--force"]
