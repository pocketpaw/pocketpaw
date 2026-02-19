# Tests for Frontend JavaScript Syntax Validation
# Created: 2026-02-05
#
# Validates JavaScript files have valid syntax using Node.js.
# This catches syntax errors before they break the UI in production.

import subprocess
from pathlib import Path

import pytest

# Frontend directory
FRONTEND_DIR = Path(__file__).parent.parent / "src" / "pocketpaw" / "frontend"


def get_js_files():
    """Get all JavaScript files in the frontend directory."""
    return list(FRONTEND_DIR.glob("**/*.js"))


class TestJavaScriptSyntax:
    """Tests for JavaScript syntax validation."""

    @pytest.fixture(autouse=True)
    def check_node_available(self):
        """Skip tests if Node.js is not available."""
        try:
            subprocess.run(
                ["node", "--version"],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("Node.js is not available")

    @pytest.mark.parametrize("js_file", get_js_files(), ids=lambda f: f.name)
    def test_javascript_syntax(self, js_file):
        """Test that JavaScript file has valid syntax."""
        result = subprocess.run(
            ["node", "--check", str(js_file)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            pytest.fail(f"JavaScript syntax error in {js_file.name}:\n{result.stderr}")

    def test_app_js_exists(self):
        """Test that main app.js file exists."""
        app_js = FRONTEND_DIR / "js" / "app.js"
        assert app_js.exists(), "Main app.js file should exist"

    def test_websocket_js_exists(self):
        """Test that websocket.js file exists."""
        ws_js = FRONTEND_DIR / "js" / "websocket.js"
        assert ws_js.exists(), "WebSocket handler file should exist"

    def test_tools_js_exists(self):
        """Test that tools.js file exists."""
        tools_js = FRONTEND_DIR / "js" / "tools.js"
        assert tools_js.exists(), "Tools handler file should exist"


class TestJavaScriptStructure:
    """Tests for JavaScript file structure."""

    def test_app_defines_app_function(self):
        """Test that app.js defines the app() function for Alpine.js."""
        app_js = FRONTEND_DIR / "js" / "app.js"
        content = app_js.read_text()

        assert "function app()" in content, "app.js should define app() function"


    def test_websocket_defines_manager(self):
        """Test that websocket.js defines WebSocket manager."""
        ws_js = FRONTEND_DIR / "js" / "websocket.js"
        content = ws_js.read_text()

        assert "WebSocket" in content, "websocket.js should use WebSocket"
