"""Tests for recent_files.py — RecentFilesTracker and Bash path heuristic.

[FI] Fix: implement missing Bash command path heuristic in recent_files.py.

The _TOOL_PATH_KEYS dict listed "Bash": [] with a comment saying the path
would be "handled separately via heuristic", but no heuristic existed.
This meant file paths accessed via Bash (e.g. `cat /tmp/report.txt`) were
never recorded in the recent files list.
"""

from __future__ import annotations

from pocketpaw.recent_files import (
    RecentFilesTracker,
    _extract_path_from_bash,
    _extract_path_from_tool,
)


class TestExtractPathFromBash:
    """Unit tests for the Bash path heuristic."""

    def test_absolute_path_cat(self):
        assert _extract_path_from_bash("cat /tmp/report.txt") == "/tmp/report.txt"

    def test_absolute_path_python(self):
        assert _extract_path_from_bash("python /home/user/script.py") == "/home/user/script.py"

    def test_tilde_path(self):
        result = _extract_path_from_bash("cat ~/notes.md")
        assert result == "~/notes.md"

    def test_relative_dotslash_path(self):
        result = _extract_path_from_bash("cat ./src/main.py")
        assert result == "./src/main.py"

    def test_relative_dotdot_path(self):
        result = _extract_path_from_bash("vim ../config.yaml")
        assert result == "../config.yaml"

    def test_returns_first_path_when_multiple(self):
        result = _extract_path_from_bash("cp /src/foo.py /dst/bar.py")
        assert result == "/src/foo.py"

    def test_no_path_returns_none(self):
        assert _extract_path_from_bash("echo hello world") is None

    def test_pure_directory_no_extension_ignored(self):
        # A bare word like "src" without / or ./ prefix should not match
        assert _extract_path_from_bash("ls src") is None

    def test_empty_command_returns_none(self):
        assert _extract_path_from_bash("") is None

    def test_strips_trailing_punctuation(self):
        result = _extract_path_from_bash("ls /tmp/file.txt;")
        assert result == "/tmp/file.txt"


class TestExtractPathFromTool:
    """Tests for _extract_path_from_tool covering all supported tools."""

    def test_read_tool(self):
        assert _extract_path_from_tool("Read", {"file_path": "/foo/bar.py"}) == "/foo/bar.py"

    def test_write_tool(self):
        assert _extract_path_from_tool("Write", {"file_path": "/foo/out.txt"}) == "/foo/out.txt"

    def test_edit_tool(self):
        assert _extract_path_from_tool("Edit", {"file_path": "/foo/main.py"}) == "/foo/main.py"

    def test_read_file_tool_path_key(self):
        assert _extract_path_from_tool("read_file", {"path": "/a/b.txt"}) == "/a/b.txt"

    def test_read_file_tool_file_path_key(self):
        assert _extract_path_from_tool("read_file", {"file_path": "/a/b.txt"}) == "/a/b.txt"

    def test_str_replace_editor(self):
        assert _extract_path_from_tool("str_replace_editor", {"path": "/x/y.py"}) == "/x/y.py"

    def test_unknown_tool_returns_none(self):
        assert _extract_path_from_tool("unknown_tool", {"path": "/x/y.py"}) is None

    def test_bash_tool_with_absolute_path(self):
        result = _extract_path_from_tool("Bash", {"command": "cat /tmp/data.csv"})
        assert result == "/tmp/data.csv"

    def test_bash_tool_with_cmd_key(self):
        result = _extract_path_from_tool("Bash", {"cmd": "python ~/run.py"})
        assert result == "~/run.py"

    def test_bash_tool_no_path_returns_none(self):
        result = _extract_path_from_tool("Bash", {"command": "echo hello"})
        assert result is None

    def test_bash_tool_empty_command_returns_none(self):
        result = _extract_path_from_tool("Bash", {"command": ""})
        assert result is None

    def test_bash_tool_missing_command_key_returns_none(self):
        result = _extract_path_from_tool("Bash", {})
        assert result is None


class TestRecentFilesTrackerBash:
    """Integration tests: ensure Bash tool paths end up in the tracker."""

    def test_bash_cat_records_path(self, tmp_path, monkeypatch):
        import pocketpaw.recent_files as rf

        monkeypatch.setattr(rf, "_STORE_FILE", tmp_path / "recent_files.json")
        tracker = RecentFilesTracker()

        tracker.record_tool_use("Bash", {"command": "cat /tmp/report.txt"})

        entries = tracker.get_recent()
        assert len(entries) == 1
        assert entries[0]["path"] == "/tmp/report.txt"
        assert entries[0]["tool"] == "Bash"

    def test_bash_no_path_does_not_record(self, tmp_path, monkeypatch):
        import pocketpaw.recent_files as rf

        monkeypatch.setattr(rf, "_STORE_FILE", tmp_path / "recent_files.json")
        tracker = RecentFilesTracker()

        tracker.record_tool_use("Bash", {"command": "echo hello world"})

        assert tracker.get_recent() == []

    def test_bash_path_deduplicates(self, tmp_path, monkeypatch):
        import pocketpaw.recent_files as rf

        monkeypatch.setattr(rf, "_STORE_FILE", tmp_path / "recent_files.json")
        tracker = RecentFilesTracker()

        tracker.record_tool_use("Bash", {"command": "cat /tmp/report.txt"})
        tracker.record_tool_use("Bash", {"command": "cat /tmp/report.txt"})

        entries = tracker.get_recent()
        assert len(entries) == 1

    def test_bash_path_moves_to_top_on_reuse(self, tmp_path, monkeypatch):
        import pocketpaw.recent_files as rf

        monkeypatch.setattr(rf, "_STORE_FILE", tmp_path / "recent_files.json")
        tracker = RecentFilesTracker()

        tracker.record_tool_use("Read", {"file_path": "/a/first.py"})
        tracker.record_tool_use("Bash", {"command": "python /b/second.py"})
        tracker.record_tool_use("Bash", {"command": "cat /a/first.py"})

        entries = tracker.get_recent()
        assert entries[0]["path"] == "/a/first.py"
        assert entries[1]["path"] == "/b/second.py"
